import os
import sys
import json
import time
import socket
import ssl
import logging
import threading
import subprocess
import base64
from urllib.parse import urlparse
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps
import smtplib

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv
from flask import Flask, render_template, jsonify, request, session, redirect, url_for, Response

# Load environment variables
load_dotenv()

def get_tz():
    tz_str = os.getenv('TIMEZONE', os.getenv('TZ', 'Asia/Kolkata')).strip()
    try:
        return ZoneInfo(tz_str)
    except Exception:
        return timezone.utc

def get_now():
    return datetime.now(get_tz())

def get_now_str(fmt='%Y-%m-%d %H:%M:%S'):
    return get_now().strftime(fmt)

# Ensure UTF-8 output in Windows consoles
if sys.platform == 'win32':
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# Ensure required directories exist
os.makedirs('logs', exist_ok=True)
os.makedirs('data', exist_ok=True)

class TimezoneFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, get_tz())
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]

# Configure logging with local timezone
log_handler_file = logging.FileHandler('logs/uptrace.log', encoding='utf-8')
log_handler_stream = logging.StreamHandler(sys.stdout)
tz_fmt = TimezoneFormatter('%(asctime)s [%(levelname)s] %(message)s')
log_handler_file.setFormatter(tz_fmt)
log_handler_stream.setFormatter(tz_fmt)

logging.basicConfig(
    level=logging.INFO,
    handlers=[log_handler_file, log_handler_stream]
)
logger = logging.getLogger('Uptrace')

CONFIG_FILE = 'config.json'
HISTORY_FILE = os.path.join('data', 'uptime_history.json')
INCIDENT_STATE_FILE = os.path.join('data', 'incident_state.json')
INCIDENTS_LOG_FILE = os.path.join('data', 'incidents.json')
STATUS_MD_FILE = 'STATUS.md'

_ssl_cache = {}
_ssl_cache_lock = threading.Lock()

def get_ssl_info(url):
    """Inspects SSL certificate for HTTPS URLs to calculate expiration days with TTL in-memory caching"""
    try:
        parsed = urlparse(url)
        if parsed.scheme != 'https':
            return None
        
        hostname = parsed.hostname
        port = parsed.port or 443
        cache_key = f"{hostname}:{port}"

        # 6-hour SSL certificate TTL cache
        with _ssl_cache_lock:
            cached = _ssl_cache.get(cache_key)
            if cached and (time.time() - cached['cached_at']) < 21600:
                return cached['data']
        
        ctx = ssl.create_default_context()
        with socket.create_connection((hostname, port), timeout=4) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                cert = ssock.getpeercert()
                not_after_str = cert.get('notAfter')
                if not_after_str:
                    expiry_date = datetime.strptime(not_after_str, '%b %d %H:%M:%S %Y %Z').replace(tzinfo=timezone.utc)
                    days_left = (expiry_date - datetime.now(timezone.utc)).days
                    data = {
                        'valid': days_left > 0,
                        'days_left': max(0, days_left),
                        'expiry_date': expiry_date.strftime('%Y-%m-%d'),
                        'issuer': dict(x[0] for x in cert.get('issuer', [])).get('organizationName', 'SSL Provider')
                    }
                    with _ssl_cache_lock:
                        _ssl_cache[cache_key] = {'data': data, 'cached_at': time.time()}
                    return data
    except Exception as e:
        logger.debug(f"SSL check note for {url}: {e}")
        return None
    return None

