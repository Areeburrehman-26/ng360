"""
worker.py
---------
Queue worker — polls ng360_queue.json every few seconds and calls
run_bot() for each pending job.

This is the missing piece between the webhook server and the browser bot.

Flow:
  1. Load queue from disk on startup
  2. Every POLL_INTERVAL_S seconds, check for the next PENDING job
    3. If found → fetch full contact from GHL → call run_bot()
  4. On success → mark job COMPLETED
  5. On failure → mark job FAILED (queue_manager retries up to 3x at LOW priority)
  6. Repeat forever

Only one job runs at a time — sequential processing by design.
Parallel processing is a P3 roadmap item (Q3 2026).
"""

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from core.queue_manager import queue_manager, JobStatus
from core.bridge_bot import run_bot
from core.logging_setup import configure_logging
from services.drive_uploader import upload_quote_pdf
from services.ghl_client import (
    GHLError,
    enrich_contact_from_custom_fields,
    get_contact,
    record_processing_started,
    record_failed_quote,
    record_successful_quote,
)
from services.notifier import notify_quote_failure, notify_quote_success
from utils.contact_defaults import contact_has_vehicles

load_dotenv()
logger = logging.getLogger(__name__)

POLL_INTERVAL_S = int(os.getenv("WORKER_POLL_INTERVAL_S", 5))


async def _fail_job(job, error_msg: str, *, missing_data: bool = False) -> None:
    """Write a consistent failure path across queue, GHL, and Slack."""
    await queue_manager.mark_failed(job.job_id, error=error_msg)
    try:
        await record_failed_quote(job.contact_id, reason=error_msg, missing_data=missing_data)
    except Exception as exc:
        logger.warning("[worker] Non-fatal: could not write failure to GHL: %s", exc)

    try:
        await notify_quote_failure(
            first_name=job.first_name,
            last_name=job.last_name,
            state=job.state,
            contact_id=job.contact_id,
            reason=str(error_msg),
        )
    except Exception as exc:
        logger.warning("[worker] Slack failure notify skipped/non-fatal: %s", exc)


