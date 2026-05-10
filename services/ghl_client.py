"""
ghl_client.py
-------------
GoHighLevel API client for the NG360 Bot.
Uses the v2 API endpoint (services.leadconnectorhq.com) with a pit- token.

Custom field IDs are defined in ``ghl_contact_fieldids.py`` (location export).

LOCATION ID (from contact response):
  Czwg7VWYU6myocqsb86R

NOTE on customFields vs customField:
  GHL v2 API returns "customFields" (plural) not "customField".
  Fields have only "id" and "value" — no "key" in the contact response.
  We match by field ID, not by key name.
"""

import logging
import os
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

from ghl_contact_fieldids import *  # noqa: F401,F403 — FIELD_ID_* source of truth

load_dotenv()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GHL_API_KEY       = os.getenv("GHL_API_KEY", "")
GHL_LOCATION_ID   = os.getenv("GHL_LOCATION_ID", "Czwg7VWYU6myocqsb86R")
GHL_BASE_URL      = "https://services.leadconnectorhq.com"
REQUEST_TIMEOUT_S = 20


# ---------------------------------------------------------------------------
# Status values
# ---------------------------------------------------------------------------

STATUS_COMPLETED     = "completed"
STATUS_FAILED        = "failed"
STATUS_INELIGIBLE    = "ineligible"
STATUS_NOT_COMPLETED = "not completed"

# Tags used to trigger GHL workflows
TAG_INSTANT_AUTOFILL  = "instantautofill"
TAG_NG_SUCCESS        = "ng-quote-success"
TAG_NG_FAILED         = "ng-quote-failed"
TAG_NG_MISSING_DATA   = "ng-quote-missing-data"
TAG_NG_PROCESSING     = "ng-quote-processing"
TAG_NG_NOT_ELIGIBLE   = "ng-quote-not-eligible"


class GHLError(RuntimeError):
    """Raised when a GHL API call fails."""


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------

def _headers() -> dict:
    return {
        "Authorization": f"Bearer {GHL_API_KEY}",
        "Content-Type":  "application/json",
        "Version":       "2021-07-28",
    }


# ---------------------------------------------------------------------------
# Contact fetch
# ---------------------------------------------------------------------------

async def get_contact(contact_id: str) -> dict[str, Any]:
    """
    Fetch full contact from GHL v2 API.
    Returns the contact dict with customFields as a list of {id, value}.
    """
    url = f"{GHL_BASE_URL}/contacts/{contact_id}"
    logger.info("[ghl_client] Fetching contact %s", contact_id)

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
        try:
            response = await client.get(url, headers=_headers())
        except httpx.RequestError as exc:
            raise GHLError(f"Network error fetching contact {contact_id}: {exc}") from exc

    if response.status_code != 200:
        raise GHLError(
            f"GHL GET contact {contact_id} returned {response.status_code}: {response.text}"
        )

    data    = response.json()
    contact = data.get("contact", data)
    logger.info(
        "[ghl_client] Fetched: %s %s",
        contact.get("firstName"), contact.get("lastName"),
    )
    return contact


# ---------------------------------------------------------------------------
# Custom field helpers
# GHL v2 contact returns customFields (plural) as [{id, value}, ...]
# ---------------------------------------------------------------------------

def get_custom_field_by_id(contact: dict, field_id: str) -> Optional[str]:
    """
    Get a custom field value by its GHL field ID.
    Returns None if absent or empty.
    """
    # v2 API uses "customFields" (plural)
    for cf in contact.get("customFields", []):
        if cf.get("id") == field_id:
            val = cf.get("value")
            if val is None or val == "" or val == {} or val == 0:
                return None
            return str(val).strip()
    return None


def has_existing_quote(contact: dict) -> bool:
    """True if bundled or NG auto quote price is already set."""
    return bool(
        get_custom_field_by_id(contact, FIELD_ID_PRICE)
        or get_custom_field_by_id(contact, FIELD_ID_NG_QUOTE_PRICE)
    )