class UptraceEngine:
    """
    Uptrace Core Engine
    - Multi-method HTTP keep-alive pings (GET, POST, HEAD, PUT, DELETE, PATCH, OPTIONS)
    - Database socket & TCP keep-alive pings
    - Connection-pooled persistent sessions for high throughput
    - Per-monitor custom ping scheduling
    - Environment segregation (Production vs Staging vs Development)
    - Multi-channel alerts (Email, Discord, Telegram, Slack)
    """
    def __init__(self, config_path=CONFIG_FILE):
        self.config_path = config_path
        self.config = self.load_config()
        self.incident_state = self.load_incident_state()
        self.latest_results_map = {}
        self.service_last_run = {}
        self.last_check_time = None
        self.lock = threading.Lock()

        # Connection-pooled persistent HTTP session
        self.session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=30,
            pool_maxsize=100,
            max_retries=Retry(total=1, backoff_factor=0.2, status_forcelist=[502, 503, 504])
        )
        self.session.mount('https://', adapter)
        self.session.mount('http://', adapter)
        
    def load_config(self):
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load {self.config_path}: {e}")
            return {
                "project_name": "Uptrace",
                "version": "2.3.0",
                "settings": {"default_timeout_seconds": 6, "max_concurrent_workers": 8, "default_check_interval_seconds": 300},
                "services": []
            }

    def sync_file_to_github(self, file_path, commit_message="chore(uptrace): automated file sync"):
        """Commits file directly to GitHub via REST API without requiring local git binary or .git folder"""
        token = os.getenv('GITHUB_TOKEN')
        if not token:
            return False

        repo = os.getenv('GITHUB_REPOSITORY', 'vardhineeditharak/Uptrace').strip()
        url = f"https://api.github.com/repos/{repo}/contents/{file_path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "Uptrace-SRE-Console"
        }

        try:
            if not os.path.exists(file_path):
                return False

            with open(file_path, 'rb') as f:
                content_bytes = f.read()
            content_b64 = base64.b64encode(content_bytes).decode('utf-8')

            # Fetch existing file SHA if present on GitHub
            get_res = requests.get(url, headers=headers, timeout=10)
            sha = get_res.json().get('sha') if get_res.status_code == 200 else None

            payload = {
                "message": commit_message,
                "content": content_b64,
                "branch": "main"
            }
            if sha:
                payload["sha"] = sha

            put_res = requests.put(url, headers=headers, json=payload, timeout=12)
            if put_res.status_code in (200, 201):
                logger.info(f"🚀 GitHub API: Committed {file_path} to remote repository successfully!")
                return True
            else:
                logger.warning(f"GitHub API commit notice for {file_path}: {put_res.status_code} {put_res.text}")
                return False
        except Exception as e:
            logger.error(f"GitHub API sync error for {file_path}: {e}")
            return False

    def save_config(self):
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=2)
            logger.info("Saved updated configuration to config.json")
            
            # Sync to GitHub if GITHUB_TOKEN is available
            if os.getenv('GITHUB_TOKEN'):
                threading.Thread(
                    target=self.sync_file_to_github, 
                    args=(self.config_path, "chore(uptrace): update config.json via Web UI"),
                    daemon=True
                ).start()

            return True
        except Exception as e:
            logger.error(f"Failed to save {self.config_path}: {e}")
            return False

    def add_monitor(self, data):
        """Adds a new monitor supporting any HTTP method (GET, POST, HEAD, etc.)"""
        with self.lock:
            services = self.config.setdefault('services', [])
            svc_id = data.get('id') or f"mon-{int(time.time())}"
            existing_ids = {s.get('id') for s in services}
            if svc_id in existing_ids:
                svc_id = f"{svc_id}-{int(time.time() % 1000)}"

            new_service = {
                'id': svc_id,
                'name': data.get('name', 'New Monitor'),
                'environment': data.get('environment', 'production').lower(),
                'type': data.get('type', 'http'),
                'category': data.get('category', 'Web Applications'),
                'keep_alive_note': data.get('keep_alive_note', ''),
                'interval_seconds': int(data.get('interval_seconds', 300)),
                'timeout': int(data.get('timeout', 6)),
                'paused': False
            }

            if new_service['type'] == 'tcp_db':
                new_service['host'] = data.get('host', 'localhost')
                new_service['port'] = int(data.get('port', 5432))
            else:
                new_service['url'] = data.get('url', 'https://')
                new_service['method'] = data.get('method', 'GET').upper()
                
                # Custom headers
                custom_headers = data.get('headers')
                if isinstance(custom_headers, str) and custom_headers.strip():
                    try:
                        new_service['headers'] = json.loads(custom_headers)
                    except Exception:
                        new_service['headers'] = {}
                elif isinstance(custom_headers, dict):
                    new_service['headers'] = custom_headers
                else:
                    new_service['headers'] = {}

                # Request body (e.g. for POST, PUT, PATCH)
                if data.get('body'):
                    new_service['body'] = data.get('body')

                expected = data.get('expected_status', [200])
                if isinstance(expected, str):
                    expected = [int(x.strip()) for x in expected.split(',') if x.strip().isdigit()]
                new_service['expected_status'] = expected or [200]

            services.append(new_service)
            self.save_config()
            return new_service

    def update_monitor(self, svc_id, data):
        """Updates an existing monitor"""
        with self.lock:
            services = self.config.get('services', [])
            for s in services:
                if s.get('id') == svc_id:
                    if 'name' in data: s['name'] = data['name']
                    if 'environment' in data: s['environment'] = data['environment'].lower()
                    if 'type' in data: s['type'] = data['type']
                    if 'category' in data: s['category'] = data['category']
                    if 'keep_alive_note' in data: s['keep_alive_note'] = data['keep_alive_note']
                    if 'interval_seconds' in data: s['interval_seconds'] = int(data['interval_seconds'])
                    if 'timeout' in data: s['timeout'] = int(data['timeout'])
                    if 'paused' in data: s['paused'] = bool(data['paused'])
                    
                    if s['type'] == 'tcp_db':
                        if 'host' in data: s['host'] = data['host']
                        if 'port' in data: s['port'] = int(data['port'])
                    else:
                        if 'url' in data: s['url'] = data['url']
                        if 'method' in data: s['method'] = data['method'].upper()
                        if 'body' in data: s['body'] = data['body']
                        if 'headers' in data:
                            custom_headers = data['headers']
                            if isinstance(custom_headers, str) and custom_headers.strip():
                                try:
                                    s['headers'] = json.loads(custom_headers)
                                except Exception:
                                    pass
                            elif isinstance(custom_headers, dict):
                                s['headers'] = custom_headers

                        if 'expected_status' in data:
                            expected = data['expected_status']
                            if isinstance(expected, str):
                                expected = [int(x.strip()) for x in expected.split(',') if x.strip().isdigit()]
                            s['expected_status'] = expected or [200]
                    
                    self.save_config()
                    return s
            return None

    def delete_monitor(self, svc_id):
        with self.lock:
            services = self.config.get('services', [])
            self.config['services'] = [s for s in services if s.get('id') != svc_id]
            if svc_id in self.latest_results_map:
                del self.latest_results_map[svc_id]
            if svc_id in self.service_last_run:
                del self.service_last_run[svc_id]
            self.save_config()
            return True

    def toggle_monitor_pause(self, svc_id):
        with self.lock:
            services = self.config.get('services', [])
            for s in services:
                if s.get('id') == svc_id:
                    s['paused'] = not s.get('paused', False)
                    self.save_config()
                    return s
            return None

    def check_http_service(self, service):
        """Pings HTTP/HTTPS with any method (GET, POST, HEAD, PUT, DELETE, PATCH, OPTIONS)"""
        name = service.get('name', 'Unnamed Service')
        url = service.get('url')
        method = service.get('method', 'GET').upper()
        env = service.get('environment', 'production').lower()
        interval = service.get('interval_seconds', 300)
        timeout = service.get('timeout', self.config.get('settings', {}).get('default_timeout_seconds', 6))
        expected_status = service.get('expected_status', [200])
        if isinstance(expected_status, int):
            expected_status = [expected_status]

        if service.get('paused', False):
            return {
                'id': service.get('id', name),
                'name': name,
                'environment': env,
                'type': service.get('type', 'http'),
                'method': method,
                'target': url,
                'category': service.get('category', 'Web Applications'),
                'keep_alive_note': service.get('keep_alive_note', ''),
                'interval_seconds': interval,
                'status': 'paused',
                'status_code': 'PAUSED',
                'latency_ms': 0,
                'ssl_info': None,
                'error': 'Monitor is paused',
                'last_checked': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }

        headers = {
            'User-Agent': f'Uptrace-KeepAlive/2.3 ({env.capitalize()}; +https://github.com/uptrace)'
        }
        if service.get('headers') and isinstance(service['headers'], dict):
            headers.update(service['headers'])

        body = service.get('body')

        ssl_info = None
        if url and url.startswith('https://'):
            ssl_info = get_ssl_info(url)

        start_time = time.time()
        try:
            # Execute request according to configured HTTP method using persistent pooled session
            response = self.session.request(
                method=method,
                url=url,
                headers=headers,
                data=body.encode('utf-8') if (body and isinstance(body, str)) else body,
                timeout=timeout,
                allow_redirects=True
            )
            latency = round((time.time() - start_time) * 1000, 2)
            
            is_healthy = response.status_code in expected_status
            status = 'healthy' if is_healthy else 'unhealthy'
            error_msg = None if is_healthy else f"Unexpected status {response.status_code} (Expected {expected_status})"

            if is_healthy:
                logger.info(f"✅ [{method}][{env.upper()}][{interval}s] {name} - {status.upper()} ({latency}ms) - {response.status_code}")
            else:
                logger.warning(f"⚠️ [{method}][{env.upper()}][{interval}s] {name} - {status.upper()} ({latency}ms) - {response.status_code}")

            return {
                'id': service.get('id', name),
                'name': name,
                'environment': env,
                'type': service.get('type', 'http'),
                'method': method,
                'target': url,
                'category': service.get('category', 'Web Applications'),
                'keep_alive_note': service.get('keep_alive_note', 'HTTP Keep-Alive Ping'),
                'interval_seconds': interval,
                'status': status,
                'status_code': response.status_code,
                'latency_ms': latency,
                'ssl_info': ssl_info,
                'error': error_msg,
                'last_checked': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
        except requests.exceptions.Timeout:
            latency = round((time.time() - start_time) * 1000, 2)
            logger.error(f"❌ [{method}][{env.upper()}] {name} - TIMEOUT after {timeout}s")
            return {
                'id': service.get('id', name),
                'name': name,
                'environment': env,
                'type': service.get('type', 'http'),
                'method': method,
                'target': url,
                'category': service.get('category', 'Web Applications'),
                'keep_alive_note': service.get('keep_alive_note', 'HTTP Keep-Alive Ping'),
                'interval_seconds': interval,
                'status': 'error',
                'status_code': 'TIMEOUT',
                'latency_ms': latency,
                'ssl_info': ssl_info,
                'error': f"Request timed out after {timeout}s",
                'last_checked': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
        except Exception as e:
            latency = round((time.time() - start_time) * 1000, 2)
            logger.error(f"❌ [{method}][{env.upper()}] {name} - ERROR: {str(e)}")
            return {
                'id': service.get('id', name),
                'name': name,
                'environment': env,
                'type': service.get('type', 'http'),
                'method': method,
                'target': url,
                'category': service.get('category', 'Web Applications'),
                'keep_alive_note': service.get('keep_alive_note', 'HTTP Keep-Alive Ping'),
                'interval_seconds': interval,
                'status': 'error',
                'status_code': 'ERR',
                'latency_ms': latency,
                'ssl_info': ssl_info,
                'error': str(e),
                'last_checked': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }

    def check_tcp_db_service(self, service):
        name = service.get('name', 'Database Target')
        env = service.get('environment', 'production').lower()
        interval = service.get('interval_seconds', 300)
        host = service.get('host', 'localhost')
        port = int(service.get('port', 5432))
        timeout = service.get('timeout', self.config.get('settings', {}).get('default_timeout_seconds', 6))

        if service.get('paused', False):
            return {
                'id': service.get('id', name),
                'name': name,
                'environment': env,
                'type': 'tcp_db',
                'method': 'TCP',
                'target': f"{host}:{port}",
                'category': service.get('category', 'Databases (Keep-Alive)'),
                'keep_alive_note': service.get('keep_alive_note', ''),
                'interval_seconds': interval,
                'status': 'paused',
                'status_code': 'PAUSED',
                'latency_ms': 0,
                'ssl_info': None,
                'error': 'Monitor is paused',
                'last_checked': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }

        start_time = time.time()
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
            sock.close()
            latency = round((time.time() - start_time) * 1000, 2)
            
            logger.info(f"✅ [TCP/DB][{env.upper()}][{interval}s] {name} ({host}:{port}) - HEALTHY ({latency}ms)")
            return {
                'id': service.get('id', name),
                'name': name,
                'environment': env,
                'type': 'tcp_db',
                'method': 'TCP',
                'target': f"{host}:{port}",
                'category': service.get('category', 'Databases (Keep-Alive)'),
                'keep_alive_note': service.get('keep_alive_note', 'Database TCP Connection Ping'),
                'interval_seconds': interval,
                'status': 'healthy',
                'status_code': f"TCP/{port} OPEN",
                'latency_ms': latency,
                'ssl_info': None,
                'error': None,
                'last_checked': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
        except socket.timeout:
            latency = round((time.time() - start_time) * 1000, 2)
            logger.error(f"❌ [TCP/DB][{env.upper()}] {name} ({host}:{port}) - TIMEOUT ({timeout}s)")
            return {
                'id': service.get('id', name),
                'name': name,
                'environment': env,
                'type': 'tcp_db',
                'method': 'TCP',
                'target': f"{host}:{port}",
                'category': service.get('category', 'Databases (Keep-Alive)'),
                'keep_alive_note': service.get('keep_alive_note', 'Database TCP Connection Ping'),
                'interval_seconds': interval,
                'status': 'error',
                'status_code': 'TIMEOUT',
                'latency_ms': latency,
                'ssl_info': None,
                'error': f"Connection to {host}:{port} timed out after {timeout}s",
                'last_checked': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
        except Exception as e:
            latency = round((time.time() - start_time) * 1000, 2)
            logger.error(f"❌ [TCP/DB][{env.upper()}] {name} ({host}:{port}) - REFUSED/UNREACHABLE: {str(e)}")
            return {
                'id': service.get('id', name),
                'name': name,
                'environment': env,
                'type': 'tcp_db',
                'method': 'TCP',
                'target': f"{host}:{port}",
                'category': service.get('category', 'Databases (Keep-Alive)'),
                'keep_alive_note': service.get('keep_alive_note', 'Database TCP Connection Ping'),
                'interval_seconds': interval,
                'status': 'error',
                'status_code': 'CLOSED/ERR',
                'latency_ms': latency,
                'ssl_info': None,
                'error': str(e),
                'last_checked': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }

    def check_service(self, service):
        service_type = service.get('type', 'http')
        if service_type == 'tcp_db':
            return self.check_tcp_db_service(service)
        else:
            return self.check_http_service(service)

    def check_due_services(self):
        self.config = self.load_config()
        services = self.config.get('services', [])
        now = time.time()
        
        due_services = []
        for svc in services:
            svc_id = svc.get('id', svc.get('name'))
            interval = svc.get('interval_seconds', 300)
            last_run = self.service_last_run.get(svc_id, 0)
            
            if (now - last_run) >= interval:
                due_services.append(svc)

        if not due_services:
            return self.get_all_results_list()

        max_workers = self.config.get('settings', {}).get('max_concurrent_workers', 8)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_service = {executor.submit(self.check_service, svc): svc for svc in due_services}
            for future in as_completed(future_to_service):
                try:
                    res = future.result()
                    svc_id = res['id']
                    self.latest_results_map[svc_id] = res
                    self.service_last_run[svc_id] = now
                except Exception as exc:
                    svc = future_to_service[future]
                    logger.error(f"Unhandled error checking {svc.get('name')}: {exc}")

        results = self.get_all_results_list()
        with self.lock:
            self.last_check_time = datetime.now()

        self.process_incidents(results)
        self.save_history(results)
        self.update_status_markdown(results)

        return results

    def check_all(self):
        self.config = self.load_config()
        services = self.config.get('services', [])
        max_workers = self.config.get('settings', {}).get('max_concurrent_workers', 8)
        now = time.time()
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_service = {executor.submit(self.check_service, svc): svc for svc in services}
            for future in as_completed(future_to_service):
                try:
                    res = future.result()
                    svc_id = res['id']
                    self.latest_results_map[svc_id] = res
                    self.service_last_run[svc_id] = now
                except Exception as exc:
                    svc = future_to_service[future]
                    logger.error(f"Unhandled error checking {svc.get('name')}: {exc}")

        results = self.get_all_results_list()
        with self.lock:
            self.last_check_time = datetime.now()

        self.process_incidents(results)
        self.save_history(results)
        self.update_status_markdown(results)

        return results

    def get_all_results_list(self):
        services = self.config.get('services', [])
        results = []
        uptime_stats = self.calculate_uptime_stats()
        
        for svc in services:
            svc_id = svc.get('id', svc.get('name'))
            res = self.latest_results_map.get(svc_id)
            if not res:
                res = {
                    'id': svc_id,
                    'name': svc.get('name'),
                    'environment': svc.get('environment', 'production'),
                    'type': svc.get('type', 'http'),
                    'method': svc.get('method', 'GET') if svc.get('type') != 'tcp_db' else 'TCP',
                    'target': svc.get('url') if svc.get('type') != 'tcp_db' else f"{svc.get('host')}:{svc.get('port')}",
                    'category': svc.get('category', 'Web Applications'),
                    'keep_alive_note': svc.get('keep_alive_note', ''),
                    'interval_seconds': svc.get('interval_seconds', 300),
                    'status': 'healthy',
                    'status_code': 'INITIAL',
                    'latency_ms': 0,
                    'ssl_info': None,
                    'error': None,
                    'last_checked': 'Awaiting check'
                }
            
            res['uptime_stats'] = uptime_stats.get(svc_id, {
                'uptime_24h': 100.0,
                'uptime_7d': 100.0,
                'uptime_30d': 100.0,
                'blocks': ['up'] * 30,
                'avg_latency': res.get('latency_ms', 0)
            })
            res['interval_seconds'] = svc.get('interval_seconds', 300)
            results.append(res)

        return results

    def calculate_uptime_stats(self):
        history = []
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                    history = json.load(f)
            except Exception:
                history = []

        stats_by_service = {}
        services = self.config.get('services', [])
        
        for svc in services:
            svc_id = svc.get('id', svc.get('name'))
            svc_history = []
            latencies = []
            
            for entry in history:
                svc_data = entry.get('services', {}).get(svc_id)
                if svc_data:
                    status = svc_data.get('status', 'healthy')
                    svc_history.append(status)
                    if 'latency' in svc_data and svc_data['latency'] > 0:
                        latencies.append(svc_data['latency'])

            if not svc_history:
                stats_by_service[svc_id] = {
                    'uptime_24h': 100.0,
                    'uptime_7d': 100.0,
                    'uptime_30d': 100.0,
                    'blocks': ['up'] * 30,
                    'avg_latency': 0
                }
                continue

            total_checks = len(svc_history)
            healthy_checks = sum(1 for s in svc_history if s == 'healthy')
            uptime_pct = round((healthy_checks / total_checks) * 100, 2) if total_checks > 0 else 100.0
            
            recent_30 = svc_history[-30:]
            blocks = []
            for s in recent_30:
                if s == 'healthy': blocks.append('up')
                elif s == 'unhealthy': blocks.append('warn')
                elif s == 'paused': blocks.append('paused')
                else: blocks.append('down')
            
            while len(blocks) < 30:
                blocks.insert(0, 'up')

            avg_lat = round(sum(latencies) / len(latencies), 1) if latencies else 0

            stats_by_service[svc_id] = {
                'uptime_24h': uptime_pct,
                'uptime_7d': uptime_pct,
                'uptime_30d': uptime_pct,
                'blocks': blocks,
                'avg_latency': avg_lat
            }

        return stats_by_service

    def load_incident_state(self):
        if os.path.exists(INCIDENT_STATE_FILE):
            try:
                with open(INCIDENT_STATE_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def save_incident_state(self):
        try:
            with open(INCIDENT_STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.incident_state, f, indent=2)
        except Exception as e:
            logger.error(f"Failed saving incident state: {e}")

    def log_incident_event(self, service_result, is_recovery=False):
        svc_id = service_result['id']
        incidents = []
        if os.path.exists(INCIDENTS_LOG_FILE):
            try:
                with open(INCIDENTS_LOG_FILE, 'r', encoding='utf-8') as f:
                    incidents = json.load(f)
            except Exception:
                incidents = []

        now_str = get_now_str()
        now_ts = time.time()

        if not is_recovery:
            open_inc = next((i for i in reversed(incidents) if i.get('service_id') == svc_id and not i.get('resolved_at')), None)
            if not open_inc:
                incidents.append({
                    'id': f"inc-{int(now_ts)}",
                    'service_id': svc_id,
                    'service_name': service_result['name'],
                    'environment': service_result.get('environment', 'production'),
                    'category': service_result.get('category', 'Web Applications'),
                    'target': service_result.get('target', ''),
                    'method': service_result.get('method', 'GET'),
                    'status': service_result['status'],
                    'error': service_result.get('error') or f"HTTP {service_result.get('status_code', 'Error')}",
                    'started_at': now_str,
                    'started_ts': now_ts,
                    'resolved_at': None,
                    'duration_seconds': None
                })
        else:
            for inc in reversed(incidents):
                if inc.get('service_id') == svc_id and not inc.get('resolved_at'):
                    inc['resolved_at'] = now_str
                    started_ts = inc.get('started_ts', now_ts)
                    inc['duration_seconds'] = max(1, int(now_ts - started_ts))
                    break

        if len(incidents) > 300:
            incidents = incidents[-300:]

        try:
            with open(INCIDENTS_LOG_FILE, 'w', encoding='utf-8') as f:
                json.dump(incidents, f, indent=2)
        except Exception as e:
            logger.error(f"Failed logging incident event: {e}")

    def process_incidents(self, results):
        now = time.time()
        cooldown_sec = self.config.get('settings', {}).get('incident_cooldown_minutes', 15) * 60
        state_changed = False

        for r in results:
            if r['status'] == 'paused':
                continue

            svc_id = r['id']
            curr_status = r['status']
            prev_state = self.incident_state.get(svc_id, {'status': 'healthy', 'last_alert': 0})

            if curr_status in ['unhealthy', 'error']:
                if prev_state.get('status') == 'healthy' or (now - prev_state.get('last_alert', 0) > cooldown_sec):
                    logger.warning(f"🚨 IMMEDIATE INCIDENT ALERT: [{r.get('environment', 'prod').upper()}] {r['name']}: {r.get('error') or r.get('status_code')}")
                    self.dispatch_all_alerts(r, is_recovery=False)
                    self.log_incident_event(r, is_recovery=False)
                    self.incident_state[svc_id] = {
                        'status': curr_status,
                        'last_alert': now,
                        'incident_started': get_now_str()
                    }
                    state_changed = True
                else:
                    self.incident_state[svc_id]['status'] = curr_status
                    state_changed = True

            elif curr_status == 'healthy' and prev_state.get('status') in ['unhealthy', 'error']:
                logger.info(f"🎉 IMMEDIATE RECOVERY ALERT: [{r.get('environment', 'prod').upper()}] {r['name']}")
                self.dispatch_all_alerts(r, is_recovery=True)
                self.log_incident_event(r, is_recovery=True)
                self.incident_state[svc_id] = {
                    'status': 'healthy',
                    'last_alert': now,
                    'recovered_at': get_now_str()
                }
                state_changed = True

        if state_changed:
            self.save_incident_state()

    def dispatch_all_alerts(self, service_result, is_recovery=False):
        self.send_email_alert(service_result, is_recovery)
        self.send_discord_alert(service_result, is_recovery)
        self.send_telegram_alert(service_result, is_recovery)
        self.send_slack_alert(service_result, is_recovery)

    def send_email_alert(self, service_result, is_recovery=False):
        smtp_enabled = os.getenv('SMTP_ENABLED', 'false').lower() in ('true', '1', 'yes')
        smtp_host = os.getenv('SMTP_HOST')
        smtp_port = int(os.getenv('SMTP_PORT', '465' if smtp_host == 'smtp.resend.com' else '587'))
        smtp_user = os.getenv('SMTP_USER')
        smtp_pass = os.getenv('SMTP_PASSWORD')
        resend_api_key = os.getenv('RESEND_API_KEY') or (smtp_pass if (smtp_host == 'smtp.resend.com' or (smtp_pass and str(smtp_pass).startswith('re_'))) else None)
        sender = os.getenv('ALERT_SENDER_EMAIL') or smtp_user or "onboarding@resend.dev"
        receiver = os.getenv('ALERT_RECEIVER_EMAIL') or smtp_user

        if not smtp_enabled and not resend_api_key:
            logger.debug("Email alert skipped: SMTP_ENABLED is false and RESEND_API_KEY not configured")
            return False

        if not receiver:
            logger.warning("Email alert skipped: ALERT_RECEIVER_EMAIL is not set")
            return False

        env_tag = service_result.get('environment', 'production').upper()
        status_name = "RECOVERED / ONLINE" if is_recovery else "DOWN / INCIDENT"
        subject = f"{'✅ [RESOLVED]' if is_recovery else '🚨 [INCIDENT ALERT]'} [{env_tag}] {service_result['name']} is {status_name}"
        status_color = "#10b981" if is_recovery else "#ef4444"
        bg_accent = "rgba(16, 185, 129, 0.12)" if is_recovery else "rgba(239, 68, 68, 0.12)"
        headline = "Service Recovered & Fully Operational" if is_recovery else "Critical Service Incident Detected"
        timestamp = get_now_str()

        plain_text = f"""⚡ UPTRACE SRE ALERT
Status: {'RESOLVED' if is_recovery else 'CRITICAL INCIDENT'}
Service: {service_result['name']}
Environment: {env_tag}
Target: {service_result.get('method', 'GET')} {service_result.get('target', '')}
Latency: {service_result.get('latency_ms', 0)} ms
Error: {service_result.get('error') or 'Operational'}
Time: {timestamp}
"""

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head><meta charset="utf-8"></head>
        <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #0b0c14; color: #ffffff; padding: 24px 12px; margin: 0;">
            <div style="max-width: 580px; margin: 0 auto; background: #121422; border-radius: 16px; border: 1px solid #1e2238; overflow: hidden; box-shadow: 0 12px 36px rgba(0,0,0,0.5);">
                <div style="background: linear-gradient(135deg, #17192a 0%, #1f2238 100%); padding: 20px 24px; border-bottom: 1px solid #232742;">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <span style="font-size: 18px; font-weight: 700; color: #ffffff;">⚡ Uptrace SRE</span>
                        <span style="background: rgba(91, 99, 211, 0.2); border: 1px solid #5b63d3; color: #a5b4fc; font-size: 11px; font-weight: 600; padding: 4px 10px; border-radius: 9999px;">{env_tag}</span>
                    </div>
                </div>
                <div style="padding: 24px;">
                    <div style="background: {bg_accent}; border-left: 4px solid {status_color}; padding: 16px; border-radius: 8px; margin-bottom: 20px;">
                        <h2 style="margin: 0 0 6px 0; color: {status_color}; font-size: 18px;">{headline}</h2>
                        <p style="margin: 0; font-size: 14px; color: #c9d3ee;"><strong>{service_result['name']}</strong> is currently <strong>{service_result['status'].upper()}</strong>.</p>
                    </div>
                    <table style="width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 20px;">
                        <tr style="border-bottom: 1px solid #1e2238;"><td style="padding: 10px 0; color: #646e87; width: 35%;">Service:</td><td style="padding: 10px 0; color: #ffffff; font-weight: 600;">{service_result['name']}</td></tr>
                        <tr style="border-bottom: 1px solid #1e2238;"><td style="padding: 10px 0; color: #646e87;">Environment:</td><td style="padding: 10px 0; color: #c9d3ee;"><code style="background: #17192a; padding: 2px 6px; border-radius: 4px;">{env_tag}</code></td></tr>
                        <tr style="border-bottom: 1px solid #1e2238;"><td style="padding: 10px 0; color: #646e87;">Endpoint Target:</td><td style="padding: 10px 0; color: #c9d3ee; font-family: monospace; word-break: break-all;">{service_result.get('method', 'GET')} {service_result.get('target', '')}</td></tr>
                        <tr style="border-bottom: 1px solid #1e2238;"><td style="padding: 10px 0; color: #646e87;">Response Latency:</td><td style="padding: 10px 0; color: #c9d3ee;">{service_result.get('latency_ms', 0)} ms</td></tr>
                        <tr style="border-bottom: 1px solid #1e2238;"><td style="padding: 10px 0; color: #646e87;">HTTP Status / Error:</td><td style="padding: 10px 0; color: {status_color}; font-weight: 600;">{service_result.get('error') or f"HTTP {service_result.get('status_code', '200')}"}</td></tr>
                        <tr><td style="padding: 10px 0; color: #646e87;">Timestamp:</td><td style="padding: 10px 0; color: #c9d3ee;">{timestamp}</td></tr>
                    </table>
                    <div style="text-align: center; margin-top: 24px; padding-top: 16px; border-top: 1px solid #1e2238;">
                        <a href="https://github.com/{os.getenv('GITHUB_REPOSITORY', 'vardhineeditharak/Uptrace')}" style="background: #5b63d3; color: #ffffff; text-decoration: none; padding: 10px 22px; border-radius: 8px; font-size: 13px; font-weight: 600; display: inline-block;">Open Uptrace Status Report</a>
                    </div>
                </div>
            </div>
        </body>
        </html>
        """

        # 1. Native Resend REST API (Fastest, zero port blocks)
        if resend_api_key:
            from_addr = sender if (sender and '@' in sender and not sender.endswith('outlook.com') and not sender.endswith('gmail.com')) else "Uptrace Alerts <onboarding@resend.dev>"
            try:
                resend_payload = {
                    "from": from_addr,
                    "to": [receiver],
                    "subject": subject,
                    "html": html_content,
                    "text": plain_text
                }
                res = requests.post(
                    "https://api.resend.com/emails",
                    headers={
                        "Authorization": f"Bearer {resend_api_key}",
                        "Content-Type": "application/json"
                    },
                    json=resend_payload,
                    timeout=10
                )
                if res.status_code in [200, 201]:
                    logger.info(f"📧 Immediate Email alert dispatched via Resend REST API to {receiver} for [{env_tag}] {service_result['name']}")
                    return True
                else:
                    logger.warning(f"Resend REST API response ({res.status_code}): {res.text}. Trying SMTP fallback...")
            except Exception as e:
                logger.warning(f"Resend REST API exception: {e}. Trying SMTP fallback...")

        # 2. Standard SMTP Relay (smtp.resend.com, Gmail, SES, etc.)
        if not smtp_host or not smtp_user:
            logger.debug("SMTP relay skipped: SMTP_HOST or SMTP_USER missing")
            return False

        try:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = sender
            msg['To'] = receiver
            msg.attach(MIMEText(plain_text, 'plain'))
            msg.attach(MIMEText(html_content, 'html'))

            if smtp_port == 465:
                server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=12)
            else:
                server = smtplib.SMTP(smtp_host, smtp_port, timeout=12)
                server.starttls()

            server.login(smtp_user, smtp_pass)
            server.sendmail(sender, [receiver], msg.as_string())
            server.quit()
            logger.info(f"📧 Immediate Email alert dispatched via SMTP to {receiver} for [{env_tag}] {service_result['name']}")
            return True
        except Exception as e:
            logger.error(f"Failed to send email alert to {receiver}: {e}")
            return False

    def send_discord_alert(self, service_result, is_recovery=False):
        webhook_url = os.getenv('DISCORD_WEBHOOK_URL')
        if not webhook_url: return False
        
        env_tag = service_result.get('environment', 'production').upper()
        color = 0x10b981 if is_recovery else 0xf43f5e
        title = f"{'🟢 [RESOLVED]' if is_recovery else '🔴 [INCIDENT]'} [{env_tag}] {service_result['name']} is {'back ONLINE' if is_recovery else 'DOWN'}"
        
        payload = {
            "embeds": [{
                "title": title,
                "color": color,
                "fields": [
                    {"name": "Environment", "value": f"`{env_tag}`", "inline": True},
                    {"name": "Method", "value": str(service_result.get('method', 'GET')), "inline": True},
                    {"name": "Latency", "value": f"{service_result.get('latency_ms', 0)}ms", "inline": True},
                    {"name": "Target", "value": f"`{service_result['target']}`", "inline": False},
                    {"name": "Details", "value": service_result.get('error') or "Operational", "inline": False}
                ],
                "footer": {"text": "Uptrace • Midnight SRE Console"},
                "timestamp": datetime.now(timezone.utc).isoformat()
            }]
        }
        try:
            requests.post(webhook_url, json=payload, timeout=5)
            logger.info(f"📲 Discord webhook alert sent for {service_result['name']}")
            return True
        except Exception as e:
            logger.error(f"Discord webhook error: {e}")
            return False

    def send_telegram_alert(self, service_result, is_recovery=False):
        token = os.getenv('TELEGRAM_BOT_TOKEN')
        chat_id = os.getenv('TELEGRAM_CHAT_ID')
        if not token or not chat_id: return False

        env_tag = service_result.get('environment', 'production').upper()
        status_emoji = "🟢 RESOLVED" if is_recovery else "🚨 INCIDENT"
        text = f"*{status_emoji}: [{env_tag}] {service_result['name']}*\n" \
               f"Target: `{service_result.get('method', 'GET')} {service_result['target']}`\n" \
               f"Status: `{service_result.get('status_code', 'N/A')}` ({service_result.get('latency_ms', 0)}ms)\n" \
               f"Details: {service_result.get('error') or 'Operational'}\n" \
               f"Time: {service_result.get('last_checked')}"

        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=5)
            logger.info(f"📲 Telegram alert sent for {service_result['name']}")
            return True
        except Exception as e:
            logger.error(f"Telegram alert error: {e}")
            return False

    def send_slack_alert(self, service_result, is_recovery=False):
        webhook_url = os.getenv('SLACK_WEBHOOK_URL')
        if not webhook_url: return False
        
        env_tag = service_result.get('environment', 'production').upper()
        status_text = "RESOLVED" if is_recovery else "INCIDENT"
        color = "#10b981" if is_recovery else "#f43f5e"
        payload = {
            "attachments": [{
                "color": color,
                "title": f"[{status_text}] [{env_tag}] {service_result['name']} is {service_result['status'].upper()}",
                "text": f"Target: {service_result.get('method', 'GET')} {service_result['target']} | Latency: {service_result.get('latency_ms', 0)}ms | Details: {service_result.get('error') or 'None'}",
                "footer": "Uptrace Midnight SRE",
                "ts": int(time.time())
            }]
        }
        try:
            requests.post(webhook_url, json=payload, timeout=5)
            logger.info(f"📲 Slack webhook alert sent for {service_result['name']}")
            return True
        except Exception as e:
            logger.error(f"Slack alert error: {e}")
            return False

    def generate_weekly_report_data(self):
        """Generates comprehensive 7-day performance metrics, uptime SLAs, and incident history"""
        history = []
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                    history = json.load(f)
            except Exception:
                history = []

        incidents = []
        if os.path.exists(INCIDENTS_LOG_FILE):
            try:
                with open(INCIDENTS_LOG_FILE, 'r', encoding='utf-8') as f:
                    incidents = json.load(f)
            except Exception:
                incidents = []

        now = get_now()
        start_date = now - timedelta(days=7)
        date_range_str = f"{start_date.strftime('%d %b %Y')} – {now.strftime('%d %b %Y')}"

        recent_history = []
        tz = get_tz()
        cutoff_dt = now - timedelta(days=7)
        for h in history:
            ts_str = h.get('timestamp')
            if ts_str:
                try:
                    dt = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=tz)
                    if dt >= cutoff_dt:
                        recent_history.append(h)
                except Exception:
                    recent_history.append(h)
            else:
                recent_history.append(h)

        if not recent_history and history:
            recent_history = history[-100:]

        services = self.config.get('services', [])
        latest_results = self.get_all_results_list()
        latest_map = {r['id']: r for r in latest_results}

        total_checks_all = 0
        healthy_checks_all = 0
        all_latencies = []
        services_report = []

        for svc in services:
            svc_id = svc.get('id', svc.get('name'))
            svc_res = latest_map.get(svc_id, {})
            svc_checks = []
            svc_latencies = []

            for h in recent_history:
                s_data = h.get('services', {}).get(svc_id)
                if s_data:
                    st = s_data.get('status', 'healthy')
                    svc_checks.append(st)
                    if s_data.get('latency', 0) > 0:
                        svc_latencies.append(s_data['latency'])
                        all_latencies.append(s_data['latency'])

            total_c = len(svc_checks)
            healthy_c = sum(1 for s in svc_checks if s == 'healthy')
            uptime_pct = round((healthy_c / total_c) * 100, 2) if total_c > 0 else 100.0
            avg_lat = round(sum(svc_latencies) / len(svc_latencies), 1) if svc_latencies else round(svc_res.get('latency_ms', 0), 1)

            total_checks_all += total_c
            healthy_checks_all += healthy_c

            svc_incidents = [inc for inc in incidents if inc.get('service_id') == svc_id]
            ssl_info = svc_res.get('ssl_info')
            ssl_text = f"{ssl_info['days_left']}d left" if ssl_info else "N/A"

            services_report.append({
                'id': svc_id,
                'name': svc.get('name'),
                'environment': svc.get('environment', 'production').upper(),
                'category': svc.get('category', 'Web Applications'),
                'target': svc.get('url') if svc.get('type') != 'tcp_db' else f"{svc.get('host')}:{svc.get('port')}",
                'method': svc.get('method', 'GET') if svc.get('type') != 'tcp_db' else 'TCP',
                'current_status': svc_res.get('status', 'healthy'),
                'uptime_7d': uptime_pct,
                'avg_latency': avg_lat,
                'total_checks': total_c,
                'ssl_days_left': ssl_text,
                'incident_count': len(svc_incidents)
            })

        overall_sla = round((healthy_checks_all / total_checks_all) * 100, 2) if total_checks_all > 0 else 100.0
        avg_latency_all = round(sum(all_latencies) / len(all_latencies), 1) if all_latencies else 0

        recent_incidents = []
        for inc in incidents:
            started_at = inc.get('started_at')
            if started_at:
                try:
                    dt = datetime.strptime(started_at, '%Y-%m-%d %H:%M:%S').replace(tzinfo=tz)
                    if dt >= cutoff_dt:
                        recent_incidents.append(inc)
                except Exception:
                    recent_incidents.append(inc)
            else:
                recent_incidents.append(inc)

        return {
            'date_range': date_range_str,
            'generated_at': get_now_str(),
            'overall_sla': overall_sla,
            'total_services': len(services),
            'total_checks': total_checks_all,
            'total_incidents': len(recent_incidents),
            'avg_latency': avg_latency_all,
            'services': services_report,
            'incidents': recent_incidents
        }

    def send_weekly_report_email(self, recipient=None):
        smtp_enabled = os.getenv('SMTP_ENABLED', 'false').lower() in ('true', '1', 'yes')
        smtp_host = os.getenv('SMTP_HOST')
        smtp_port = int(os.getenv('SMTP_PORT', '465' if smtp_host == 'smtp.resend.com' else '587'))
        smtp_user = os.getenv('SMTP_USER')
        smtp_pass = os.getenv('SMTP_PASSWORD')
        resend_api_key = os.getenv('RESEND_API_KEY') or (smtp_pass if (smtp_host == 'smtp.resend.com' or (smtp_pass and str(smtp_pass).startswith('re_'))) else None)
        sender = os.getenv('ALERT_SENDER_EMAIL') or smtp_user or "onboarding@resend.dev"
        receiver = recipient or os.getenv('WEEKLY_REPORT_RECIPIENT') or os.getenv('ALERT_RECEIVER_EMAIL') or smtp_user

        data = self.generate_weekly_report_data()
        subject = f"📊 [WEEKLY REPORT] Uptrace SRE Performance & Reliability ({data['date_range']}) — {data['overall_sla']}% SLA"

        if not smtp_enabled and not resend_api_key:
            logger.warning("⚠️ Email delivery is disabled or credentials missing. Weekly report not dispatched.")
            return {"status": "skipped", "message": "Email delivery is disabled or not configured in .env", "data": data}

        if not receiver:
            logger.warning("⚠️ Weekly report skipped: No recipient email configured.")
            return {"status": "skipped", "message": "No recipient email configured", "data": data}

        rows_html = ""
        for s in data['services']:
            status_badge = '<span style="color: #10b981; font-weight: 600;">🟢 Healthy</span>' if s['current_status'] == 'healthy' else (
                '<span style="color: #f59e0b; font-weight: 600;">🟡 Warning</span>' if s['current_status'] == 'unhealthy' else '<span style="color: #ef4444; font-weight: 600;">🔴 Error</span>'
            )
            sla_color = "#10b981" if s['uptime_7d'] >= 99.0 else ("#f59e0b" if s['uptime_7d'] >= 95.0 else "#ef4444")
            
            rows_html += f"""
            <tr style="border-bottom: 1px solid #1f2433;">
                <td style="padding: 12px 10px; font-weight: 600; color: #ffffff;">{s['name']}<br><span style="font-size: 11px; font-weight: normal; color: #8892b0;">{s['method']} • {s['category']}</span></td>
                <td style="padding: 12px 10px; text-align: center;"><span style="background: #1e2238; color: #c9d3ee; padding: 3px 8px; border-radius: 4px; font-size: 11px; font-family: monospace;">{s['environment']}</span></td>
                <td style="padding: 12px 10px; text-align: center; font-weight: 700; color: {sla_color}; font-family: monospace;">{s['uptime_7d']}%</td>
                <td style="padding: 12px 10px; text-align: center; color: #c9d3ee; font-family: monospace;">{s['avg_latency']}ms</td>
                <td style="padding: 12px 10px; text-align: center; color: #8892b0; font-size: 12px;">{s['ssl_days_left']}</td>
                <td style="padding: 12px 10px; text-align: center;">{status_badge}</td>
            </tr>
            """

        incidents_html = ""
        if data['incidents']:
            inc_rows = ""
            for inc in data['incidents']:
                dur_text = f"{inc['duration_seconds']}s" if inc.get('duration_seconds') else "Active"
                resolved_text = f"Resolved at {inc['resolved_at']}" if inc.get('resolved_at') else "Ongoing"
                inc_rows += f"""
                <tr style="border-bottom: 1px solid #2d1822;">
                    <td style="padding: 8px 10px; color: #f43f5e; font-weight: 600;">{inc.get('service_name')}</td>
                    <td style="padding: 8px 10px; color: #c9d3ee; font-size: 12px;">{inc.get('error')}</td>
                    <td style="padding: 8px 10px; color: #8892b0; font-size: 12px;">{inc.get('started_at')}</td>
                    <td style="padding: 8px 10px; color: #10b981; font-size: 12px;">{resolved_text} ({dur_text})</td>
                </tr>
                """
            incidents_html = f"""
            <div style="margin-top: 24px;">
                <h3 style="margin: 0 0 12px 0; color: #f43f5e; font-size: 15px;">⚠️ Logged Incidents & Downtime Events ({len(data['incidents'])})</h3>
                <table style="width: 100%; border-collapse: collapse; background: #1a1017; border: 1px solid #3b1822; border-radius: 8px; font-size: 13px;">
                    <thead>
                        <tr style="border-bottom: 1px solid #3b1822; background: #24121d; color: #f43f5e;">
                            <th style="padding: 8px 10px; text-align: left;">Service</th>
                            <th style="padding: 8px 10px; text-align: left;">Root Cause / Status</th>
                            <th style="padding: 8px 10px; text-align: left;">Started</th>
                            <th style="padding: 8px 10px; text-align: left;">Resolution (Duration)</th>
                        </tr>
                    </thead>
                    <tbody>
                        {inc_rows}
                    </tbody>
                </table>
            </div>
            """
        else:
            incidents_html = """
            <div style="margin-top: 24px; background: rgba(16, 185, 129, 0.08); border: 1px solid rgba(16, 185, 129, 0.25); border-radius: 10px; padding: 14px 18px;">
                <div style="color: #10b981; font-weight: 600; font-size: 14px;">
                    <span>🛡️ 100% Reliability — Zero Incidents Logged This Week</span>
                </div>
                <p style="margin: 4px 0 0 0; font-size: 12px; color: #8892b0;">All synthetic keep-alive pings and database connections remained operational without downtime.</p>
            </div>
            """

        overall_sla_color = "#10b981" if data['overall_sla'] >= 99.0 else ("#f59e0b" if data['overall_sla'] >= 95.0 else "#ef4444")

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
        </head>
        <body style="margin: 0; padding: 24px 12px; background-color: #090a10; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #ffffff;">
            <div style="max-width: 680px; margin: 0 auto; background: #11131f; border-radius: 16px; border: 1px solid #1e2238; overflow: hidden; box-shadow: 0 12px 36px rgba(0,0,0,0.5);">
                <!-- Header -->
                <div style="background: linear-gradient(135deg, #17192a 0%, #1a1c33 100%); padding: 24px 28px; border-bottom: 1px solid #232742;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                        <span style="font-size: 20px; font-weight: 700; letter-spacing: -0.5px; color: #ffffff;">⚡ Uptrace</span>
                        <span style="background: rgba(91, 99, 211, 0.2); border: 1px solid #5b63d3; color: #a5b4fc; font-size: 11px; font-weight: 600; padding: 4px 10px; border-radius: 9999px;">WEEKLY SRE REPORT</span>
                    </div>
                    <h1 style="margin: 0 0 6px 0; font-size: 18px; color: #ffffff;">Cloud Reliability & SLA Performance Summary</h1>
                    <div style="font-size: 13px; color: #8892b0;">Period: <strong style="color: #c9d3ee;">{data['date_range']}</strong> • Generated: <strong style="color: #c9d3ee;">{data['generated_at']}</strong></div>
                </div>

                <div style="padding: 24px 28px;">
                    <!-- 4-KPI Metric Cards Grid -->
                    <table style="width: 100%; border-collapse: separate; border-spacing: 10px; margin: -10px -10px 14px -10px;">
                        <tr>
                            <td style="background: #17192a; border: 1px solid #232742; border-radius: 12px; padding: 14px; text-align: center; width: 25%;">
                                <div style="font-size: 11px; color: #8892b0; text-transform: uppercase; margin-bottom: 4px;">7-Day SLA</div>
                                <div style="font-size: 22px; font-weight: 800; color: {overall_sla_color};">{data['overall_sla']}%</div>
                                <div style="font-size: 10px; color: #646e87; margin-top: 2px;">Overall Availability</div>
                            </td>
                            <td style="background: #17192a; border: 1px solid #232742; border-radius: 12px; padding: 14px; text-align: center; width: 25%;">
                                <div style="font-size: 11px; color: #8892b0; text-transform: uppercase; margin-bottom: 4px;">Monitors</div>
                                <div style="font-size: 22px; font-weight: 800; color: #60a5fa;">{data['total_services']}</div>
                                <div style="font-size: 10px; color: #646e87; margin-top: 2px;">Active Endpoints</div>
                            </td>
                            <td style="background: #17192a; border: 1px solid #232742; border-radius: 12px; padding: 14px; text-align: center; width: 25%;">
                                <div style="font-size: 11px; color: #8892b0; text-transform: uppercase; margin-bottom: 4px;">Incidents</div>
                                <div style="font-size: 22px; font-weight: 800; color: {'#10b981' if data['total_incidents'] == 0 else '#f43f5e'};">{data['total_incidents']}</div>
                                <div style="font-size: 10px; color: #646e87; margin-top: 2px;">7d Downtime Events</div>
                            </td>
                            <td style="background: #17192a; border: 1px solid #232742; border-radius: 12px; padding: 14px; text-align: center; width: 25%;">
                                <div style="font-size: 11px; color: #8892b0; text-transform: uppercase; margin-bottom: 4px;">Avg Latency</div>
                                <div style="font-size: 22px; font-weight: 800; color: #c084fc;">{data['avg_latency']}ms</div>
                                <div style="font-size: 10px; color: #646e87; margin-top: 2px;">Global Response</div>
                            </td>
                        </tr>
                    </table>

                    <!-- Service Health Table -->
                    <div style="margin-top: 16px;">
                        <h3 style="margin: 0 0 10px 0; font-size: 15px; color: #ffffff;">📊 Service Availability & Latency Matrix</h3>
                        <table style="width: 100%; border-collapse: collapse; background: #151726; border: 1px solid #20243b; border-radius: 10px; overflow: hidden; font-size: 13px;">
                            <thead>
                                <tr style="background: #1a1d30; color: #8892b0; font-size: 11px; text-transform: uppercase; border-bottom: 1px solid #20243b;">
                                    <th style="padding: 10px; text-align: left;">Service / Target</th>
                                    <th style="padding: 10px; text-align: center;">Env</th>
                                    <th style="padding: 10px; text-align: center;">7d SLA</th>
                                    <th style="padding: 10px; text-align: center;">Avg Latency</th>
                                    <th style="padding: 10px; text-align: center;">SSL Cert</th>
                                    <th style="padding: 10px; text-align: center;">Status</th>
                                </tr>
                            </thead>
                            <tbody>
                                {rows_html}
                            </tbody>
                        </table>
                    </div>

                    <!-- Incidents Block -->
                    {incidents_html}

                    <!-- Footer / Actions -->
                    <div style="margin-top: 28px; padding-top: 20px; border-top: 1px solid #1e2238; text-align: center; color: #646e87; font-size: 12px;">
                        <p style="margin: 0 0 12px 0;">Generated automatically by <strong>Uptrace Midnight SRE Monitor</strong>.</p>
                        <div style="display: inline-block;">
                            <a href="https://github.com/{os.getenv('GITHUB_REPOSITORY', 'vardhineeditharak/Uptrace')}" style="background: #5b63d3; color: #ffffff; text-decoration: none; padding: 8px 18px; border-radius: 8px; font-size: 12px; font-weight: 600; margin: 0 4px; display: inline-block;">View Repository & STATUS.md</a>
                        </div>
                    </div>
                </div>
            </div>
        </body>
        </html>
        """

        plain_text = f"Uptrace Weekly SRE Report ({data['date_range']})\nOverall 7-Day SLA: {data['overall_sla']}%\nActive Services: {data['total_services']}\nTotal Incidents: {data['total_incidents']}\nAvg Latency: {data['avg_latency']}ms\n"

        # 1. Native Resend REST API (Fastest, zero port blocks)
        if resend_api_key:
            from_addr = sender if (sender and '@' in sender and not sender.endswith('outlook.com') and not sender.endswith('gmail.com')) else "Uptrace <onboarding@resend.dev>"
            try:
                resend_payload = {
                    "from": from_addr,
                    "to": [receiver],
                    "subject": subject,
                    "html": html_content,
                    "text": plain_text
                }
                res = requests.post(
                    "https://api.resend.com/emails",
                    headers={
                        "Authorization": f"Bearer {resend_api_key}",
                        "Content-Type": "application/json"
                    },
                    json=resend_payload,
                    timeout=12
                )
                if res.status_code in [200, 201]:
                    logger.info(f"📧 Weekly SRE report successfully mailed via Resend REST API to {receiver}")
                    return {"status": "success", "recipient": receiver, "subject": subject, "data": data}
                else:
                    logger.warning(f"Resend REST API response ({res.status_code}): {res.text}. Trying SMTP relay fallback...")
            except Exception as e:
                logger.warning(f"Resend REST API exception: {e}. Trying SMTP fallback...")

        # 2. Standard SMTP Relay
        if not smtp_host or not smtp_user:
            logger.warning("⚠️ SMTP relay is incomplete in .env. Weekly report not dispatched via email.")
            return {"status": "skipped", "message": "SMTP relay not configured", "data": data}

        try:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = sender
            msg['To'] = receiver
            msg.attach(MIMEText(plain_text, 'plain'))
            msg.attach(MIMEText(html_content, 'html'))

            if smtp_port == 465:
                server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=12)
            else:
                server = smtplib.SMTP(smtp_host, smtp_port, timeout=12)
                server.starttls()

            server.login(smtp_user, smtp_pass)
            server.sendmail(sender, [receiver], msg.as_string())
            server.quit()
            logger.info(f"📧 Weekly SRE report successfully mailed via SMTP to {receiver}")
            return {"status": "success", "recipient": receiver, "subject": subject, "data": data}
        except Exception as e:
            logger.error(f"Failed to send weekly report email: {e}")
            return {"status": "error", "error": str(e), "data": data}

    def save_history(self, results):
        entry = {
            'timestamp': get_now_str('%Y-%m-%d %H:%M:%S'),
            'total': len(results),
            'healthy': len([r for r in results if r['status'] == 'healthy']),
            'unhealthy': len([r for r in results if r['status'] in ['unhealthy', 'error']]),
            'services': {r['id']: {'status': r['status'], 'latency': r['latency_ms'], 'env': r.get('environment', 'production'), 'method': r.get('method', 'GET')} for r in results}
        }
        
        history = []
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                    history = json.load(f)
            except Exception:
                history = []
        
        history.append(entry)
        if len(history) > 1000:
            history = history[-1000:]

        try:
            with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(history, f, indent=2)
        except Exception as e:
            logger.error(f"Failed saving history: {e}")

    def update_status_markdown(self, results):
        total = len(results)
        healthy = len([r for r in results if r['status'] == 'healthy'])
        unhealthy = total - healthy
        tz_code = os.getenv('TIMEZONE_CODE', 'IST')
        now_str = get_now_str(f'%Y-%m-%d %H:%M:%S {tz_code}')

        overall_badge = "https://img.shields.io/badge/Status-Operational-brightgreen" if unhealthy == 0 else "https://img.shields.io/badge/Status-Degraded-red"
        
        def format_interval(sec):
            if sec < 60: return f"{sec}s"
            elif sec < 3600: return f"{sec//60}m"
            else: return f"{sec//3600}h"

        rows = []
        for r in results:
            badge = "🟢 Healthy" if r['status'] == 'healthy' else ("🟡 Warning" if r['status'] == 'unhealthy' else ("⏸️ Paused" if r['status'] == 'paused' else "🔴 Error"))
            note = r.get('keep_alive_note', '')
            env_badge = f"`{r.get('environment', 'prod').upper()}`"
            method_badge = f"`{r.get('method', 'GET')}`"
            schedule_tag = f"`Every {format_interval(r.get('interval_seconds', 300))}`"
            error_text = f"`{r.get('error')}`" if r.get('error') else "-"
            ssl_text = f"SSL: {r['ssl_info']['days_left']}d left" if r.get('ssl_info') else "-"
            uptime_pct = f"{r.get('uptime_stats', {}).get('uptime_24h', 100.0)}%"
            rows.append(f"| **{r['name']}** | {env_badge} | {method_badge} | `{r['category']}` | {badge} | {schedule_tag} | `{uptime_pct}` | `{r.get('status_code', '-')}` | `{r.get('latency_ms', 0)}ms` | {ssl_text} | {note} | {error_text} |")

        markdown_content = f"""# ⚡ Uptrace Status Report (Midnight SRE Console)

![Uptrace Status]({overall_badge})
![Total Services](https://img.shields.io/badge/Monitored_Services-{total}-blue)
![Healthy](https://img.shields.io/badge/Healthy-{healthy}-success)
![Issues](https://img.shields.io/badge/Issues-{unhealthy}-{'red' if unhealthy > 0 else 'lightgrey'})
![Last Checked](https://img.shields.io/badge/Last_Ping-{now_str.replace(' ', '_').replace('-', '--')}-informational)

Automated synthetic health check and keep-alive ping report. This file is continuously updated by **Uptrace** to monitor web applications, keep cloud databases away from inactivity sleep, and record real-time uptime metrics.

---

## 📊 Service Health & Schedule Matrix

| Service Name | Env | Method | Category | Status | Schedule | 24h Uptime | Status Code | Latency | SSL Cert | Keep-Alive Notice | Error / Incident |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
{chr(10).join(rows)}

---

## 🕒 Last Sync

- **Timestamp**: `{now_str}`
- **Active Databases Pinged**: `{len([r for r in results if 'Database' in r.get('category', '')])}`
- **Web Applications Pinged**: `{len([r for r in results if 'Web' in r.get('category', '')])}`
- **System Status**: `{"All Systems Nominal" if unhealthy == 0 else f"{unhealthy} System(s) Experiencing Degradation"}`

> *Generated automatically by [Uptrace](https://github.com/uptrace/keep-alive-monitor)*
"""
        try:
            with open(STATUS_MD_FILE, 'w', encoding='utf-8') as f:
                f.write(markdown_content)
        except Exception as e:
            logger.error(f"Failed writing STATUS.md: {e}")

    def commit_and_push_to_git(self):
        auto_commit = os.getenv('ENABLE_GIT_AUTO_COMMIT', 'false').lower() in ('true', '1', 'yes')
        if not auto_commit:
            logger.info("ℹ️ Git auto-commit is disabled in .env (ENABLE_GIT_AUTO_COMMIT=false). Skipping git commit.")
            return {"status": "skipped", "message": "ENABLE_GIT_AUTO_COMMIT is false"}

        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        commit_msg = f"chore(uptrace): automated health check & keep-alive ping [{timestamp}]"

        # 1. Primary Cloud Sync via GitHub REST API (No git CLI required)
        if os.getenv('GITHUB_TOKEN'):
            c_ok = self.sync_file_to_github(CONFIG_FILE, f"chore(uptrace): sync config.json [{timestamp}]")
            s_ok = self.sync_file_to_github(STATUS_MD_FILE, f"chore(uptrace): automated health check & status report [{timestamp}]")
            if c_ok or s_ok:
                logger.info(f"🚀 GitHub API: Cloud sync completed for [{timestamp}]")
                return {"status": "success", "message": f"Committed via GitHub API: {commit_msg}"}

        # 2. Local Git CLI fallback
        try:
            gh_token = os.getenv('GITHUB_TOKEN')
            if gh_token:
                repo_url = f"https://x-access-token:{gh_token}@github.com/vardhineeditharak/Uptrace.git"
                subprocess.run(["git", "remote", "set-url", "origin", repo_url], capture_output=True, text=True)

            user_name = os.getenv('GIT_COMMIT_AUTHOR_NAME', 'vardhineeditharak')
            user_email = os.getenv('GIT_COMMIT_AUTHOR_EMAIL', 'vardhineedi.tharak@gmail.com')
            subprocess.run(["git", "config", "user.name", user_name], capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", user_email], capture_output=True, text=True)

            subprocess.run(["git", "add", CONFIG_FILE], capture_output=True, text=True)
            subprocess.run(["git", "add", "-f", STATUS_MD_FILE], capture_output=True, text=True)
            subprocess.run(["git", "add", "-f", HISTORY_FILE], capture_output=True, text=True)
            subprocess.run(["git", "add", "-f", "logs/"], capture_output=True, text=True)
            res = subprocess.run(["git", "commit", "-m", commit_msg], capture_output=True, text=True)
            if res.returncode == 0:
                logger.info(f"🌱 Git commit successful: '{commit_msg}'")
                push_res = subprocess.run(["git", "push", "origin", "HEAD"], capture_output=True, text=True)
                if push_res.returncode == 0:
                    logger.info("🚀 Git push successful!")
                    return {"status": "success", "message": f"Committed & pushed: {commit_msg}"}
                else:
                    logger.warning(f"Git push warning: {push_res.stderr}")
                    return {"status": "partial", "message": f"Committed locally: {commit_msg}"}
            else:
                return {"status": "no_change", "message": "No new changes to commit"}
        except Exception as e:
            logger.error(f"Git auto-commit note: {e}")
            return {"status": "error", "error": str(e)}

# Initialize monitor engine
monitor = UptraceEngine()

def background_checker_job():
    logger.info("⏱️ Uptrace multi-schedule background engine started (Ticking every 5s)")
    try:
        monitor.check_all()
    except Exception as e:
        logger.error(f"Initial check error: {e}")

    while True:
        try:
            monitor.check_due_services()
            if os.getenv('ENABLE_GIT_AUTO_COMMIT', 'false').lower() in ('true', '1', 'yes'):
                monitor.commit_and_push_to_git()
        except Exception as e:
            logger.error(f"Background check error: {e}")
        time.sleep(5)

# Initialize Flask app
app = Flask(__name__, static_folder='static', static_url_path='/static')
app.secret_key = os.getenv('SECRET_KEY', 'uptrace-super-secret-key-session')

# --- GITHUB OAUTH & AUTHENTICATION ROUTES ---

@app.after_request
def add_security_and_cache_headers(response):
    if request.path.startswith('/static/'):
        response.headers['Cache-Control'] = 'public, max-age=86400'
    else:
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response

@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if session.get('user'):
        return redirect('/')

    error = None
    if request.method == 'POST':
        entered_password = request.form.get('password', '').strip()
        configured_password = os.getenv('ADMIN_PASSWORD', 'admin123').strip()

        if configured_password and entered_password == configured_password:
            session['user'] = {
                'login': 'admin',
                'name': 'Administrator',
                'avatar_url': 'https://avatars.githubusercontent.com/u/9919?s=200&v=4',
                'html_url': 'https://github.com',
                'is_admin_pass': True,
                'is_demo': False
            }
            logger.info("👤 Admin authenticated via Master Password")
            return redirect('/')
        else:
            error = "Invalid master password. Please verify your credentials."

    has_github_oauth = bool(os.getenv('GITHUB_CLIENT_ID') and os.getenv('GITHUB_CLIENT_SECRET'))
    enable_demo = os.getenv('ENABLE_DEMO_LOGIN', 'true').lower() in ('true', '1', 'yes')

    return render_template(
        'login.html',
        error=error,
        has_github_oauth=has_github_oauth,
        enable_demo=enable_demo
    )

@app.route('/login/github')
def github_login():
    client_id = os.getenv('GITHUB_CLIENT_ID')
    enable_demo = os.getenv('ENABLE_DEMO_LOGIN', 'true').lower() in ('true', '1', 'yes')

    if not client_id:
        if enable_demo:
            # Fallback to demo login if GITHUB_CLIENT_ID not configured
            session['user'] = {
                'login': 'developer',
                'name': 'SRE Engineer',
                'avatar_url': 'https://avatars.githubusercontent.com/u/583231?v=4',
                'html_url': 'https://github.com',
                'is_demo': True
            }
        return redirect('/')

    redirect_uri = url_for('github_callback', _external=True)
    github_auth_url = f"https://github.com/login/oauth/authorize?client_id={client_id}&redirect_uri={redirect_uri}&scope=read:user,user:email"
    return redirect(github_auth_url)

@app.route('/login/github/callback')
def github_callback():
    code = request.args.get('code')
    if not code:
        return redirect('/')

    client_id = os.getenv('GITHUB_CLIENT_ID')
    client_secret = os.getenv('GITHUB_CLIENT_SECRET')
    allowed_users_raw = os.getenv('ALLOWED_GITHUB_USERS', '').strip()
    allowed_users = [u.strip().lower() for u in allowed_users_raw.split(',') if u.strip()]

    try:
        token_res = requests.post(
            'https://github.com/login/oauth/access_token',
            headers={'Accept': 'application/json'},
            data={
                'client_id': client_id,
                'client_secret': client_secret,
                'code': code
            },
            timeout=10
        ).json()

        access_token = token_res.get('access_token')
        if access_token:
            user_res = requests.get(
                'https://api.github.com/user',
                headers={'Authorization': f'token {access_token}', 'Accept': 'application/json'},
                timeout=10
            ).json()

            user_login = user_res.get('login', '').strip()

            # Security Whitelist Enforcement
            if allowed_users and user_login.lower() not in allowed_users:
                logger.warning(f"🚫 Unauthorized GitHub user tried to log in: @{user_login}")
                session.pop('user', None)
                return "403 Forbidden: Your GitHub account is not on the ALLOWED_GITHUB_USERS whitelist for this private Uptrace instance.", 403

            session['user'] = {
                'login': user_login,
                'name': user_res.get('name') or user_login,
                'avatar_url': user_res.get('avatar_url', 'https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png'),
                'html_url': user_res.get('html_url', 'https://github.com'),
                'is_demo': False
            }
            logger.info(f"👤 GitHub User authenticated: @{user_login}")
    except Exception as e:
        logger.error(f"GitHub OAuth Callback error: {e}")

    return redirect('/')

@app.route('/login/demo')
def demo_login():
    """1-Click Demo Login (can be disabled in production via ENABLE_DEMO_LOGIN=false)"""
    enable_demo = os.getenv('ENABLE_DEMO_LOGIN', 'true').lower() in ('true', '1', 'yes')
    if not enable_demo:
        return "403 Forbidden: Demo login is disabled in production.", 403

    session['user'] = {
        'login': 'lead-sre',
        'name': 'DevOps Lead',
        'avatar_url': 'https://avatars.githubusercontent.com/u/9919?s=200&v=4',
        'html_url': 'https://github.com',
        'is_demo': True
    }
    return redirect('/')

@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('login_page'))

@app.route('/api/me')
def api_me():
    """Returns current user authentication state"""
    user = session.get('user')
    return jsonify({
        'authenticated': bool(user),
        'user': user or None
    })

def login_required_if_enabled(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        require_auth = os.getenv('REQUIRE_AUTH', 'true').lower() in ('true', '1', 'yes')
        if require_auth and not session.get('user'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Unauthorized: Please log in to access this private SRE console.'}), 401
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated_function

# --- DASHBOARD & STATUS ROUTES ---

@app.route('/')
@login_required_if_enabled
def dashboard():
    return render_template('dashboard.html', project_name="Uptrace", user=session.get('user'))

@app.route('/STATUS.md')
@app.route('/status')
@login_required_if_enabled
def serve_status_markdown():
    if os.path.exists(STATUS_MD_FILE):
        with open(STATUS_MD_FILE, 'r', encoding='utf-8') as f:
            content = f.read()
        return Response(content, mimetype='text/markdown; charset=utf-8')
    return "STATUS.md not generated yet", 404

@app.route('/api/health-check')
@login_required_if_enabled
def api_health_check():
    results = monitor.check_all()
    total = len(results)
    healthy = len([r for r in results if r['status'] == 'healthy'])
    unhealthy = total - healthy
    return jsonify({
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total_services': total,
        'healthy_services': healthy,
        'unhealthy_services': unhealthy,
        'results': results
    })

@app.route('/api/status-latest')
@login_required_if_enabled
def api_status_latest():
    results = monitor.get_all_results_list()
    last_time = monitor.last_check_time.strftime('%Y-%m-%d %H:%M:%S') if monitor.last_check_time else datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    total = len(results)
    healthy = len([r for r in results if r['status'] == 'healthy'])
    unhealthy = total - healthy
    return jsonify({
        'timestamp': last_time,
        'total_services': total,
        'healthy_services': healthy,
        'unhealthy_services': unhealthy,
        'results': results
    })

# --- MONITOR CRUD API ENDPOINTS ---

@app.route('/api/monitors', methods=['POST'])
@login_required_if_enabled
def api_add_monitor():
    data = request.get_json() or {}
    if not data.get('name'):
        return jsonify({'error': 'Monitor name is required'}), 400
    
    if data.get('type') == 'tcp_db' and not data.get('host'):
        return jsonify({'error': 'Host is required for TCP Database monitor'}), 400
    elif data.get('type') != 'tcp_db' and not data.get('url'):
        return jsonify({'error': 'URL is required for HTTP monitor'}), 400

    new_svc = monitor.add_monitor(data)
    monitor.check_all()
    return jsonify({'status': 'success', 'monitor': new_svc}), 201

@app.route('/api/monitors/<svc_id>', methods=['PUT'])
@login_required_if_enabled
def api_update_monitor(svc_id):
    data = request.get_json() or {}
    updated = monitor.update_monitor(svc_id, data)
    if not updated:
        return jsonify({'error': 'Monitor not found'}), 404
    monitor.check_all()
    return jsonify({'status': 'success', 'monitor': updated})

@app.route('/api/monitors/<svc_id>', methods=['DELETE'])
@login_required_if_enabled
def api_delete_monitor(svc_id):
    monitor.delete_monitor(svc_id)
    monitor.check_all()
    return jsonify({'status': 'success', 'message': f'Monitor {svc_id} deleted'})

@app.route('/api/monitors/<svc_id>/toggle', methods=['POST'])
@login_required_if_enabled
def api_toggle_monitor(svc_id):
    svc = monitor.toggle_monitor_pause(svc_id)
    if not svc:
        return jsonify({'error': 'Monitor not found'}), 404
    monitor.check_all()
    return jsonify({'status': 'success', 'monitor': svc})

@app.route('/api/trigger-git-commit', methods=['POST'])
@login_required_if_enabled
def api_trigger_git_commit():
    res = monitor.commit_and_push_to_git()
    return jsonify(res)

@app.route('/api/history')
@login_required_if_enabled
def api_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                history = json.load(f)
            return jsonify({'history': history})
        except Exception as e:
            return jsonify({'error': str(e), 'history': []})
    return jsonify({'history': []})

@app.route('/api/incidents')
@login_required_if_enabled
def api_incidents():
    return jsonify({'incidents': monitor.incident_state})

@app.route('/api/incidents/history')
@login_required_if_enabled
def api_incidents_history():
    if os.path.exists(INCIDENTS_LOG_FILE):
        try:
            with open(INCIDENTS_LOG_FILE, 'r', encoding='utf-8') as f:
                incidents = json.load(f)
            return jsonify({'incidents': incidents})
        except Exception as e:
            return jsonify({'error': str(e), 'incidents': []})
    return jsonify({'incidents': []})

@app.route('/api/weekly-report')
@login_required_if_enabled
def api_weekly_report():
    data = monitor.generate_weekly_report_data()
    return jsonify(data)

@app.route('/api/alerts/send-weekly-report', methods=['POST'])
@login_required_if_enabled
def api_send_weekly_report():
    body = request.get_json() or {}
    recipient = body.get('recipient')
    res = monitor.send_weekly_report_email(recipient=recipient)
    return jsonify(res)

@app.route('/api/alerts/test-incident', methods=['POST'])
@login_required_if_enabled
def api_test_incident_alert():
    # Send a simulated test alert
    sample_result = {
        'id': 'test-mon-sample',
        'name': 'Sample Web Application (Test Alert)',
        'environment': 'production',
        'type': 'http',
        'method': 'GET',
        'target': 'https://example.com/health',
        'category': 'Web Applications',
        'status': 'unhealthy',
        'status_code': 503,
        'latency_ms': 1240.5,
        'error': '503 Service Unavailable (Test Incident Alert verification)',
        'last_checked': get_now_str()
    }
    sent = monitor.send_email_alert(sample_result, is_recovery=False)
    receiver = os.getenv('ALERT_RECEIVER_EMAIL') or os.getenv('SMTP_USER') or 'configured email'
    if sent:
        return jsonify({'status': 'success', 'message': f'Test incident alert email dispatched to {receiver}'})
    else:
        return jsonify({'status': 'error', 'message': 'Failed sending test alert. Check SMTP_ENABLED and SMTP credentials in .env'}), 400

if __name__ == '__main__':
    checker_thread = threading.Thread(target=background_checker_job, daemon=True)
    checker_thread.start()

    port = int(os.getenv('PORT', '8000'))
    logger.info(f"🚀 Starting Uptrace SaaS Server on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)