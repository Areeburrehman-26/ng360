"""
contact_defaults.py
-------------------
Backup values for GHL contacts before the worker calls bridge_bot.

Applied when standard or custom-mapped fields are empty so quotes can still run.
Vehicles are NEVER defaulted — at least one real vehicle (year/make/model or VIN)
must be present or the worker stops the job.

bridge_bot.py has separate portal-level fallbacks during automation — not changed here.

Backup values in this module (worker/GHL enrichment layer):
  maritalStatus  → Single     (bridge_bot: _driver_marital_portal_value default)
  occupation     → Manager    (user request; bridge maps unknown → Other dropdown)
  gender         → Male        (bridge_bot: _driver_gender_portal_value default)
  dateOfBirth    → 1980-06-29  (bridge_bot test fixture)
  address/phone  → Atlanta GA placeholders when missing

Portal-only defaults inside bridge_bot (unchanged):
  year_built → 1995, square_footage → 2200, stories → 2, coverage_a → 452937
  prior carrier → Allstate, gender portal → M/F, education → 4, etc.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

logger = logging.getLogger(__name__)

# Top-level contact fields — only applied when empty after enrich_contact_from_custom_fields.
CONTACT_BACKUP_DEFAULTS: dict[str, str] = {
    "firstName": "Unknown",
    "lastName": "Contact",
    "dateOfBirth": "1980-06-29",
    "gender": "Male",
    "maritalStatus": "Single",
    "occupation": "Manager",
    "postalCode": "30301",
    "phone": "+14045550100",
    "address1": "123 Main St",
    "city": "Atlanta",
    "email": "noreply@example.com",
}

_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "firstName": ("firstName", "first_name"),
    "lastName": ("lastName", "last_name"),
    "postalCode": ("postalCode", "zip"),
    "dateOfBirth": ("dateOfBirth", "date_of_birth"),
    "gender": ("gender",),
    "maritalStatus": ("maritalStatus", "marital_status"),
    "occupation": ("occupation",),
    "phone": ("phone",),
    "address1": ("address1", "address"),
    "city": ("city",),
    "email": ("email",),
}

_VEHICLE_SIGNAL_KEYS = ("year", "make", "model", "submodel", "vin_prefix", "vin")


def _is_empty(val: Any) -> bool:
    return val is None or not str(val).strip()


def _get_first_nonempty(contact: dict, aliases: tuple[str, ...]) -> str:
    for key in aliases:
        val = contact.get(key)
        if not _is_empty(val):
            return str(val).strip()
    return ""


def _vehicle_has_signal(vehicle: dict) -> bool:
    return any(not _is_empty(vehicle.get(k)) for k in _VEHICLE_SIGNAL_KEYS)


def contact_has_vehicles(contact: dict) -> bool:
    """True when at least one vehicle has real GHL data (year/make/model or VIN)."""
    vehicles = contact.get("vehicles")
    if not isinstance(vehicles, list):
        return False
    return any(isinstance(v, dict) and _vehicle_has_signal(v) for v in vehicles)


def apply_contact_defaults(contact: dict) -> dict:
    """
    Fill missing contact fields with backup defaults so run_bot can proceed.

    Does NOT invent vehicles — use contact_has_vehicles() to gate quoting.
    Returns a new dict; logs which backup keys were applied.
    """
    out = deepcopy(contact)
    applied: list[str] = []

    for canonical, default in CONTACT_BACKUP_DEFAULTS.items():
        aliases = _FIELD_ALIASES.get(canonical, (canonical,))
        if _get_first_nonempty(out, aliases):
            continue
        primary = aliases[0]
        out[primary] = default
        applied.append(canonical)

    if not str(out.get("state", "")).strip():
        out["state"] = "GA"

    if applied:
        logger.info(
            "[contact_defaults] Applied backup values for: %s",
            ", ".join(applied),
        )

    return out