def is_marked_ineligible(contact: dict) -> bool:
    """True if not_eligible is set on the contact."""
    return bool(get_custom_field_by_id(contact, FIELD_ID_NOT_ELIGIBLE))


def has_instant_autofill_tag(contact: dict) -> bool:
    """True if the contact has the instantautofill tag → EXTREME priority."""
    return TAG_INSTANT_AUTOFILL in contact.get("tags", [])


def get_year_built(contact: dict) -> str:
    """Return the year_built custom field value, or empty string."""
    return get_custom_field_by_id(contact, FIELD_ID_YEAR_BUILT) or ""


def _coerce_int(val: Any) -> int | None:
    if val is None:
        return None
    s = str(val).strip().replace(",", "")
    if not s:
        return None
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def _ownership_portal_value(raw: str | None) -> int:
    """Map GHL ownership text to NG360 ``ddlOwnershipStatus`` option values."""
    if not raw:
        return 3
    t = str(raw).strip().lower()
    if "lease" in t:
        return 1
    if "lien" in t or "financ" in t or "loan" in t:
        return 2
    return 3


def _append_vehicle_from_fields(
    contact: dict,
    *,
    year_id: str,
    make_id: str,
    model_id: str,
    submodel_id: str,
    vin_id: str,
    ownership_id: str,
    annual_mi_id: str,
    dist_mi_id: str,
    fallback_annual_id: str,
    into: list[dict],
) -> None:
    y = get_custom_field_by_id(contact, year_id)
    mk = get_custom_field_by_id(contact, make_id)
    md = get_custom_field_by_id(contact, model_id)
    sub = get_custom_field_by_id(contact, submodel_id)
    vin = get_custom_field_by_id(contact, vin_id)
    if not any([y, mk, md, sub, vin]):
        return
    own_raw = get_custom_field_by_id(contact, ownership_id)
    ann = (
        _coerce_int(get_custom_field_by_id(contact, annual_mi_id))
        or _coerce_int(get_custom_field_by_id(contact, dist_mi_id))
        or _coerce_int(get_custom_field_by_id(contact, fallback_annual_id))
        or _coerce_int(get_custom_field_by_id(contact, FIELD_ID_ANNUAL_MILEAGE))
    )
    into.append(
        {
            "year": y,
            "make": mk,
            "model": md,
            "submodel": sub,
            "vin_prefix": vin,
            "ownership_status": _ownership_portal_value(own_raw),
            "annual_mileage": ann or 10000,
            "purchase_date": "03/01/2024",
        }
    )


def _pad_vehicles_to_count(out: dict) -> None:
    vlist = out.get("vehicles")
    if not isinstance(vlist, list):
        vlist = []
    n_raw = out.get("num_vehicles")
    try:
        n_int = int(n_raw) if n_raw is not None else 0
    except (TypeError, ValueError):
        n_int = 0
    target = max(n_int, len(vlist), 1)
    base = (
        dict(vlist[-1])
        if vlist and isinstance(vlist[-1], dict)
        else {"ownership_status": 3, "annual_mileage": 10000, "purchase_date": "03/01/2024"}
    )
    while len(vlist) < target:
        vlist.append(dict(base))
    out["vehicles"] = vlist


