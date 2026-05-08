
"""Fetch all GA contacts from GHL and save to JSON."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

GHL_API_KEY = os.getenv("GHL_API_KEY", "").strip()
GHL_BASE_URL = os.getenv("GHL_BASE_URL", "https://services.leadconnectorhq.com").strip()
GHL_LOCATION_ID = os.getenv("GHL_LOCATION_ID", "").strip()

DEFAULT_OUTPUT = "data/ghl_contacts_ga.json"
DEFAULT_PAGE_SIZE = 100


def _headers() -> dict[str, str]:
    if not GHL_API_KEY:
        raise RuntimeError("GHL_API_KEY is not set")
    return {
        "Authorization": f"Bearer {GHL_API_KEY}",
        "Content-Type": "application/json",
        "Version": "2021-07-28",
    }


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

    last_search_status = None
    last_search_body = ""
    for payload in _candidate_search_payloads(location_id, page, page_size, start_after):
        response = client.post(search_endpoint, headers=headers, json=payload)
        if response.status_code in (200, 201):
            return _extract_contacts(response.json())
        last_search_status = response.status_code
        last_search_body = response.text

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


def _get_state(contact: dict[str, Any]) -> str:
    for key in ("state", "stateCode", "region"):
        value = str(contact.get(key, "")).strip().upper()
        if value:
            return value
    return ""


def _fetch_contact_detail(client: httpx.Client, contact_id: str) -> dict[str, Any]:
    url = f"{GHL_BASE_URL}/contacts/{contact_id}"
    response = client.get(url, headers=_headers())
    if response.status_code != 200:
        raise RuntimeError(f"GHL contact fetch failed for {contact_id}: {response.status_code} {response.text}")
    data = response.json()
    return data.get("contact", data)


def _write_output(path: str, contacts: list[dict[str, Any]]) -> None:
    payload = {
        "generatedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(contacts),
        "contacts": contacts,
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch GA contacts from GHL")
    parser.add_argument("--output", type=str, default=DEFAULT_OUTPUT, help="Output JSON file path")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE, help="Page size for API calls")
    parser.add_argument("--sleep-ms", type=int, default=0, help="Sleep between contact fetches")
    parser.add_argument("--max-contacts", type=int, default=0, help="Maximum GA contacts to save (0 = all)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not GHL_API_KEY:
        print("ERROR: GHL_API_KEY is not set", file=sys.stderr)
        return 1
    if not GHL_LOCATION_ID:
        print("ERROR: GHL_LOCATION_ID is not set", file=sys.stderr)
        return 1

    ga_contacts: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    scanned = 0
    start_after: Optional[Any] = None
    page = 1

    with httpx.Client(timeout=20) as client:
        while True:
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
                contact_id = str(contact.get("id") or "").strip()
                if not contact_id or contact_id in seen_ids:
                    continue
                seen_ids.add(contact_id)

                state = _get_state(contact)
                if not state or state == "GA":
                    try:
                        full_contact = _fetch_contact_detail(client, contact_id)
                    except Exception as exc:
                        print(f"WARN: {exc}")
                        continue

                    if _get_state(full_contact) == "GA":
                        ga_contacts.append(full_contact)

                if args.max_contacts and len(ga_contacts) >= args.max_contacts:
                    break

                if args.sleep_ms > 0:
                    time.sleep(args.sleep_ms / 1000.0)

            if args.max_contacts and len(ga_contacts) >= args.max_contacts:
                break

            start_after = next_start_after
            page += 1
            if not start_after and len(contacts) < args.page_size:
                break

    _write_output(args.output, ga_contacts)
    print(f"Wrote {len(ga_contacts)} GA contact(s) to {args.output}")
    print(f"Scanned {scanned} contact(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
