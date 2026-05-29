"""
run_ga_contact.py
-----------------
Auto-finds the best GA contact that has vehicle fields populated in GHL,
prints a full human-readable summary, waits for your confirmation, then
runs NG360BridgeBot end-to-end.

No contact ID needed — the script finds one for you.

Usage
-----
    python run_ga_contact.py              # find + confirm + run bot
    python run_ga_contact.py --dry-run   # find + confirm, do NOT open browser
    python run_ga_contact.py --out enriched.json  # also dump normalised JSON

Environment (.env or exported shell vars)
-----------------------------------------
    GHL_API_KEY      — required (pit-xxx Bearer token)
    GHL_LOCATION_ID  — optional, defaults to Czwg7VWYU6myocqsb86R
    NATGEN_USERNAME  — required for live run
    NATGEN_PASSWORD  — required for live run
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv()

import httpx
from services.ghl_client import (
    GHLError,
    enrich_contact_from_custom_fields,
    get_contact,
    _headers,
)
from ghl_contact_fieldids import (
    FIELD_ID_VEH1_YEAR,
    FIELD_ID_VEH1_MAKE,
    FIELD_ID_VEH2_YEAR,
    FIELD_ID_VEH2_MAKE,
)
from core.bridge_bot import run_bot

GHL_BASE_URL    = "https://services.leadconnectorhq.com"
GHL_LOCATION_ID = os.getenv("GHL_LOCATION_ID", "Czwg7VWYU6myocqsb86R")
REQUEST_TIMEOUT = 30

# Vehicle field IDs we check to confirm a contact is "vehicle-populated"
VEHICLE_SIGNAL_IDS = {FIELD_ID_VEH1_YEAR, FIELD_ID_VEH1_MAKE, FIELD_ID_VEH2_YEAR, FIELD_ID_VEH2_MAKE}

REQUIRED_CONTACT_KEYS: dict[str, tuple[str, ...]] = {
    "firstName":     ("firstName", "first_name"),
    "lastName":      ("lastName", "last_name"),
    "postalCode":    ("postalCode", "zip"),
    "dateOfBirth":   ("dateOfBirth", "date_of_birth"),
    "gender":        ("gender",),
    "maritalStatus": ("maritalStatus", "marital_status"),
    "occupation":    ("occupation",),
    "phone":         ("phone",),
    "address1":      ("address1", "address"),
    "city":          ("city",),
    "email":         ("email",),
}

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _banner(text: str) -> None:
    bar = "─" * 64
    print(f"\n{bar}\n  {text}\n{bar}", flush=True)


def _has_vehicle_fields(contact: dict) -> bool:
    """Return True if at least one vehicle signal field has a non-empty value."""
    for cf in contact.get("customFields", []):
        if cf.get("id") in VEHICLE_SIGNAL_IDS:
            val = cf.get("value")
            if val and str(val).strip():
                return True
    return False


def _missing_required_fields(contact: dict) -> list[str]:
    missing: list[str] = []
    for canonical, aliases in REQUIRED_CONTACT_KEYS.items():
        if not any(str(contact.get(alias, "")).strip() for alias in aliases):
            missing.append(canonical)
    vehicles = contact.get("vehicles")
    if not isinstance(vehicles, list) or not any(isinstance(v, dict) for v in vehicles):
        missing.append("vehicles")
    return missing


# ---------------------------------------------------------------------------
# GHL contact search
# ---------------------------------------------------------------------------

async def _search_ga_contacts_page(
    client: httpx.AsyncClient,
    start_after: str | None = None,
    limit: int = 20,
) -> tuple[list[dict], str | None]:
    """
    Fetch a page of GA contacts via POST /contacts/search (v2).
    Returns (contacts_list, next_start_after_id).

    Falls back to GET /contacts/ (deprecated but reliable with pit- tokens)
    if the POST endpoint returns 4xx.
    """
    # ── Try POST /contacts/search first (current recommended endpoint) ───
    url_post = f"{GHL_BASE_URL}/contacts/search"
    body: dict = {
        "locationId": GHL_LOCATION_ID,
        "filters": [
            {"field": "state", "operator": "eq", "value": "GA"}
        ],
        "page": 1,
        "pageLimit": limit,
        "sort": [{"field": "date_updated", "direction": "desc"}],
    }
    if start_after:
        body["startAfterId"] = start_after

    try:
        resp = await client.post(url_post, headers=_headers(), json=body, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            logger.warning(
                "GHL POST /contacts/search returned %s: %s — falling back to GET /contacts/",
                resp.status_code,
                (resp.text or "").replace("\n", " ")[:400],
            )
        if resp.status_code == 200:
            data = resp.json()
            contacts = (
                data.get("contacts")
                or data.get("data", {}).get("contacts")
                or []
            )
            meta = data.get("meta") or data.get("data", {}).get("meta") or {}
            next_id = meta.get("startAfterId") or meta.get("nextStartAfter") or None
            # If we got an empty contacts list with no error, fall through to GET
            if contacts or next_id is not None:
                return contacts, next_id
    except httpx.RequestError:
        pass  # network error → fall through to GET

    # ── Fallback: GET /contacts/ (deprecated, works with pit- tokens) ────
    # Uses startAfter cursor for pagination (value = last contact id from prev page).
    url_get = f"{GHL_BASE_URL}/contacts/"
    # GET list API only allows sortBy=date_added|date_updated (no sortDirection param).
    params: dict = {
        "locationId": GHL_LOCATION_ID,
        "limit": limit,
        "sortBy": "date_updated",
    }
    if start_after:
        params["startAfter"] = start_after

    try:
        resp = await client.get(url_get, headers=_headers(), params=params, timeout=REQUEST_TIMEOUT)
    except httpx.RequestError as exc:
        raise GHLError(f"Network error listing contacts: {exc}") from exc

    if resp.status_code != 200:
        raise GHLError(f"GHL GET /contacts/ returned {resp.status_code}: {resp.text}")

    data = resp.json()
    contacts = data.get("contacts") or []
    meta = data.get("meta") or {}
    # GET /contacts/ uses startAfter as a numeric offset or last-id cursor
    next_id = meta.get("startAfterId") or meta.get("nextPageUrl") or None
    # For the GET endpoint, if we got a full page there may be more
    if len(contacts) == limit and not next_id:
        # Use last contact id as the cursor for next page
        next_id = contacts[-1].get("id") if contacts else None
    return contacts, next_id


async def find_ga_contact_with_vehicles(max_pages: int = 5) -> dict:
    """
    Pages through GA contacts (newest first) and returns the first one
    whose full record has at least one vehicle signal field populated.
    Raises GHLError if none found after max_pages pages.
    """
    _banner("Searching GHL for GA contact with vehicle fields")

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        start_after: str | None = None
        for page in range(1, max_pages + 1):
            print(f"  Fetching page {page} of GA contacts...", flush=True)
            try:
                candidates, next_cursor = await _search_ga_contacts_page(
                    client, start_after=start_after, limit=20
                )
            except GHLError as exc:
                print(f"\n[ERROR] {exc}", file=sys.stderr)
                sys.exit(1)

            if not candidates:
                print("  No more contacts on this page.", flush=True)
                break

            # Filter to GA only (GET fallback may return mixed states)
            ga_candidates = [
                c for c in candidates
                if str(c.get("state", "")).strip().upper() == "GA"
            ]
            print(
                f"  Got {len(candidates)} contacts, {len(ga_candidates)} are GA "
                f"— checking vehicle fields...",
                flush=True,
            )

            for c in ga_candidates:
                cid   = c.get("id") or c.get("contactId", "")
                fname = c.get("firstName", "")
                lname = c.get("lastName", "")
                if not cid:
                    continue

                # Fetch full contact to inspect customFields
                try:
                    full = await get_contact(cid)
                except GHLError as exc:
                    logger.warning("  Skipping %s %s — fetch error: %s", fname, lname, exc)
                    continue

                if _has_vehicle_fields(full):
                    print(f"  ✓ Found: {fname} {lname}  (id={cid})", flush=True)
                    return full
                else:
                    print(f"    ✗ {fname} {lname} — no vehicle fields, skipping", flush=True)

            if not next_cursor:
                print("  No further pages available.", flush=True)
                break
            start_after = next_cursor

    raise GHLError(
        f"No GA contact with vehicle fields found after searching {max_pages} pages. "
        "Check that vehicle custom fields are populated in GHL."
    )


# ---------------------------------------------------------------------------
# Pretty summary printer
# ---------------------------------------------------------------------------

def _print_summary(contact: dict) -> None:
    _banner(f"Contact Summary — {contact.get('firstName')} {contact.get('lastName')}")

    # Standard top-level fields
    fields_to_show = [
        ("GHL ID",           contact.get("id")),
        ("Name",             f"{contact.get('firstName')} {contact.get('lastName')}"),
        ("Email",            contact.get("email")),
        ("Phone",            contact.get("phone")),
        ("Address",          f"{contact.get('address1')}, {contact.get('city')}, {contact.get('state')} {contact.get('postalCode')}"),
        ("DOB",              contact.get("dateOfBirth")),
        ("Gender",           contact.get("gender")),
        ("Marital Status",   contact.get("maritalStatus")),
        ("Occupation",       contact.get("occupation")),
        ("DL Number",        contact.get("driverLicenseNumber")),
        ("Prior Carrier",    contact.get("prior_carrier_home")),
        ("Years at Address", contact.get("years_at_residence")),
        ("Year Built",       contact.get("year_built")),
        ("Date Purchased",   contact.get("datePurchased")),
        ("Effective Date",   contact.get("effective_date") or "(tomorrow — bot default)"),
        ("Prior Expiration", contact.get("prior_expiration")),
        ("Yrs Continuous Ins", contact.get("years_continuous_ins")),
    ]

    for label, val in fields_to_show:
        if val is not None and str(val).strip():
            print(f"  {label:<24} {val}")

    # Vehicles
    vehicles = contact.get("vehicles", [])
    print(f"\n  Vehicles ({len(vehicles)} total):")
    if not vehicles:
        print("    (none — bot will use defaults)")
    for i, v in enumerate(vehicles, 1):
        yr  = v.get("year", "?")
        mk  = v.get("make", "?")
        md  = v.get("model", "?")
        sub = v.get("submodel") or ""
        vin = v.get("vin_prefix") or "?"
        own = v.get("ownership_status", "?")
        mi  = v.get("annual_mileage", "?")
        pd  = v.get("purchase_date", "?")
        print(f"    [{i}] {yr} {mk} {md} {sub}".rstrip())
        print(f"        VIN prefix={vin}  ownership={own}  annual_mi={mi}  purchase={pd}")

    # Missing fields warning
    missing = _missing_required_fields(contact)
    if missing:
        print(f"\n  ⚠  Missing required fields: {', '.join(missing)}")
        print("     The bot will FAIL at the worker validation step.")
    else:
        print("\n  ✓  All required fields present")

    print(f"\n  Tags: {contact.get('tags', [])}")
    print(f"  customFields: {len(contact.get('customFields', []))} entries in GHL payload")


# ---------------------------------------------------------------------------
# Core flow
# ---------------------------------------------------------------------------

async def run(dry_run: bool, out_path: str | None) -> None:

    # 1. Find a good contact
    try:
        raw = await find_ga_contact_with_vehicles()
    except GHLError as exc:
        print(f"\n[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)

    # 2. Enrich — same call worker.py makes
    _banner("Enriching contact")
    contact = enrich_contact_from_custom_fields(raw)
    print(f"  Vehicles after enrichment: {len(contact.get('vehicles', []))}", flush=True)

    # 3. Print full summary
    _print_summary(contact)

    # 4. Optionally dump JSON
    if out_path:
        Path(out_path).write_text(
            json.dumps(contact, indent=2, default=str), encoding="utf-8"
        )
        print(f"\n  Normalised JSON written to: {out_path}")

    # 5. Abort early on missing required fields
    missing = _missing_required_fields(contact)
    if missing:
        print(
            f"\n[ERROR] Cannot run bot — missing required fields: {', '.join(missing)}\n"
            "        Fill them in GHL and try again.",
            file=sys.stderr,
        )
        sys.exit(1)

    if dry_run:
        _banner("Dry-run — browser NOT launched")
        return

    # 6. Pause for confirmation
    _banner("Ready to launch NG360BridgeBot")
    print(
        "  This will open a real Chromium browser and run the full 14-step\n"
        "  National General PKGProtect2 quote flow against the portal.\n"
    )
    try:
        input("  Press Enter to continue, or Ctrl-C to abort... ")
    except KeyboardInterrupt:
        print("\n\n  Aborted by user.")
        sys.exit(0)

    # 7. Credential guard
    missing_creds = [
        v for v in ("NATGEN_USERNAME", "NATGEN_PASSWORD")
        if not os.getenv(v, "").strip()
    ]
    if missing_creds:
        print(
            f"\n[ERROR] Missing env vars: {', '.join(missing_creds)}\n"
            "        Add them to .env or export before running.",
            file=sys.stderr,
        )
        sys.exit(1)

    # 8. Run the bot
    _banner("Launching NG360BridgeBot")
    try:
        result = await asyncio.wait_for(run_bot(contact), timeout=600.0)
    except asyncio.TimeoutError:
        print("\n[ERROR] Watchdog timeout — quote took > 600 s", file=sys.stderr)
        sys.exit(1)

    # 9. Print result
    _banner("Result")
    print(json.dumps(result, indent=2, default=str))
    if result.get("success"):
        print("\n✓  PASS — quote completed successfully")
    else:
        print(f"\n✗  FAIL — {result.get('error', 'unknown error')}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Auto-find a GA+vehicle contact in GHL, enrich it, confirm, run bridge_bot.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Find, enrich and print summary — do NOT open the browser",
    )
    parser.add_argument(
        "--out",
        metavar="FILE",
        default=None,
        help="Also write the normalised contact JSON to FILE",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",         # quiet by default — important prints go to stdout directly
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    if not os.getenv("GHL_API_KEY", "").strip():
        print(
            "[ERROR] GHL_API_KEY is not set.\n"
            "        Add it to .env or run:  export GHL_API_KEY=pit-xxx",
            file=sys.stderr,
        )
        sys.exit(1)

    asyncio.run(run(dry_run=args.dry_run, out_path=args.out))


if __name__ == "__main__":
    main()