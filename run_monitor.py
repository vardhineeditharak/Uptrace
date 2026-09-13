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
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("⚡ Starting Uptrace Headless Monitor Run")
    logger.info("=" * 60)

    engine = UptraceEngine()
    results = engine.check_all()

    total = len(results)
    healthy = len([r for r in results if r['status'] == 'healthy'])
    unhealthy = total - healthy

    logger.info(f"📊 Run Complete: {healthy}/{total} services operational ({unhealthy} issues).")

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
