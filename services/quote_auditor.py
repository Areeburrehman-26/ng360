"""
quote_auditor.py — real-time sanity audit, run right after each quote write.

Catches the class of bugs seen in production so a bad value never sits silently
on a customer record:

  * Unit mix-ups — a MONTHLY value stored in the annual Fire Price field, or an
    ANNUAL value stored in the monthly Auto Price field (the Willesa bug).
  * Cheaper-rule violations — a carrier marked as the winner when its price is
    not actually below the price it replaced.
  * Partial / inconsistent writes — carrier says one thing, price or PDF link
    says another (e.g. carrier = National General 360 but the PDF is not an
    NG360/Drive link, or the price field is blank).

Findings are logged, appended to logs/audit_alerts.log, and sent to Slack
(SLACK_WEBHOOK_ALERTS). Auditing is fire-and-forget: it must NEVER raise into
the quote flow.
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from ghl_contact_fieldids import (
    FIELD_ID_PRICE,               # fire_price (annual home)
    FIELD_ID_AUTO_PRICE,          # auto_price (monthly)
    FIELD_ID_FIRE_QUOTE_CARRIER,
    FIELD_ID_AUTO_QUOTE_CARRIER,
    FIELD_ID_UPLOAD_FIRE_QUOTE,
    FIELD_ID_AUTO_QUOTE_URL,
)

logger = logging.getLogger(__name__)

# Plausible ranges (tune as needed)
HOME_ANNUAL_MIN  = 300.0     # annual home premium below this = likely a monthly value
HOME_ANNUAL_MAX  = 50000.0
AUTO_MONTHLY_MAX = 1000.0    # monthly auto above this = likely an annual value
AUTO_MONTHLY_MIN = 10.0

NG360 = "National General 360"
AUDIT_LOG = Path(__file__).resolve().parent.parent / "logs" / "audit_alerts.log"


def _money(v):
    if v is None:
        return None
    s = str(v).strip().replace("$", "").replace(",", "").replace(" ", "")
    if s in ("", "0", "0.0", "0.00"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _is_drive_link(url) -> bool:
    return bool(url) and "drive.google.com" in str(url)


def audit_contact(contact: dict, bot_name: str, expected: dict | None = None) -> list[str]:
    """Return a list of human-readable issues for this contact's quote fields.

    `expected` (optional) carries the bot's decision context so we can re-verify
    the cheaper-rule: keys ng_home_annual, ng_auto_monthly, current_home,
    current_auto, both_cheaper.
    """
    from services.ghl_client import get_custom_field_by_id  # local import avoids cycle

    issues: list[str] = []

    home         = _money(get_custom_field_by_id(contact, FIELD_ID_PRICE))       # annual
    auto         = _money(get_custom_field_by_id(contact, FIELD_ID_AUTO_PRICE))  # monthly
    fire_carrier = (get_custom_field_by_id(contact, FIELD_ID_FIRE_QUOTE_CARRIER) or "").strip()
    auto_carrier = (get_custom_field_by_id(contact, FIELD_ID_AUTO_QUOTE_CARRIER) or "").strip()
    fire_link    = get_custom_field_by_id(contact, FIELD_ID_UPLOAD_FIRE_QUOTE) or ""
    auto_link    = get_custom_field_by_id(contact, FIELD_ID_AUTO_QUOTE_URL) or ""

    # --- Unit plausibility -------------------------------------------------
    if home is not None and home < HOME_ANNUAL_MIN:
        issues.append(
            f"Fire/Home Price ${home:,.2f} is below ${HOME_ANNUAL_MIN:,.0f}/yr — "
            f"looks like a MONTHLY value in the annual field (unit bug)."
        )
    if home is not None and home > HOME_ANNUAL_MAX:
        issues.append(f"Fire/Home Price ${home:,.2f}/yr is implausibly high.")
    if auto is not None and auto > AUTO_MONTHLY_MAX:
        issues.append(
            f"Auto Price ${auto:,.2f}/mo is above ${AUTO_MONTHLY_MAX:,.0f} — "
            f"looks like an ANNUAL value in the monthly field (unit bug)."
        )
    if auto is not None and auto < AUTO_MONTHLY_MIN:
        issues.append(f"Auto Price ${auto:,.2f}/mo is implausibly low.")

    # --- Carrier / PDF consistency (NG360) --------------------------------
    if fire_carrier == NG360 and not _is_drive_link(fire_link):
        issues.append(
            f"Fire Quote Carrier is '{NG360}' but the home PDF link is not a "
            f"National General (Drive) link: {fire_link or '(empty)'}."
        )
    if auto_carrier == NG360 and not _is_drive_link(auto_link):
        issues.append(
            f"Auto Quote Carrier is '{NG360}' but the auto PDF link is not a "
            f"National General (Drive) link: {auto_link or '(empty)'}."
        )

    # --- Partial write ----------------------------------------------------
    if fire_carrier == NG360 and home is None:
        issues.append(f"Fire Quote Carrier is '{NG360}' but Fire Price is empty (partial write).")
    if auto_carrier == NG360 and auto is None:
        issues.append(f"Auto Quote Carrier is '{NG360}' but Auto Price is empty (partial write).")

    # --- Cheaper-rule cross-check (from the bot's own decision) ------------
    if expected:
        nh = expected.get("ng_home_annual")
        na = expected.get("ng_auto_monthly")
        ch = expected.get("current_home")
        ca = expected.get("current_auto")
        if expected.get("both_cheaper"):
            if nh is not None and ch is not None and not (nh < ch):
                issues.append(
                    f"NG360 marked HOME cheaper but ${nh:,.2f}/yr is NOT below the "
                    f"prior ${ch:,.2f}/yr (cheaper-rule / unit bug)."
                )
            if na is not None and ca is not None and not (na < ca):
                issues.append(
                    f"NG360 marked AUTO cheaper but ${na:,.2f}/mo is NOT below the "
                    f"prior ${ca:,.2f}/mo (cheaper-rule / unit bug)."
                )
        if nh is not None and nh < HOME_ANNUAL_MIN:
            issues.append(
                f"NG360 HOME premium ${nh:,.2f}/yr is implausibly low for an ANNUAL "
                f"value — check the premium-extraction units."
            )

    return issues


def _append_audit_log(header: str, issues: list[str]) -> None:
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        with open(AUDIT_LOG, "a") as f:
            f.write(f"[{ts}] {header}\n")
            for i in issues:
                f.write(f"    - {i}\n")
    except Exception as exc:
        logger.warning("[quote_auditor] could not write audit log: %s", exc)


async def audit_and_alert(contact_id: str, bot_name: str,
                          contact: dict | None = None,
                          expected: dict | None = None) -> None:
    """Re-fetch the contact (post-write), run checks, and alert on any issue.
    Fire-and-forget — never raises into the quote flow."""
    try:
        from services import ghl_client as G
        if contact is None:
            contact = await G.get_contact(contact_id)

        issues = audit_contact(contact, bot_name, expected)
        if not issues:
            logger.info("[quote_auditor] %s %s — clean", bot_name, contact_id)
            return

        name = f"{contact.get('firstName','')} {contact.get('lastName','')}".strip()
        header = f"[{bot_name}] AUDIT FLAG — {name or '?'} ({contact_id})"
        logger.warning("%s | %s", header, " | ".join(issues))
        _append_audit_log(header, issues)

        try:
            from services.notifier import notify_audit_alert
            await notify_audit_alert(header, issues)
        except Exception as exc:
            logger.warning("[quote_auditor] Slack alert failed (non-fatal): %s", exc)

    except Exception as exc:
        logger.warning("[quote_auditor] audit failed (non-fatal): %s", exc)
