"""
poller.py
---------
Background poller that discovers GA contacts from GHL and enqueues them at MEDIUM priority.
"""

import asyncio
import logging
import os

from dotenv import load_dotenv

from core.logging_setup import configure_logging
from core.queue_manager import Priority, queue_manager
from services.ghl_client import (
    GHLError,
    enrich_contact_from_custom_fields,
    find_eligible_ga_contacts,
)
from utils.contact_defaults import contact_has_vehicles

load_dotenv()
logger = logging.getLogger(__name__)

POLLER_INTERVAL_S = int(os.getenv("POLLER_INTERVAL_S", "300"))
POLLER_MAX_PAGES = int(os.getenv("POLLER_MAX_PAGES", "5"))


async def run_poller() -> None:
    logger.info("[poller] Poller started — interval=%ss max_pages=%s", POLLER_INTERVAL_S, POLLER_MAX_PAGES)
    while True:
        try:
            contacts = await find_eligible_ga_contacts(max_pages=POLLER_MAX_PAGES)
            enqueued = 0
            for contact in contacts:
                contact_id = str(contact.get("id") or "").strip()
                if not contact_id:
                    continue
                if queue_manager.is_contact_already_queued(contact_id):
                    continue

                normalized = enrich_contact_from_custom_fields(contact)
                if not contact_has_vehicles(normalized):
                    continue

                await queue_manager.enqueue(
                    contact_id=contact_id,
                    priority=Priority.MEDIUM,
                    first_name=str(normalized.get("firstName") or "").strip(),
                    last_name=str(normalized.get("lastName") or "").strip(),
                    state=str(normalized.get("state") or "GA").strip().upper(),
                )
                enqueued += 1

            logger.info("[poller] Scan complete — candidates=%s enqueued=%s", len(contacts), enqueued)
        except GHLError as exc:
            logger.warning("[poller] GHL scan failed: %s", exc)
        except Exception as exc:
            logger.exception("[poller] Unexpected poller failure: %s", exc)

        await asyncio.sleep(POLLER_INTERVAL_S)


if __name__ == "__main__":
    configure_logging("poller")
    asyncio.run(run_poller())
