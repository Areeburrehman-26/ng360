# Emergency queue reset script
"""
force_requeue_pending.py
------------------------
Emergency script to reset any PROCESSING jobs back to PENDING.

USE WHEN:
  - The bot crashed mid-job and quote_queue.json shows stuck PROCESSING jobs
  - curl /queue-status shows is_processing=true but no browser is running
  - Webhook server was force-killed while a job was running

SAFE TO RUN:
  - While the webhook server is stopped
  - While no Chrome browser is running for HOA Bot
  - Never run while a quote is actively being processed — risk of corruption

HOW TO USE:
  1. Stop all processing:
       pkill -f ghl_webhook_server
       pkill -f chrome
  2. Run this script:
       python scripts/force_requeue_pending.py
  3. Restart the bot:
       ./start_bot.sh
  4. Verify recovery:
       curl http://localhost:8002/queue-status
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Default queue file location — adjust if QUEUE_FILE_PATH is set differently in .env
QUEUE_FILE = Path("data/ng360_queue.json")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def force_requeue_pending(queue_file: Path = QUEUE_FILE) -> None:
    if not queue_file.exists():
        print(f"Queue file not found: {queue_file}")
        print("Nothing to reset.")
        return

    with open(queue_file) as f:
        try:
            jobs = json.load(f)
        except json.JSONDecodeError as exc:
            print(f"ERROR: Could not parse queue file: {exc}")
            sys.exit(1)

    reset_count = 0
    for job in jobs:
        if job.get("status") == "PROCESSING":
            job["status"]     = "PENDING"
            job["updated_at"] = _now_iso()
            reset_count += 1
            print(
                f"  Reset: job_id={job['job_id']} "
                f"contact={job.get('contact_id')} "
                f"({job.get('first_name')} {job.get('last_name')})"
            )

    if reset_count == 0:
        print("No PROCESSING jobs found — queue is clean.")
        return

    # Backup before writing
    backup_path = queue_file.parent / f"quote_queue_backup_{_now_iso().replace(':', '-')}.json"
    backup_path.write_text(queue_file.read_text())
    print(f"\nBackup saved to: {backup_path}")

    with open(queue_file, "w") as f:
        json.dump(jobs, f, indent=2)

    print(f"\nDone — reset {reset_count} job(s) from PROCESSING → PENDING.")
    print("Restart the bot with ./start_bot.sh to process them.")


if __name__ == "__main__":
    print("=" * 50)
    print("HOA Bot — Force Requeue PROCESSING Jobs")
    print("=" * 50)
    print()
    force_requeue_pending()