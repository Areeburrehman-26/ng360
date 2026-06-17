#!/usr/bin/env python3
"""
fetch_failed_contacts_from_ghl.py
----------------------------------
Query GHL API for contacts with failed NG360 quote status
and add them back to the ng360_queue.json for reprocessing.

Looks for GA-state contacts where:
  - fire_quote_status   (FIELD_ID_QUOTE_STATUS)     = "failed"
  - auto_quote_status   (FIELD_ID_AUTO_QUOTE_STATUS) = "failed"

Usage:
    cd /Users/desmondthomas/Desktop/all-in-one/nsg360_bot
    python3 fetch_failed_contacts_from_ghl.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone
import uuid

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
import httpx

load_dotenv()

from ghl_contact_fieldids import FIELD_ID_QUOTE_STATUS, FIELD_ID_AUTO_QUOTE_STATUS

GHL_API_KEY     = os.getenv("GHL_API_KEY", "")
GHL_LOCATION_ID = os.getenv("GHL_LOCATION_ID", "")
GHL_BASE_URL    = "https://services.leadconnectorhq.com"
QUEUE_FILE      = Path(__file__).parent / "data" / "ng360_queue.json"

# Only re-queue GA state contacts — NSG360 handles GA only.
ELIGIBLE_STATES = {"GA"}

# Field IDs to check for "failed" status
FAILED_STATUS_FIELD_IDS = {FIELD_ID_QUOTE_STATUS, FIELD_ID_AUTO_QUOTE_STATUS}


async def get_failed_contacts():
    """Fetch all GA contacts with a failed NG360 quote status from GHL."""
    url = f"{GHL_BASE_URL}/contacts/"
    headers = {
        "Authorization": f"Bearer {GHL_API_KEY}",
        "Content-Type": "application/json",
        "Version": "2021-07-28",
    }
    params = {
        "locationId": GHL_LOCATION_ID,
        "limit": 100,
    }

    all_contacts = []
    print("Fetching contacts from GHL (looking for failed NG360 quotes in GA)...")

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                response = await client.get(url, headers=headers, params=params)
                response.raise_for_status()

                data     = response.json()
                contacts = data.get("contacts", [])

                if not contacts:
                    break

                for contact in contacts:
                    # Only GA state contacts
                    state = (contact.get("state") or "").strip().upper()
                    if state not in ELIGIBLE_STATES:
                        continue

                    # Check for "failed" in either quote status field
                    custom_fields = contact.get("customFields", [])
                    has_failure = any(
                        cf.get("id") in FAILED_STATUS_FIELD_IDS and cf.get("value") == "failed"
                        for cf in custom_fields
                    )
                    if has_failure:
                        all_contacts.append(contact)

                fetched_so_far = len(all_contacts)
                print(f"  Fetched {len(contacts)} contacts ({fetched_so_far} with failed NG360 status so far)...")

                next_page = data.get("meta", {}).get("nextPageUrl")
                if not next_page:
                    break

                url    = next_page
                params = {}  # nextPageUrl already includes all params

            except Exception as e:
                print(f"❌ Error fetching contacts: {e}")
                break

    return all_contacts


async def add_to_queue(contacts: list):
    """Add failed contacts to the NG360 queue."""
    if not contacts:
        print("No failed GA contacts to add.")
        return

    print(f"\nFound {len(contacts)} contacts with failed NG360 quote status (state=GA)")

    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)

    if QUEUE_FILE.exists():
        with open(QUEUE_FILE) as f:
            queue = json.load(f)
    else:
        queue = []

    existing_ids = {job.get("contact_id") for job in queue}

    added = 0
    for contact in contacts:
        contact_id = contact.get("id", "")
        first_name = contact.get("firstName", "")
        last_name  = contact.get("lastName", "")
        state      = (contact.get("state") or "").strip().upper()

        if contact_id in existing_ids:
            print(f"  ⏭ {first_name} {last_name} — already in queue, skipping")
            continue

        job = {
            "job_id":     str(uuid.uuid4()),
            "contact_id": contact_id,
            "priority":   1,
            "status":     "PENDING",
            "attempts":   0,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "error":      None,
            "first_name": first_name,
            "last_name":  last_name,
            "state":      state,
        }

        queue.append(job)
        existing_ids.add(contact_id)
        added += 1
        print(f"  ✓ {first_name} {last_name} ({state})")

    with open(QUEUE_FILE, "w") as f:
        json.dump(queue, f, indent=2)

    print(f"\n✅ Added {added} failed contacts to ng360_queue.json")
    print(f"   Queue file: {QUEUE_FILE}")
    print("\nThe NG360 worker will process these jobs automatically.")


async def main():
    print("=" * 70)
    print("FETCH FAILED NG360 CONTACTS FROM GHL")
    print("=" * 70)
    print()

    if not GHL_API_KEY:
        print("❌ GHL_API_KEY is not set in .env")
        sys.exit(1)
    if not GHL_LOCATION_ID:
        print("❌ GHL_LOCATION_ID is not set in .env")
        sys.exit(1)

    contacts = await get_failed_contacts()
    await add_to_queue(contacts)

    print("\n" + "=" * 70)
    print("DONE")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
