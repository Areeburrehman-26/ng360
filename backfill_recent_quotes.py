#!/usr/bin/env python3
"""
backfill_recent_quotes.py
-------------------------
Backfill National General quote price and PDF URL for recent completed NG360 quotes.

This script updates contacts that were quoted before a field-mapping fix.
Add the relevant contact IDs to RECENT_CONTACTS before running.

Usage:
    cd /Users/desmondthomas/Desktop/all-in-one/nsg360_bot
    python3 backfill_recent_quotes.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from services.ghl_client import (
    get_contact,
    update_contact_fields,
    get_custom_field_by_id,
)
from ghl_contact_fieldids import (
    FIELD_ID_NG_QUOTE_PRICE,
    FIELD_ID_NG_QUOTE_PDF,
    FIELD_ID_AUTO_QUOTE_STATUS,
    FIELD_ID_QUOTE_STATUS,
)


# ---------------------------------------------------------------------------
# Fill in the GHL contact IDs you want to backfill.
# Leave empty to run a dry-run against zero contacts.
# ---------------------------------------------------------------------------

RECENT_CONTACTS: list[str] = [
    # "abc123",  # John Doe
    # "xyz789",  # Jane Smith
]

CARRIER_NAME = "National General"


async def backfill_contact(contact_id: str) -> None:
    """Inspect and optionally fix one NG360 contact's quote fields."""
    try:
        contact    = await get_contact(contact_id)
        first_name = contact.get("firstName", "")
        last_name  = contact.get("lastName", "")

        print(f"\n📋 Processing: {first_name} {last_name} ({contact_id})")

        ng_price       = get_custom_field_by_id(contact, FIELD_ID_NG_QUOTE_PRICE)
        ng_pdf         = get_custom_field_by_id(contact, FIELD_ID_NG_QUOTE_PDF)
        quote_status   = get_custom_field_by_id(contact, FIELD_ID_QUOTE_STATUS)
        auto_status    = get_custom_field_by_id(contact, FIELD_ID_AUTO_QUOTE_STATUS)

        print(f"   NG Quote Price:  {ng_price  or '(empty)'}")
        print(f"   NG Quote PDF:    {ng_pdf    or '(empty)'}")
        print(f"   Quote Status:    {quote_status  or '(empty)'}")
        print(f"   Auto Status:     {auto_status   or '(empty)'}")

        updates: dict[str, str] = {}

        # If auto_quote_status is missing but quote_status shows success, mirror it
        if not auto_status and quote_status in ("completed", "success"):
            updates[FIELD_ID_AUTO_QUOTE_STATUS] = quote_status
            print(f"   ✅ Will set auto_quote_status → {quote_status}")

        if not ng_price:
            print(f"   ⚠️  NG Quote Price is empty — cannot backfill (not stored in queue data)")
        else:
            print(f"   ✓  NG Quote Price already set")

        if not ng_pdf:
            print(f"   ⚠️  NG Quote PDF is empty — cannot backfill (not stored in queue data)")
        else:
            print(f"   ✓  NG Quote PDF already set")

        if updates:
            await update_contact_fields(contact_id, updates)
            print(f"   ✅ Updated successfully!")
        else:
            print(f"   ✓  No updates needed")

    except Exception as e:
        print(f"   ❌ Error: {e}")


async def main():
    print("=" * 70)
    print(f"BACKFILL RECENT NG360 QUOTES — Carrier: {CARRIER_NAME}")
    print("=" * 70)

    if not RECENT_CONTACTS:
        print("\n⚠️  RECENT_CONTACTS list is empty.")
        print("   Add contact IDs to RECENT_CONTACTS in this script and re-run.")
        return

    print(f"\nProcessing {len(RECENT_CONTACTS)} contacts...")

    for contact_id in RECENT_CONTACTS:
        await backfill_contact(contact_id)

    print("\n" + "=" * 70)
    print("✅ BACKFILL COMPLETE")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
