# ![Uptrace](static/assets/uptrace-logo.svg)

Commercial-Grade Cloud Web Application & Database Keep-Alive Monitor with Incident Alerts and GitHub Authentication.

[![Uptrace Status](https://img.shields.io/badge/Status-Operational-brightgreen)](STATUS.md)
[![Python Version](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![GitHub Actions](https://img.shields.io/badge/GitHub_Actions-24%2F7_Monitoring-purple.svg)](.github/workflows/uptrace-keepalive.yml)
[![Design System](https://img.shields.io/badge/Theme-Midnight_SRE_Console-5b63d3.svg)](design.md)

---

## 🎯 What Uptrace Does

Modern cloud hosting platforms (**Render**, **Railway**, **Fly.io**, **Vercel**, **Heroku**) and serverless databases (**Supabase**, **Neon**, **PlanetScale**, **Upstash Redis**) automatically place inactive projects into sleep, pause, or cold-standby states after 15 minutes to a few days of zero incoming traffic.

**Uptrace** is an enterprise-ready, self-hosted synthetic keep-alive and incident response platform:

1. 🛡️ **Prevents Inactivity Sleep**: Continuously pings deployed web applications and establishes TCP socket handshakes with cloud databases to keep them warm, active, and responsive.
2. 🔄 **Multi-Method HTTP Protocol**: Executes `GET`, `POST`, `HEAD`, `PUT`, `DELETE`, `PATCH`, and `OPTIONS` requests with custom HTTP headers and JSON/text payload assertions.
3. 🗄️ **Direct TCP Database Socket Health**: Performs raw TCP socket handshakes for PostgreSQL (`5432`), MySQL (`3306`), Redis (`6379`), and custom ports.
4. 🔐 **GitHub Authentication & Access Control**: Secure GitHub OAuth 2.0 login with username whitelisting (`ALLOWED_GITHUB_USERS`) and private dashboard locking (`REQUIRE_AUTH=true`).
5. 🚨 **Instant Multi-Channel Incident Alerts**: Dispatches automated HTML alerts when services go down, and automatic resolution notices upon recovery (Email/SMTP, Discord, Telegram, Slack).
6. ⏱️ **Per-Monitor Granular Schedules**: Customize frequencies independently (`30s`, `1m`, `2m`, `5m`, `15m`, `1h`, `6h`, `24h`) to match each service's sleep threshold.
7. 🤖 **24/7 Automated Cloud Monitoring**: Automatically executes health checks and updates [`STATUS.md`](STATUS.md) on a scheduled GitHub Actions cron.
8. 🎨 **Midnight SRE Console UI**: Better Stack dark aesthetics with custom SVG vector brand assets, Partner Trust Bar, and live latency charts.

---

## ✨ Key Features & Architecture

- **Multi-Protocol & Multi-Method Monitoring**:
  - **HTTP/HTTPS**: Supports `GET`, `POST`, `HEAD`, `PUT`, `DELETE`, `PATCH`, `OPTIONS`, custom request headers, request bodies, and expected status codes.
  - **TCP Sockets / Databases**: Direct socket connectivity checks for PostgreSQL (`5432`), MySQL (`3306`), MongoDB Atlas (`27017`), Redis (`6379`), etc.
  - **Serverless DB REST Endpoints**: Pings Supabase, Neon, Firebase, or custom health routes.
- **Per-Monitor Individual Ping Scheduling**:
  - Configure each monitor's frequency independently (`30s`, `1m`, `2m`, `5m`, `15m`, `1h`, `6h`, `24h`).
- **Environment Segregation**:
  - Filter and manage `Production`, `Development`, and `Staging` environments separately.
- **GitHub Authentication & Private Whitelist**:
  - Real OAuth login via GitHub (`/login/github`) with username whitelisting (`ALLOWED_GITHUB_USERS`).
  - 1-click local SRE demo fallback for development (`/login/demo`).
- **Data Privacy & Zero Secret Leakage**:
  - Strict `.gitignore` policy protecting `.env`, logs, and history data.
  - Works seamlessly inside private repositories.
- **Multi-Channel Alert Dispatcher**:
  - Email (SMTP) supporting Gmail App Passwords, Outlook, SendGrid, Mailgun, and custom SMTP.
  - Discord Webhooks, Telegram Bots, and Slack Incoming Webhooks.
  - Alert throttling & incident cooldown to prevent inbox spam.
- **Automated Keep-Alive Engine**:
  - Built-in GitHub Actions workflow (`.github/workflows/uptrace-keepalive.yml`) runs on a scheduled cron.
  - Headless CLI script (`run_monitor.py`) for custom cron jobs and CI/CD pipelines.

---

## 🚀 Quick Start

### 1. Clone & Setup

```bash
git clone https://github.com/yourusername/uptrace.git
cd uptrace
```

### 2. Install Dependencies

```bash
python -m pip install -r requirements.txt
```

### 3. Configure Environment (`.env`)

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Edit `.env` to configure your settings:

```env
# Flask Session Security Key
SECRET_KEY=uptrace-super-secret-key-change-in-production

# Dashboard Privacy & Authentication (Optional)
REQUIRE_AUTH=false
ALLOWED_GITHUB_USERS=your_github_username
ENABLE_DEMO_LOGIN=true

# GitHub OAuth (Create at: https://github.com/settings/developers)
GITHUB_CLIENT_ID=
GITHUB_CLIENT_SECRET=

# Email Incident Alerts (Optional)
SMTP_ENABLED=true
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your-email@gmail.com
SMTP_PASSWORD=your-16-character-gmail-app-password
ALERT_SENDER_EMAIL=your-email@gmail.com
ALERT_RECEIVER_EMAIL=your-alerts-inbox@gmail.com

# Webhook Alerts (Optional)
DISCORD_WEBHOOK_URL=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Server Port
PORT=5000
```

### 4. Run Uptrace

#### Option A: Interactive Web Dashboard (Midnight SRE Console)

```bash
python app.py
```

Open **`http://localhost:5000`** in your browser.

#### Option B: Headless CLI (Single Run)

```bash
python run_monitor.py --no-commit
```

---

## 🔒 Deploying with 100% Data Privacy

To run Uptrace with **zero public exposure** of your database connection strings, endpoints, and logs:

### 1. Keep Your GitHub Repository Private

1. Make your repository **Private** on GitHub.
2. GitHub Actions will execute automated checks and commit status updates inside your private repo without exposing your endpoints or code.

### 2. Lock Down Your Deployed Web Dashboard

When deploying on Render, Railway, Fly.io, or VPS, set these environment variables:

```env
REQUIRE_AUTH=true
ALLOWED_GITHUB_USERS=your_github_username
ENABLE_DEMO_LOGIN=false
```

- Any visitor to your deployed website URL is redirected to GitHub OAuth.
- Only your whitelisted GitHub account can view or manage the console.

---

## 🤖 24/7 GitHub Actions Automation

To keep your cloud apps awake without running your local PC:

1. Push this repository to your private GitHub account.
2. Go to **Settings > Secrets and variables > Actions** in your repository.
3. Add any desired secrets (e.g. `SMTP_ENABLED`, `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`, `ALERT_RECEIVER_EMAIL`).
4. The workflow in [`.github/workflows/uptrace-keepalive.yml`](.github/workflows/uptrace-keepalive.yml) will execute automatically on schedule, ping all services, update [`STATUS.md`](STATUS.md), and commit to your private repository!

---

## 📁 Project Structure

```text
uptrace/
├── app.py                   # Main Flask app, OAuth 2.0, and Uptrace Engine
├── run_monitor.py           # Headless CLI runner for GitHub Actions & Crons
├── config.json              # Monitored apps, DB targets, methods, and schedules
├── STATUS.md                # Real-time status report with GitHub shields
├── design.md                # Better Stack Midnight SRE design specification
├── .env.example             # Configuration template for OAuth, SMTP & Privacy
├── .gitignore               # Strict secret protection (.env, logs, history)
├── requirements.txt         # Dependencies
├── .github/
│   └── workflows/
│       └── uptrace-keepalive.yml # 24/7 GitHub Actions keep-alive runner
├── data/
│   └── uptime_history.json  # Check history & latency records (git-ignored)
├── logs/
│   └── uptrace.log          # Runtime log file (git-ignored)
├── templates/
│   └── dashboard.html       # Midnight SRE Console UI with SaaS Assets
└── static/
    ├── assets/
    │   ├── favicon.svg      # SVG browser tab favicon
    │   ├── uptrace-icon.svg # Vector emblem mark
    │   ├── uptrace-logo.svg # Full wordmark logo
    │   └── logos/           # Custom partner SVGs (Render, Supabase, etc.)
    ├── css/style.css        # Midnight SRE Console stylesheet
    └── js/dashboard.js      # Interactive controller & poller
```

---

## 🛡️ License

MIT License. Built for developers keeping cloud apps & databases alive with maximum reliability.
