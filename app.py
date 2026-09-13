import os
import sys
import json
import time
import socket
import ssl
import logging
import threading
import subprocess
from urllib.parse import urlparse
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps
import smtplib

import requests
from dotenv import load_dotenv
from flask import Flask, render_template, jsonify, request, session, redirect, url_for, Response

# Load environment variables
load_dotenv()

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

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('logs/uptrace.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('Uptrace')

CONFIG_FILE = 'config.json'
HISTORY_FILE = os.path.join('data', 'uptime_history.json')
STATUS_MD_FILE = 'STATUS.md'

def get_ssl_info(url):
    """Inspects SSL certificate for HTTPS URLs to calculate expiration days"""
    try:
        parsed = urlparse(url)
        if parsed.scheme != 'https':
            return None
        
        hostname = parsed.hostname
        port = parsed.port or 443
        
        ctx = ssl.create_default_context()
        with socket.create_connection((hostname, port), timeout=4) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                cert = ssock.getpeercert()
                not_after_str = cert.get('notAfter')
                if not_after_str:
                    expiry_date = datetime.strptime(not_after_str, '%b %d %H:%M:%S %Y %Z').replace(tzinfo=timezone.utc)
                    days_left = (expiry_date - datetime.now(timezone.utc)).days
                    return {
                        'valid': days_left > 0,
                        'days_left': max(0, days_left),
                        'expiry_date': expiry_date.strftime('%Y-%m-%d'),
                        'issuer': dict(x[0] for x in cert.get('issuer', [])).get('organizationName', 'SSL Provider')
                    }
    except Exception as e:
        logger.debug(f"SSL check note for {url}: {e}")
        return None
    return None

class UptraceEngine:
    """
    Uptrace Core Engine
    - Multi-method HTTP keep-alive pings (GET, POST, HEAD, PUT, DELETE, PATCH, OPTIONS)
    - Database socket & TCP keep-alive pings
    - Per-monitor custom ping scheduling
    - Environment segregation (Production vs Staging vs Development)
    - Multi-channel alerts (Email, Discord, Telegram, Slack)
    """
    def __init__(self, config_path=CONFIG_FILE):
        self.config_path = config_path
        self.config = self.load_config()
        self.incident_state = {}
        self.latest_results_map = {}
        self.service_last_run = {}
        self.last_check_time = None
        self.lock = threading.Lock()
        
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

    def save_config(self):
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=2)
            logger.info("Saved updated configuration to config.json")
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
            # Execute request according to configured HTTP method
            response = requests.request(
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

    def process_incidents(self, results):
        now = time.time()
        cooldown_sec = self.config.get('settings', {}).get('incident_cooldown_minutes', 15) * 60

        for r in results:
            if r['status'] == 'paused':
                continue

            svc_id = r['id']
            curr_status = r['status']
            prev_state = self.incident_state.get(svc_id, {'status': 'healthy', 'last_alert': 0})

            if curr_status in ['unhealthy', 'error']:
                if prev_state['status'] == 'healthy' or (now - prev_state.get('last_alert', 0) > cooldown_sec):
                    logger.warning(f"🚨 INCIDENT TRIGGERED for [{r.get('environment', 'prod').upper()}] {r['name']}: {r.get('error') or r.get('status_code')}")
                    self.dispatch_all_alerts(r, is_recovery=False)
                    self.incident_state[svc_id] = {
                        'status': curr_status,
                        'last_alert': now,
                        'incident_started': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    }
                else:
                    self.incident_state[svc_id]['status'] = curr_status

            elif curr_status == 'healthy' and prev_state.get('status') in ['unhealthy', 'error']:
                logger.info(f"🎉 RECOVERY TRIGGERED for [{r.get('environment', 'prod').upper()}] {r['name']}")
                self.dispatch_all_alerts(r, is_recovery=True)
                self.incident_state[svc_id] = {
                    'status': 'healthy',
                    'last_alert': now,
                    'recovered_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }

    def dispatch_all_alerts(self, service_result, is_recovery=False):
        self.send_email_alert(service_result, is_recovery)
        self.send_discord_alert(service_result, is_recovery)
        self.send_telegram_alert(service_result, is_recovery)
        self.send_slack_alert(service_result, is_recovery)

    def send_email_alert(self, service_result, is_recovery=False):
        smtp_enabled = os.getenv('SMTP_ENABLED', 'false').lower() in ('true', '1', 'yes')
        smtp_host = os.getenv('SMTP_HOST')
        smtp_port = int(os.getenv('SMTP_PORT', '587'))
        smtp_user = os.getenv('SMTP_USER')
        smtp_pass = os.getenv('SMTP_PASSWORD')
        sender = os.getenv('ALERT_SENDER_EMAIL') or smtp_user
        receiver = os.getenv('ALERT_RECEIVER_EMAIL') or smtp_user

        env_tag = service_result.get('environment', 'production').upper()
        subject = f"{'✅ [RESOLVED]' if is_recovery else '🚨 [INCIDENT]'} [{env_tag}] Uptrace Alert: {service_result['name']} is {'back ONLINE' if is_recovery else 'DOWN'}"
        status_color = "#10b981" if is_recovery else "#ef4444"
        headline = "Service Recovered" if is_recovery else "Incident Detected"
        
        html_content = f"""
        <html>
        <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color: #0f101a; color: #ffffff; padding: 20px;">
            <div style="max-width: 600px; margin: 0 auto; background: #151621; border-radius: 16px; padding: 24px; border: 1px solid #1f2433;">
                <h2 style="margin: 0 0 16px 0; color: #ffffff;">⚡ Uptrace • [{env_tag}] Alert</h2>
                <div style="background-color: {status_color}18; border-left: 3px solid {status_color}; padding: 12px 16px; border-radius: 6px; margin-bottom: 20px;">
                    <h3 style="margin: 0 0 4px 0; color: {status_color};">{headline}</h3>
                    <p style="margin: 0; font-size: 14px;"><strong>{service_result['name']}</strong> is {service_result['status'].upper()}.</p>
                </div>
                <table style="width: 100%; border-collapse: collapse; font-size: 13px;">
                    <tr><td style="padding: 6px 0; color: #646e87;">Environment:</td><td style="padding: 6px 0; color: #ffffff;">{env_tag}</td></tr>
                    <tr><td style="padding: 6px 0; color: #646e87;">Target:</td><td style="padding: 6px 0; color: #c9d3ee;">{service_result.get('method', 'GET')} {service_result['target']}</td></tr>
                    <tr><td style="padding: 6px 0; color: #646e87;">Latency:</td><td style="padding: 6px 0; color: #c9d3ee;">{service_result.get('latency_ms', 0)} ms</td></tr>
                    <tr><td style="padding: 6px 0; color: #646e87;">Details:</td><td style="padding: 6px 0; color: #f43f5e;">{service_result.get('error') or 'Operational'}</td></tr>
                </table>
            </div>
        </body>
        </html>
        """

        if not smtp_enabled or not smtp_host or not smtp_user:
            return False

        try:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = sender
            msg['To'] = receiver
            msg.attach(MIMEText(html_content, 'html'))

            server = smtplib.SMTP(smtp_host, smtp_port, timeout=10)
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(sender, [receiver], msg.as_string())
            server.quit()
            logger.info(f"📧 Email alert sent to {receiver} for [{env_tag}] {service_result['name']}")
            return True
        except Exception as e:
            logger.error(f"Failed to send email alert: {e}")
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

    def save_history(self, results):
        entry = {
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
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
        if len(history) > 150:
            history = history[-150:]

        try:
            with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(history, f, indent=2)
        except Exception as e:
            logger.error(f"Failed saving history: {e}")

    def update_status_markdown(self, results):
        total = len(results)
        healthy = len([r for r in results if r['status'] == 'healthy'])
        unhealthy = total - healthy
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')

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

        try:
            subprocess.run(["git", "add", STATUS_MD_FILE, HISTORY_FILE, "logs/"], check=True, capture_output=True, text=True)
            res = subprocess.run(["git", "commit", "-m", commit_msg], capture_output=True, text=True)
            if res.returncode == 0:
                logger.info(f"🌱 Git commit successful: '{commit_msg}'")
                push_res = subprocess.run(["git", "push"], capture_output=True, text=True)
                if push_res.returncode == 0:
                    logger.info("🚀 Git push successful!")
                    return {"status": "success", "message": f"Committed & pushed: {commit_msg}"}
                else:
                    return {"status": "partial", "message": f"Committed locally: {commit_msg}"}
            else:
                return {"status": "no_change", "message": "No new changes to commit"}
        except Exception as e:
            logger.error(f"Git auto-commit failed: {e}")
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

@app.route('/login')
def login_page():
    return redirect(url_for('github_login'))

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
    return redirect('/')

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
        require_auth = os.getenv('REQUIRE_AUTH', 'false').lower() in ('true', '1', 'yes')
        if require_auth and not session.get('user'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Unauthorized: Please log in with GitHub to access this private console.'}), 401
            return redirect(url_for('github_login'))
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
def api_incidents():
    return jsonify({'incidents': monitor.incident_state})

if __name__ == '__main__':
    checker_thread = threading.Thread(target=background_checker_job, daemon=True)
    checker_thread.start()

    port = int(os.getenv('PORT', '5000'))
    logger.info(f"🚀 Starting Uptrace SaaS Server on http://localhost:{port}")
    app.run(debug=True, port=port, use_reloader=False)