def enrich_contact_from_custom_fields(contact: dict) -> dict:
    """
    Copy GHL ``customFields`` into top-level keys and ``vehicles`` the bridge bot expects.

    Safe to call on already-normalized contacts (only fills empty standard keys).
    """
    out = dict(contact)

    _yb = get_year_built(out)
    if _yb and not str(out.get("year_built", "")).strip():
        out["year_built"] = str(_yb).strip()

    cfs = out.get("customFields")
    if not isinstance(cfs, list) or not cfs:
        _pad_vehicles_to_count(out)
        return out

    def set_if_empty(key: str, value: str | None) -> None:
        if value is None or not str(value).strip():
            return
        cur = out.get(key)
        if cur is None or not str(cur).strip():
            out[key] = value

    set_if_empty("firstName", get_custom_field_by_id(out, FIELD_ID_DRV1_FIRST))
    set_if_empty("lastName", get_custom_field_by_id(out, FIELD_ID_DRV1_LAST))
    set_if_empty("dateOfBirth", get_custom_field_by_id(out, FIELD_ID_DRV1_DOB))
    set_if_empty("gender", get_custom_field_by_id(out, FIELD_ID_DRV1_GENDER))
    set_if_empty("maritalStatus", get_custom_field_by_id(out, FIELD_ID_DRV1_MARITAL))
    set_if_empty("occupation", get_custom_field_by_id(out, FIELD_ID_DRV1_OCCUPATION))
    set_if_empty("driverLicenseNumber", get_custom_field_by_id(out, FIELD_ID_DRV1_LIC_NUM))

    home_carrier = get_custom_field_by_id(out, FIELD_ID_CURRENT_HOME_CARRIER)
    insurer = get_custom_field_by_id(out, FIELD_ID_CURRENT_INSURER)
    set_if_empty("prior_carrier_home", home_carrier or insurer)

    n_veh = _coerce_int(get_custom_field_by_id(out, FIELD_ID_TOTAL_VEHICLES))
    if n_veh is None:
        n_veh = _coerce_int(get_custom_field_by_id(out, FIELD_ID_NUM_AUTO))
    if n_veh is not None and n_veh > 0:
        out["num_vehicles"] = n_veh

    vehicles: list[dict] = []
    _append_vehicle_from_fields(
        out,
        year_id=FIELD_ID_VEH1_YEAR,
        make_id=FIELD_ID_VEH1_MAKE,
        model_id=FIELD_ID_VEH1_MODEL,
        submodel_id=FIELD_ID_VEH1_SUBMODEL,
        vin_id=FIELD_ID_VEH1_VIN,
        ownership_id=FIELD_ID_VEH1_OWNERSHIP,
        annual_mi_id=FIELD_ID_VEH1_ANNUAL_MI,
        dist_mi_id=FIELD_ID_VEH1_DIST_MI,
        fallback_annual_id=FIELD_ID_ANNUAL_MILEAGE1,
        into=vehicles,
    )
    _append_vehicle_from_fields(
        out,
        year_id=FIELD_ID_VEH2_YEAR,
        make_id=FIELD_ID_VEH2_MAKE,
        model_id=FIELD_ID_VEH2_MODEL,
        submodel_id=FIELD_ID_VEH2_SUBMODEL,
        vin_id=FIELD_ID_VEH2_VIN,
        ownership_id=FIELD_ID_VEH2_OWNERSHIP,
        annual_mi_id=FIELD_ID_VEH2_ANNUAL_MI,
        dist_mi_id=FIELD_ID_VEH2_DIST_MI,
        fallback_annual_id=FIELD_ID_ANNUAL_MILEAGE2,
        into=vehicles,
    )

    existing = out.get("vehicles")
    if vehicles:
        out["vehicles"] = vehicles
    elif not (isinstance(existing, list) and any(isinstance(v, dict) for v in existing)):
        out["vehicles"] = []

    _pad_vehicles_to_count(out)
    return out


# ---------------------------------------------------------------------------
# Contact updates
# GHL v2 update uses PUT /contacts/{id} with customFields as a list
# ---------------------------------------------------------------------------

async def update_contact_fields(contact_id: str, field_updates: dict[str, str]) -> None:
    """
    Update custom fields on a GHL contact.

    Args:
        contact_id:    GHL contact ID.
        field_updates: Dict of {field_id: value} — use FIELD_ID_* constants.

    Raises:
        GHLError: on non-2xx response.
    """
    # GHL v2 expects customFields as a list of {id, field_value}
    custom_fields_payload = [
        {"id": fid, "field_value": val}
        for fid, val in field_updates.items()
        if fid  # skip empty IDs (fields not yet created in GHL)
    ]

    if not custom_fields_payload:
        logger.warning("[ghl_client] No valid field IDs to update for contact %s", contact_id)
        return

    url     = f"{GHL_BASE_URL}/contacts/{contact_id}"
    payload = {"customFields": custom_fields_payload}

    logger.info(
        "[ghl_client] Updating contact %s — %d fields",
        contact_id, len(custom_fields_payload),
    )

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
        try:
            response = await client.put(url, headers=_headers(), json=payload)
        except httpx.RequestError as exc:
            raise GHLError(f"Network error updating contact {contact_id}: {exc}") from exc

    if response.status_code not in (200, 201):
        raise GHLError(
            f"GHL PUT contact {contact_id} returned {response.status_code}: {response.text}"
        )

    logger.info("[ghl_client] Contact %s updated successfully", contact_id)


