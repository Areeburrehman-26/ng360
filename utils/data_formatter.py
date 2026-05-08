"""Address, phone, and date formatting helpers."""

import re
from datetime import datetime


def normalize_phone_number(value: str) -> str:
    """Return a normalized 10-digit US phone number string."""
    digits = re.sub(r"\D+", "", value or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        raise ValueError(f"Invalid phone number format: {value}")
    return digits


def split_phone_number(value: str) -> tuple[str, str, str]:
    """Split a phone number into area code, prefix, and line number."""
    digits = normalize_phone_number(value)
    return digits[0:3], digits[3:6], digits[6:10]


def format_date_mmddyyyy(value: str) -> str:
    """Return date in strict MMDDYYYY with separators removed."""
    raw = (value or "").strip()
    digits = re.sub(r"\D+", "", raw)

    if len(digits) == 8:
        # Validate calendar correctness while preserving MMDDYYYY output.
        try:
            datetime.strptime(digits, "%m%d%Y")
            return digits
        except ValueError:
            pass

    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%m%d%Y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%m%d%Y")
        except ValueError:
            continue

    raise ValueError(f"Cannot parse date: {value}")


def format_date_to_mmddyyyy(value: str) -> str:
    """Compatibility alias used by bridge automation code."""
    return format_date_mmddyyyy(value)


def format_address(line1: str, city: str, state: str, postal_code: str) -> str:
    parts = [part for part in [line1, city, state, postal_code] if part]
    return ", ".join(parts)