async def run_worker():
    """
    Main worker loop. Runs forever until the process is killed.
    Called by start_bot.sh alongside the webhook server.
    """
    await queue_manager.load_and_recover()
    logger.info("[worker] Worker started — polling every %ds", POLL_INTERVAL_S)

    while True:
        try:
            job = await queue_manager.get_next_pending()

            if not job:
                # Nothing in queue — wait and check again
                await asyncio.sleep(POLL_INTERVAL_S)
                continue

            logger.info(
                "[worker] Picked up job %s for %s %s (%s)",
                job.job_id, job.first_name, job.last_name, job.state,
            )
            print(
                f"\n[worker] ── Starting job {job.job_id} ──────────────────────\n"
                f"         Contact : {job.first_name} {job.last_name}\n"
                f"         State   : {job.state}\n"
                f"         GHL ID  : {job.contact_id}\n",
                flush=True,
            )

            await queue_manager.mark_processing(job.job_id)
            try:
                await record_processing_started(job.contact_id)
            except Exception as exc:
                logger.warning("[worker] Non-fatal: could not apply processing tag: %s", exc)

            # Fetch full contact data from GHL
            try:
                contact = enrich_contact_from_custom_fields(await get_contact(job.contact_id))
                if job.state:
                    contact["state"] = job.state.strip().upper()
            except GHLError as exc:
                error_msg = f"GHL fetch failed: {exc}"
                logger.error("[worker] GHL fetch failed for job %s: %s", job.job_id, exc)
                await _fail_job(job, error_msg)
                await asyncio.sleep(POLL_INTERVAL_S)
                continue

            # Backup defaults for driver/address fields; vehicles must be real GHL data.
            if not contact_has_vehicles(contact):
                error_msg = (
                    "Missing required vehicle data — at least one vehicle with "
                    "year/make/model (or VIN) is required"
                )
                await _fail_job(job, error_msg, missing_data=True)
                await asyncio.sleep(POLL_INTERVAL_S)
                continue

            # Run the 14-page National General automation with a 1800s (30-minute) watchdog timeout
            try:
                result = await asyncio.wait_for(run_bot(contact), timeout=1800.0)
            except asyncio.TimeoutError:
                error_msg = "Watchdog Timeout: The quote took longer than 1800 seconds and was killed."
                logger.error("[worker] Job %s failed: %s", job.job_id, error_msg)
                await _fail_job(job, error_msg)
                await asyncio.sleep(POLL_INTERVAL_S)
                continue
            except Exception as exc:
                logger.exception("[worker] run_bot crashed for job %s", job.job_id)
                await _fail_job(job, str(exc))
                await asyncio.sleep(POLL_INTERVAL_S)
                continue

            # Handle result
            if result.get("success"):
                total_premium = str(result.get("total_premium") or result.get("premium") or "$0.00")
                home_premium = str(result.get("home_premium") or "$0.00")
                auto_premium = str(result.get("auto_premium") or "$0.00")
                local_pdf_path = str(result.get("pdf_path") or "")

                if not local_pdf_path or not Path(local_pdf_path).exists():
                    error_msg = "Quote reported success but no local PDF artifact was found"
                    await _fail_job(job, error_msg)
                    await asyncio.sleep(POLL_INTERVAL_S)
                    continue

                # Optional integration: Drive upload should not block core processing.
                drive_url = ""
                try:
                    drive_url = upload_quote_pdf(local_pdf_path, job.first_name, job.last_name)
                except Exception as exc:
                    logger.warning("[worker] Drive upload skipped/non-fatal for job %s: %s", job.job_id, exc)

                # Required integration: write results to GHL.
                try:
                    await record_successful_quote(
                        contact_id=job.contact_id,
                        total_premium=total_premium,
                        home_premium=home_premium,
                        auto_premium=auto_premium,
                        drive_url=drive_url,
                        pay_plan=str(result.get("pay_plan") or ""),
                    )
                except Exception as exc:
                    error_msg = f"GHL update failed after quote completion: {exc}"
                    logger.error("[worker] %s", error_msg)
                    await _fail_job(job, error_msg)
                    await asyncio.sleep(POLL_INTERVAL_S)
                    continue

                await queue_manager.mark_completed(job.job_id)
                logger.info(
                    "[worker] Job %s COMPLETED — premium: %s",
                    job.job_id, total_premium,
                )

                # Optional integration: Slack success notification should not block completion.
                try:
                    await notify_quote_success(
                        first_name=job.first_name,
                        last_name=job.last_name,
                        state=job.state,
                        total_premium=total_premium,
                        home_premium=home_premium,
                        auto_premium=auto_premium,
                        drive_url=drive_url or local_pdf_path,
                        contact_id=job.contact_id,
                    )
                except Exception as exc:
                    logger.warning("[worker] Slack success notify skipped/non-fatal: %s", exc)

                print(
                    f"\n[worker] ✓ Job {job.job_id} COMPLETED\n"
                    f"         Total   : {total_premium}\n"
                    f"         Home    : {home_premium}\n"
                    f"         Auto    : {auto_premium}\n"
                    f"         PDF     : {local_pdf_path}\n"
                    f"         Drive   : {drive_url or 'not uploaded'}\n",
                    flush=True,
                )
            else:
                error = result.get("error", "Unknown error")
                is_missing_data = "Missing required contact field" in str(error)
                await _fail_job(job, str(error), missing_data=is_missing_data)

                logger.warning(
                    "[worker] Job %s FAILED at step %s: %s",
                    job.job_id, result.get("step"), error,
                )
                print(
                    f"\n[worker] ✗ Job {job.job_id} FAILED\n"
                    f"         Step    : {result.get('step')}\n"
                    f"         Reason  : {error}\n",
                    flush=True,
                )

        except Exception as exc:
            # Catch-all so the worker never dies from an unexpected error
            logger.exception("[worker] Unexpected error in worker loop: %s", exc)

        await asyncio.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    configure_logging("worker")
    asyncio.run(run_worker())