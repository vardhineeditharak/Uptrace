#!/usr/bin/env python3
"""
Uptrace Headless Monitor Runner
Designed for GitHub Actions, Cron jobs, and CLI execution.
Pings web apps and databases, triggers email incident alerts, and commits to GitHub.
"""

import sys
import argparse
from app import UptraceEngine, logger

def main():
    parser = argparse.ArgumentParser(description="Uptrace Automated Monitor & Keep-Alive Runner")
    parser.add_argument('--commit', action='store_true', help="Force git auto-commit and push")
    parser.add_argument('--no-commit', action='store_true', help="Disable git auto-commit")
    parser.add_argument('--weekly-report', action='store_true', help="Generate and email the 7-day Weekly SRE Reliability Report")
    parser.add_argument('--send-weekly-report-only', action='store_true', help="Email weekly report from existing history without executing new checks")
    parser.add_argument('--test-alert', action='store_true', help="Dispatch a test incident email alert to verify SMTP delivery")
    parser.add_argument('--recipient', type=str, default=None, help="Custom recipient email address for report or alert")
    args = parser.parse_args()

    engine = UptraceEngine()

    if args.test_alert:
        logger.info("🧪 Sending simulated test incident alert...")
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
            'error': '503 Service Unavailable (Test Incident Alert verification)'
        }
        ok = engine.send_email_alert(sample_result, is_recovery=False)
        if ok:
            logger.info("✅ Test alert successfully dispatched!")
            sys.exit(0)
        else:
            logger.error("❌ Test alert failed. Please verify SMTP_ENABLED and credentials in .env")
            sys.exit(1)

    if args.send_weekly_report_only:
        logger.info("📊 Generating and dispatching Weekly SRE Report from existing history...")
        res = engine.send_weekly_report_email(recipient=args.recipient)
        logger.info(f"✨ Weekly Report result: {res.get('status')} - {res.get('message', res.get('recipient', ''))}")
        sys.exit(0 if res.get('status') == 'success' else 1)

    logger.info("=" * 60)
    logger.info("⚡ Starting Uptrace Headless Monitor Run")
    logger.info("=" * 60)

    results = engine.check_all()

    total = len(results)
    healthy = len([r for r in results if r['status'] == 'healthy'])
    unhealthy = total - healthy

    logger.info(f"📊 Run Complete: {healthy}/{total} services operational ({unhealthy} issues).")

    if args.weekly_report:
        logger.info("📊 Dispatching Weekly SRE Performance & Reliability Report...")
        rep_res = engine.send_weekly_report_email(recipient=args.recipient)
        if rep_res.get('status') == 'success':
            logger.info(f"✅ Weekly report emailed to {rep_res.get('recipient')}")
        else:
            logger.warning(f"ℹ️ Weekly report status: {rep_res.get('message') or rep_res.get('error')}")

    if args.commit:
        import os
        os.environ['ENABLE_GIT_AUTO_COMMIT'] = 'true'
        engine.commit_and_push_to_git()
    elif not args.no_commit:
        engine.commit_and_push_to_git()

    logger.info("✨ Uptrace Run Finished.")
    if unhealthy > 0:
        sys.exit(1)
    sys.exit(0)

if __name__ == '__main__':
    main()