async def add_tag_to_contact(contact_id: str, tag: str) -> None:
    """Add a specific tag to a GHL contact to trigger workflows."""
    url = f"{GHL_BASE_URL}/contacts/{contact_id}/tags"
    payload = {"tags": [tag]}

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
        try:
            response = await client.post(url, headers=_headers(), json=payload)
            if response.status_code in (200, 201):
                logger.info("[ghl_client] Added tag '%s' to contact %s", tag, contact_id)
            else:
                logger.warning("[ghl_client] Failed to add tag '%s': %s", tag, response.text)
        except Exception as exc:
            logger.error("[ghl_client] Network error adding tag: %s", exc)


# ---------------------------------------------------------------------------
# High-level update functions
# ---------------------------------------------------------------------------

async def record_successful_quote(
    contact_id: str,
    total_premium: str,
    home_premium: str,
    auto_premium: str,
    drive_url: str,
    pay_plan: str = "",
) -> None:
    """Write NG360 quote results to GHL (bundle + NG auto fields) and tag for success."""
    ng_price = (auto_premium or "").strip() or total_premium
    updates: dict[str, str] = {
        FIELD_ID_PRICE: total_premium,
        FIELD_ID_QUOTE_STATUS: STATUS_COMPLETED,
        FIELD_ID_NG_QUOTE_PRICE: ng_price,
        FIELD_ID_AUTO_QUOTE_STATUS: STATUS_COMPLETED,
    }
    if drive_url.strip():
        updates[FIELD_ID_AUTO_QUOTE_URL] = drive_url.strip()
        updates[FIELD_ID_NG_QUOTE_PDF] = drive_url.strip()

    await update_contact_fields(contact_id, updates)
    await add_tag_to_contact(contact_id, TAG_NG_SUCCESS)


async def record_failed_quote(contact_id: str, reason: str = "", missing_data: bool = False) -> None:
    """Mark quote as failed in GHL and apply appropriate tags."""
    await update_contact_fields(contact_id, {
        FIELD_ID_QUOTE_STATUS: STATUS_FAILED,
        FIELD_ID_AUTO_QUOTE_STATUS: STATUS_FAILED,
    })

    # Apply specific tag based on why it failed
    tag_to_apply = TAG_NG_MISSING_DATA if missing_data else TAG_NG_FAILED
    await add_tag_to_contact(contact_id, tag_to_apply)

    if reason:
        logger.warning("[ghl_client] Quote failed for %s: %s", contact_id, reason)


async def record_ineligible_contact(contact_id: str, reason: str) -> None:
    """
    Mark contact as ineligible (state outside configured eligible list).
    Sets not_eligible — contact will be skipped on future triggers.
    """
    updates: dict[str, str] = {
        FIELD_ID_NOT_ELIGIBLE: reason,
        FIELD_ID_QUOTE_STATUS: STATUS_INELIGIBLE,
        FIELD_ID_AUTO_QUOTE_STATUS: STATUS_INELIGIBLE,
    }
    await update_contact_fields(contact_id, updates)

    await add_tag_to_contact(contact_id, TAG_NG_NOT_ELIGIBLE)
    logger.info("[ghl_client] Contact %s marked ineligible: %s", contact_id, reason)


async def record_processing_started(contact_id: str) -> None:
    """Mark contact as actively being processed to prevent duplicate submissions."""
    await add_tag_to_contact(contact_id, TAG_NG_PROCESSING)


# Note: Home premium breakdown and pay plan are captured in worker notifications.