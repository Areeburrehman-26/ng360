
"""
Fetch eligible GHL contacts for NG360 smoke testing.

Criteria (client-side):
- State == GA
- not_eligible custom field is empty
- Fire Price (existing quote) custom field is empty
- Required fields present
- At least one vehicle can be built from custom fields

Outputs a JSON file with normalized contacts ready to paste into the smoke test.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

GHL_API_KEY = os.getenv("GHL_API_KEY", "").strip()
GHL_BASE_URL = os.getenv("GHL_BASE_URL", "https://services.leadconnectorhq.com").strip()
GHL_LOCATION_ID = os.getenv("GHL_LOCATION_ID", "").strip()

# Shared field IDs from services/ghl_client.py
FIELD_ID_PRICE = "FbUGPnB3rSRHDU52RV2d"        # Fire Price
FIELD_ID_NOT_ELIGIBLE = "Ni7UAcQDhxsWG6OwnBdh"  # not_eligible

DEFAULT_OUTPUT = "data/ghl_contacts_eligible.json"
DEFAULT_PAGE_SIZE = 100
DEFAULT_SCAN_LIMIT = 500
DEFAULT_MAX_VEHICLES = 4


def _headers() -> dict[str, str]:
    if not GHL_API_KEY:
        raise RuntimeError("GHL_API_KEY is not set")
    return {
        "Authorization": f"Bearer {GHL_API_KEY}",
        "Content-Type": "application/json",
        "Version": "2021-07-28",
    }


def _load_custom_fields_map(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    # Some exports include a UTF-8 BOM; utf-8-sig handles both cases.
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    fields = data.get("fields", [])
    name_to_id: dict[str, str] = {}
    for item in fields:
        name = str(item.get("name", "")).strip()
        fid = str(item.get("id", "")).strip()
        if name and fid:
            name_to_id[name] = fid
    return name_to_id


def _cf_value(contact: dict[str, Any], field_id: str) -> Optional[str]:
    for cf in contact.get("customFields", []) or []:
        if cf.get("id") == field_id:
            val = cf.get("value")
            if val in (None, "", {}, 0):
                return None
            return str(val).strip()
    return None


def _find_field_id(name_to_id: dict[str, str], patterns: Iterable[str]) -> Optional[str]:
    for pattern in patterns:
        for name, fid in name_to_id.items():
            if re.fullmatch(pattern, name, flags=re.IGNORECASE):
                return fid
    return None


def _safe_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


def _normalize_gender(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    raw = str(value).strip().lower()
    if raw.startswith("m"):
        return "M"
    if raw.startswith("f"):
        return "F"
    return value.strip()


def _split_name(value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if not value:
        return None, None
    raw = str(value).strip()
    if not raw or not re.search(r"[A-Za-z]", raw):
        return None, None

    parts = re.split(r"\s+", raw)
    if len(parts) >= 2:
        return parts[0], parts[-1]

    # Try camel-case or concatenated names like "RobertCheeks".
    tokens = re.findall(r"[A-Z][a-z]+|[A-Z]+(?![a-z])|[a-z]+", raw)
    if len(tokens) >= 2:
        return tokens[0], tokens[-1]

    return raw, None


def _pick_custom_by_patterns(
    contact: dict[str, Any],
    name_to_id: dict[str, str],
    patterns: Iterable[str],
) -> Optional[str]:
    for name, fid in name_to_id.items():
        for pattern in patterns:
            if re.search(pattern, name, flags=re.IGNORECASE):
                val = _cf_value(contact, fid)
                if val:
                    return val
    return None


def _normalize_contact(
    contact: dict[str, Any],
    name_to_id: dict[str, str],
    max_vehicles: int,
    required_state: Optional[str],
    allow_quoted: bool,
) -> tuple[Optional[dict[str, Any]], str, list[str]]:
    def pick(keys: Iterable[str]) -> Optional[str]:
        for key in keys:
            val = contact.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
        return None

    def pick_custom(field_names: Iterable[str]) -> Optional[str]:
        field_id = _find_field_id(name_to_id, [re.escape(name) for name in field_names])
        if not field_id:
            return None
        return _cf_value(contact, field_id)

    state = (pick(["state", "stateCode", "region"]) or "").upper()
    if required_state and state != required_state:
        return None, "state", []

    # Skip if marked ineligible or already quoted.
    if _cf_value(contact, FIELD_ID_NOT_ELIGIBLE):
        return None, "not_eligible", []
    if not allow_quoted and _cf_value(contact, FIELD_ID_PRICE):
        return None, "has_quote", []

    dob = pick(["dateOfBirth", "date_of_birth"])
    if not dob:
        dob = pick_custom(["Driver #1 Date Of Birth", "Auto.DOB"])

    gender = pick(["gender"]) or pick_custom(["Driver #1 Gender"])
    gender = _normalize_gender(gender)

    marital_status = pick(["maritalStatus", "marital_status"]) or pick_custom(["Driver #1 Marital Status"])
    occupation = pick(["occupation"]) or pick_custom(["Driver #1 Occupation"])

    first_name = pick(["firstName", "first_name"])
    last_name = pick(["lastName", "last_name"])

    if not (first_name and last_name):
        contact_name = pick(["contactName", "name"])
        alt_first, alt_last = _split_name(contact_name)
        first_name = first_name or alt_first
        last_name = last_name or alt_last

    if not (first_name and last_name):
        driver_name = pick_custom(["Driver #1 Name"])
        alt_first, alt_last = _split_name(driver_name)
        first_name = first_name or alt_first
        last_name = last_name or alt_last

    email = pick(["email"]) or _pick_custom_by_patterns(contact, name_to_id, [r"email"]) 

    normalized = {
        "id": contact.get("id") or "",
        "locationId": contact.get("locationId") or "",
        "firstName": first_name,
        "lastName": last_name,
        "postalCode": pick(["postalCode", "zip"]),
        "dateOfBirth": dob,
        "gender": gender,
        "maritalStatus": marital_status,
        "occupation": occupation,
        "phone": pick(["phone"]),
        "address1": pick(["address1", "address"]),
        "city": pick(["city"]),
        "state": state,
        "email": email,
        "vehicles": [],
    }

    # Required fields
    required_keys = [
        "firstName",
        "lastName",
        "postalCode",
        "dateOfBirth",
        "gender",
        "maritalStatus",
        "occupation",
        "phone",
        "address1",
        "city",
        "email",
    ]
    missing_keys = [key for key in required_keys if not normalized.get(key)]
    if missing_keys:
        return None, "missing_fields", missing_keys

    vehicles = _build_vehicles(contact, name_to_id, max_vehicles)
    if not vehicles:
        return None, "no_vehicles", []

    normalized["vehicles"] = vehicles
    normalized["num_vehicles"] = len(vehicles)
    return normalized, "ok", []


def _build_vehicles(contact: dict[str, Any], name_to_id: dict[str, str], max_vehicles: int) -> list[dict[str, Any]]:
    cf_map = {cf.get("id"): cf.get("value") for cf in contact.get("customFields", []) or []}
    vehicles: list[dict[str, Any]] = []

    for idx in range(1, max_vehicles + 1):
        suffix = str(idx)
        year_id = _find_field_id(name_to_id, [fr"Vehicle #{suffix} Year"]) or ""
        make_id = _find_field_id(name_to_id, [fr"Vehicle #{suffix} Make"]) or ""
        model_id = _find_field_id(name_to_id, [fr"Vehicle #{suffix} Model"]) or ""
        vin_id = _find_field_id(
            name_to_id,
            [
                fr"Vehicle #{suffix} Vin Prefix",
                fr"Vehicle #{suffix} VIN",
                fr"Vehicle #{suffix} Vin",
            ],
        ) or ""
        mileage_id = _find_field_id(
            name_to_id,
            [
                fr"Vehicle #{suffix} Annual Distance \(MI\)",
                fr"Vehicle #{suffix} Distance Driven \(MI\)",
                fr"Vehicle #{suffix} Annual Distance",
            ],
        ) or ""
        ownership_id = _find_field_id(
            name_to_id,
            [
                fr"Vehicle #{suffix} Owned / Leased",
                fr"Vehicle #{suffix} Owned/Leased",
            ],
        ) or ""

        year = _safe_int(cf_map.get(year_id))
        make = str(cf_map.get(make_id) or "").strip() if make_id else ""
        model = str(cf_map.get(model_id) or "").strip() if model_id else ""

        if not (year and make and model):
            continue

        vin = str(cf_map.get(vin_id) or "").strip() if vin_id else ""
        mileage = _safe_int(cf_map.get(mileage_id)) or 10000
        ownership_status = _ownership_status_from_value(cf_map.get(ownership_id))

        vehicles.append(
            {
                "year": year,
                "make": make,
                "model": model,
                "vin": vin,
                "annual_mileage": mileage,
                "ownership_status": ownership_status,
                "purchase_date": "03/01/2024",
            }
        )

    return vehicles


def _ownership_status_from_value(raw: Any) -> int:
    if raw is None:
        return 3
    text = str(raw).strip().lower()
    if "own" in text:
        return 1
    if "lease" in text:
        return 3
    if "finance" in text:
        return 2
    return 3


def _extract_contacts(data: Any) -> tuple[list[dict[str, Any]], Optional[Any]]:
    if isinstance(data, list):
        return data, None
    if isinstance(data, dict):
        if isinstance(data.get("contacts"), list):
            contacts = data.get("contacts")
        elif isinstance(data.get("data"), dict) and isinstance(data["data"].get("contacts"), list):
            contacts = data["data"]["contacts"]
        elif isinstance(data.get("contact"), dict):
            contacts = [data["contact"]]
        else:
            contacts = []

        next_start = data.get("nextStartAfter") or data.get("next_start_after") or data.get("startAfter")
        if not next_start and contacts:
            next_start = contacts[-1].get("startAfter")
        return contacts, next_start
    return [], None


def _serialize_start_after(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return str(value)


def _candidate_search_payloads(
    location_id: str,
    page: int,
    page_size: int,
    start_after: Optional[Any],
) -> list[dict[str, Any]]:
    payloads = [
        {
            "locationId": location_id,
            "page": page,
            "limit": page_size,
            "query": "",
            "filters": [],
        },
        {
            "locationId": location_id,
            "page": page,
            "pageLimit": page_size,
            "query": "",
            "filters": [],
        },
    ]

    if start_after is not None:
        payloads.extend(
            [
                {
                    "locationId": location_id,
                    "searchAfter": start_after,
                    "limit": page_size,
                },
                {
                    "locationId": location_id,
                    "searchAfter": start_after,
                    "pageLimit": page_size,
                },
                {
                    "locationId": location_id,
                    "startAfter": start_after,
                    "limit": page_size,
                },
            ]
        )

    return payloads


def _fetch_contacts_page(
    client: httpx.Client,
    location_id: str,
    page_size: int,
    start_after: Optional[Any],
    page: int,
) -> tuple[list[dict[str, Any]], Optional[Any]]:
    search_endpoint = os.getenv("GHL_CONTACTS_SEARCH_ENDPOINT") or f"{GHL_BASE_URL}/contacts/search"
    list_endpoint = os.getenv("GHL_CONTACTS_LIST_ENDPOINT") or f"{GHL_BASE_URL}/contacts"

    headers = _headers()

    # Try search endpoint first with multiple payload shapes.
    last_search_status = None
    last_search_body = ""
    for payload in _candidate_search_payloads(location_id, page, page_size, start_after):
        response = client.post(search_endpoint, headers=headers, json=payload)
        if response.status_code in (200, 201):
            return _extract_contacts(response.json())
        last_search_status = response.status_code
        last_search_body = response.text

    # Fallback: list endpoint variants (with/without trailing slash).
    params = {"locationId": location_id, "limit": page_size}
    if start_after:
        params["startAfter"] = _serialize_start_after(start_after)

    list_headers = {**headers, "LocationId": location_id}
    list_endpoints = [list_endpoint, f"{list_endpoint}/"]
    last_list_status = None
    last_list_body = ""
    for endpoint in list_endpoints:
        response = client.get(endpoint, headers=list_headers, params=params)
        if response.status_code in (200, 201):
            return _extract_contacts(response.json())
        last_list_status = response.status_code
        last_list_body = response.text

    raise RuntimeError(
        "GHL list/search request failed. "
        f"search_status={last_search_status} search_body={last_search_body[:300]!r} "
        f"list_status={last_list_status} list_body={last_list_body[:300]!r}"
    )


def _write_output(path: Path, contacts: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generatedAtUtc": datetime_utc_iso(),
        "count": len(contacts),
        "contacts": contacts,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def datetime_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch eligible GHL contacts for NG360 smoke tests")
    parser.add_argument("--limit", type=int, default=10, help="Number of eligible contacts to return")
    parser.add_argument("--scan-limit", type=int, default=DEFAULT_SCAN_LIMIT, help="Max contacts to scan")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE, help="Page size for API calls")
    parser.add_argument("--max-vehicles", type=int, default=DEFAULT_MAX_VEHICLES, help="Max vehicles to map")
    parser.add_argument("--state", type=str, default="GA", help="Required state (use ANY to disable)")
    parser.add_argument("--allow-quoted", action="store_true", help="Allow contacts with existing quotes")
    parser.add_argument("--output", type=str, default=DEFAULT_OUTPUT, help="Output JSON file path")
    parser.add_argument("--include-raw", action="store_true", help="Include raw contact payloads")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not GHL_API_KEY:
        print("ERROR: GHL_API_KEY is not set", file=sys.stderr)
        return 1
    if not GHL_LOCATION_ID:
        print("ERROR: GHL_LOCATION_ID is not set", file=sys.stderr)
        return 1

    name_to_id = _load_custom_fields_map(Path("data/custom_fields_map.json"))

    eligible: list[dict[str, Any]] = []
    scanned = 0
    start_after: Optional[Any] = None
    page = 1
    reject_counts: dict[str, int] = {}
    missing_field_counts: dict[str, int] = {}
    required_state = args.state.strip().upper()
    if required_state in ("", "ANY"):
        required_state = None

    with httpx.Client(timeout=20) as client:
        while len(eligible) < args.limit and scanned < args.scan_limit:
            contacts, next_start_after = _fetch_contacts_page(
                client,
                location_id=GHL_LOCATION_ID,
                page_size=args.page_size,
                start_after=start_after,
                page=page,
            )
            if not contacts:
                break

            for contact in contacts:
                scanned += 1
                normalized, reason, missing_keys = _normalize_contact(
                    contact,
                    name_to_id,
                    args.max_vehicles,
                    required_state,
                    args.allow_quoted,
                )
                if not normalized:
                    reject_counts[reason] = reject_counts.get(reason, 0) + 1
                    if reason == "missing_fields":
                        for key in missing_keys:
                            missing_field_counts[key] = missing_field_counts.get(key, 0) + 1
                    continue

                record = {
                    "contact_id": normalized.get("id"),
                    "normalized": normalized,
                }
                if args.include_raw:
                    record["raw"] = contact
                eligible.append(record)
                if len(eligible) >= args.limit:
                    break

            start_after = next_start_after
            page += 1

            if scanned >= args.scan_limit:
                break

    _write_output(Path(args.output), eligible)
    print(f"Wrote {len(eligible)} eligible contact(s) to {args.output}")
    print(f"Scanned {scanned} contact(s)")
    if reject_counts:
        print("Rejected counts:")
        for reason, count in sorted(reject_counts.items(), key=lambda item: item[0]):
            print(f"  {reason}: {count}")
    if missing_field_counts:
        print("Missing required fields (top):")
        for key, count in sorted(missing_field_counts.items(), key=lambda item: item[1], reverse=True):
            print(f"  {key}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
