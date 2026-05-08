"""
ghl_client.py
-------------
GoHighLevel API client for the NG360 Bot.
Uses the v2 API endpoint (services.leadconnectorhq.com) with a pit- token.

Field IDs are shared with the HOA bot and hardcoded here:
    FIELD_ID_PRICE
    FIELD_ID_QUOTE_STATUS
    FIELD_ID_NOT_ELIGIBLE

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
# Shared GHL custom field IDs (reused from HOA bot)
# ---------------------------------------------------------------------------

FIELD_ID_PRICE        = "FbUGPnB3rSRHDU52RV2d"  # Reusing fire_price for total bundled premium
FIELD_ID_QUOTE_STATUS = "WxZtUOwNYitB1ZRKxzgY"  # Reusing fire_quote_status
FIELD_ID_NOT_ELIGIBLE = "Ni7UAcQDhxsWG6OwnBdh"  # Reusing not_eligible

# Keep legacy helper fields that are still read by automation.
FIELD_ID_YEAR_BUILT      = "oGHsIqmSaJUdYs3QHtOI"   # Year Built


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
    """True if price is already set — contact already has a quote."""
    return bool(get_custom_field_by_id(contact, FIELD_ID_PRICE))


def is_marked_ineligible(contact: dict) -> bool:
    """True if not_eligible is set on the contact."""
    return bool(get_custom_field_by_id(contact, FIELD_ID_NOT_ELIGIBLE))


def has_instant_autofill_tag(contact: dict) -> bool:
    """True if the contact has the instantautofill tag → EXTREME priority."""
    return TAG_INSTANT_AUTOFILL in contact.get("tags", [])


def get_year_built(contact: dict) -> str:
    """Return the year_built custom field value, or empty string."""
    return get_custom_field_by_id(contact, FIELD_ID_YEAR_BUILT) or ""


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
    """Write NG360 quote results to shared HOA fields and tag for success."""
    updates = {
        FIELD_ID_PRICE: total_premium,
        FIELD_ID_QUOTE_STATUS: STATUS_COMPLETED,
    }

    await update_contact_fields(contact_id, updates)
    await add_tag_to_contact(contact_id, TAG_NG_SUCCESS)


async def record_failed_quote(contact_id: str, reason: str = "", missing_data: bool = False) -> None:
    """Mark quote as failed in GHL and apply appropriate tags."""
    await update_contact_fields(contact_id, {
        FIELD_ID_QUOTE_STATUS: STATUS_FAILED,
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
    }
    await update_contact_fields(contact_id, updates)

    await add_tag_to_contact(contact_id, TAG_NG_NOT_ELIGIBLE)
    logger.info("[ghl_client] Contact %s marked ineligible: %s", contact_id, reason)


async def record_processing_started(contact_id: str) -> None:
    """Mark contact as actively being processed to prevent duplicate submissions."""
    await add_tag_to_contact(contact_id, TAG_NG_PROCESSING)


# Note:
# Home/auto premium breakdown, quote URL/date, pay plan, and carrier are still
# captured in worker notifications but are not written to GHL fields.