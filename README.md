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
- **Multi-Channel Alert Dispatcher & Weekly Reports**:
  - Immediate email incident alerts (`🚨 [INCIDENT]` & `✅ [RESOLVED]`) supporting Gmail App Passwords, Outlook, AWS SES, and custom SMTP.
  - Automated **Weekly SRE Performance & SLA Reports** summarizing 7-day availability, average latency, and incident logs.
  - Discord Webhooks, Telegram Bots, and Slack Incoming Webhooks.
  - Alert throttling & incident cooldown to prevent inbox spam.
- **Automated Keep-Alive & Weekly Digest Engine**:
  - Keep-Alive workflow ([`.github/workflows/uptrace-keepalive.yml`](.github/workflows/uptrace-keepalive.yml)) runs every 12 hours.
  - Weekly Report workflow ([`.github/workflows/uptrace-weekly-report.yml`](.github/workflows/uptrace-weekly-report.yml)) emails 7-day reliability digests every Monday.
  - Headless CLI script (`run_monitor.py`) for custom cron jobs, weekly reports (`--weekly-report`), and test alerts (`--test-alert`).

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

## 🌐 Render Deployment (Web Dashboard & API)

To host the interactive **Midnight SRE Web Console** on Render with zero secret exposure:

1. Create a new **Web Service** on [Render Dashboard](https://dashboard.render.com).
2. Connect your GitHub repository (`vardhineeditharak/Uptrace`).
3. Set **Runtime**: `Python 3`, **Build Command**: `pip install -r requirements.txt`, **Start Command**: `python app.py`.
4. In the **Environment** tab on Render, add these variables:

| Variable | Value | Purpose |
| :--- | :--- | :--- |
| `SECRET_KEY` | `uptrace-super-secret-key-prod` | Encrypts user session cookies |
| `REQUIRE_AUTH` | `true` | Locks dashboard from public access |
| `ADMIN_PASSWORD` | `your-master-admin-password` | 1-click password login without OAuth |
| `ENABLE_DEMO_LOGIN` | `false` | Disables demo bypass in production |
| `GITHUB_CLIENT_ID` | `Iv1.xxxxxxxxxxxx` *(Optional)* | GitHub OAuth App Client ID |
| `GITHUB_CLIENT_SECRET` | `xxxxxxxxxxxxxxxx` *(Optional)* | GitHub OAuth App Secret |
| `ALLOWED_GITHUB_USERS` | `your_github_username` | Whitelisted GitHub accounts |
| `SMTP_ENABLED` | `true` | Enables incident email alerts |
| `SMTP_HOST` | `smtp.gmail.com` | SMTP host |
| `SMTP_PORT` | `587` | SMTP port (587 for TLS, 465 for SSL) |
| `SMTP_USER` | `your-email@gmail.com` | Sender account |
| `SMTP_PASSWORD` | `your-16-char-app-password` | Gmail App Password |
| `ALERT_SENDER_EMAIL` | `your-email@gmail.com` | From email address |
| `ALERT_RECEIVER_EMAIL` | `your-alerts-inbox@gmail.com` | To email address (incident recipient) |
| `WEEKLY_REPORT_ENABLED` | `true` | Enables weekly SRE digest emails |
| `WEEKLY_REPORT_RECIPIENT`| `your-alerts-inbox@gmail.com` | Recipient for weekly reports |
| `ENABLE_GIT_AUTO_COMMIT` | `true` | Pushes live config edits to GitHub |
| `GITHUB_TOKEN` | `ghp_xxxxxxxxxxxxxxxxxxxx` | GitHub PAT for REST API repo sync |
| `GITHUB_REPOSITORY` | `vardhineeditharak/Uptrace` | GitHub repository name |
| `TIMEZONE` | `Asia/Kolkata` | Local time zone |
| `TIMEZONE_CODE` | `IST` | Time zone abbreviation |

---

## 🤖 GitHub Actions Secrets (Automated 12h Keep-Alive & Weekly Reports)

To keep your cloud apps awake and receive weekly reports automatically without running your local PC or keeping Render awake:

1. In your GitHub repository, navigate to: **Settings > Secrets and variables > Actions**.
2. Click **New repository secret** and add these secrets:

### Required Secrets for GitHub Actions:
| GitHub Secret Name | Recommended Value | Description |
| :--- | :--- | :--- |
| `SMTP_ENABLED` | `true` | Enables email sending inside GitHub Actions |
| `SMTP_HOST` | `smtp.gmail.com` | SMTP Server (Gmail, SendGrid, Outlook, SES) |
| `SMTP_PORT` | `587` | SMTP Port (`587` for STARTTLS, `465` for SSL) |
| `SMTP_USER` | `your-email@gmail.com` | Your SMTP username / email address |
| `SMTP_PASSWORD` | `your-16-char-app-password` | 16-character Google App Password |
| `ALERT_SENDER_EMAIL` | `your-email@gmail.com` | Sender email address |
| `ALERT_RECEIVER_EMAIL` | `your-alerts-inbox@gmail.com`| Where incident alerts & weekly reports go |

### Optional Secrets:
| GitHub Secret Name | Recommended Value | Description |
| :--- | :--- | :--- |
| `WEEKLY_REPORT_RECIPIENT` | `your-alerts-inbox@gmail.com` | Dedicated weekly report recipient inbox |
| `GIT_AUTHOR_EMAIL` | `vardhineedi.tharak@gmail.com` | Email for automated git status commits |
| `DISCORD_WEBHOOK_URL` | `https://discord.com/api/...` | Discord incident notification webhook |
| `TELEGRAM_BOT_TOKEN` | `123456789:ABCdef...` | Telegram bot API token |
| `TELEGRAM_CHAT_ID` | `987654321` | Telegram chat ID for incident alerts |
| `SLACK_WEBHOOK_URL` | `https://hooks.slack.com/...`| Slack incident alert webhook |

### Workflows Triggered:
- [`.github/workflows/uptrace-keepalive.yml`](.github/workflows/uptrace-keepalive.yml): Runs **every 12 hours** to ping all services, keep cloud apps/databases warm, and trigger immediate email alerts if any monitor fails.
- [`.github/workflows/uptrace-weekly-report.yml`](.github/workflows/uptrace-weekly-report.yml): Runs **every Monday at 09:00 AM IST** to compile 7-day SLA performance metrics and dispatch the HTML weekly digest email.

---

## 📁 Project Structure

```text
uptrace/
├── app.py                   # Main Flask app, OAuth 2.0, Weekly Digest & Uptrace Engine
├── run_monitor.py           # Headless CLI runner for GitHub Actions, Reports & Alerts
├── config.json              # Monitored apps, DB targets, methods, and schedules
├── STATUS.md                # Real-time status report with GitHub shields
├── design.md                # Better Stack Midnight SRE design specification
├── .env.example             # Configuration template for OAuth, SMTP & Privacy
├── .gitignore               # Strict secret protection (.env, logs, history)
├── requirements.txt         # Dependencies
├── .github/
│   └── workflows/
│       ├── uptrace-keepalive.yml     # 12-hour GitHub Actions keep-alive & incident runner
│       └── uptrace-weekly-report.yml # Weekly SRE report & email digest workflow
├── data/
│   ├── uptime_history.json  # Check history & latency records (git-ignored)
│   ├── incident_state.json  # Active incident state tracker (git-ignored)
│   └── incidents.json       # Historical incident & resolution logs (git-ignored)
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
