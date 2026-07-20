# Slack webhook calls (success + alerts)
"""
notifier.py
-----------
Slack webhook notification service for the HOA Bot.
Slack webhook notification service for the NG360 Bot.

Channels:
  #insurance-quotes  — successful quote completions
  #bot-alerts        — failures, errors, and health warnings

All notification functions are fire-and-forget safe:
  If Slack is unreachable, a warning is logged but no exception is raised.
  Slack failure must NEVER block or fail a quote run.
"""

import logging
import os
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SLACK_WEBHOOK_QUOTES  = os.getenv("SLACK_WEBHOOK_QUOTES", "")
SLACK_WEBHOOK_ALERTS  = os.getenv("SLACK_WEBHOOK_ALERTS", "")
REQUEST_TIMEOUT_S     = 10


# ---------------------------------------------------------------------------
# Low-level sender
# ---------------------------------------------------------------------------

async def _post_to_slack(webhook_url: str, payload: dict) -> None:
    """
    Send a JSON payload to a Slack incoming webhook.
    Logs a warning on failure — never raises.
    """
    if not webhook_url:
        logger.warning("[notifier] Slack webhook URL not configured — skipping notification")
        return

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
            response = await client.post(webhook_url, json=payload)
        if response.status_code != 200:
            logger.warning(
                "[notifier] Slack returned %s: %s", response.status_code, response.text
            )
    except httpx.RequestError as exc:
        logger.warning("[notifier] Slack request failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def notify_quote_success(
    first_name: str,
    last_name: str,
    state: str,
    total_premium: str,
    home_premium: str,
    auto_premium: str,
    drive_url: str,
    contact_id: str,
) -> None:
    """Post a success message to the quotes channel."""
    customer = f"{first_name} {last_name}"
    timestamp = _now_formatted()

    payload = {
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":tada: *NG360 Quote Success*\n"
                        f"*Customer:* {customer} ({state})\n"
                        f"*Total Bundled Premium:* {total_premium}\n"
                        f"• Home Premium: {home_premium}\n"
                        f"• Auto Premium: {auto_premium}\n"
                        f"<{drive_url}|*View Quote PDF*>\n"
                        f"_{timestamp}_ | <https://app.gohighlevel.com/v2/location/{os.getenv('GHL_LOCATION_ID')}/contacts/detail/{contact_id}|View in GHL>"
                    ),
                },
            }
        ]
    }

    logger.info("[notifier] Sending success alert for %s", customer)
    await _post_to_slack(SLACK_WEBHOOK_QUOTES, payload)


async def notify_quote_failure(
    first_name: str,
    last_name: str,
    state: str,
    contact_id: str,
    reason: str,
) -> None:
    """Post a failure message to the alerts channel."""
    customer = f"{first_name} {last_name}"
    timestamp = _now_formatted()

    payload = {
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":x: *NG360 Quote FAILED*\n"
                        f"*Customer:* {customer} ({state})\n"
                        f"*Reason:* {reason}\n"
                        f"*GHL Contact:* {contact_id}\n"
                        f"*Time:* {timestamp}"
                    ),
                },
            }
        ]
    }

    logger.warning("[notifier] Sending failure alert for %s — %s", customer, reason)
    await _post_to_slack(SLACK_WEBHOOK_ALERTS, payload)


async def notify_bot_health(message: str, is_critical: bool = False) -> None:
    """
    Post a general health/status message to #bot-alerts.
    Use for: queue backlog warnings, restart events, critical errors.

    Args:
        message:     The message body.
        is_critical: If True, uses a red warning emoji.
    """
    emoji = ":rotating_light:" if is_critical else ":information_source:"
    timestamp = _now_formatted()

    payload = {
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{emoji} *NG360 Bot Alert*\n{message}\n_Time: {timestamp}_",
                },
            }
        ]
    }

    await _post_to_slack(SLACK_WEBHOOK_ALERTS, payload)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_formatted() -> str:
    """Return a human-readable UTC timestamp string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

# ---------------------------------------------------------------------------
# Audit alerts (quote_auditor -> #bot-alerts)
# ---------------------------------------------------------------------------

async def notify_audit_alert(header: str, issues: list) -> None:
    """Send a quote-audit flag to the alerts channel. Fire-and-forget."""
    lines = "\n".join(f"• {i}" for i in issues)
    payload = {"text": f":rotating_light: *{header}*\n{lines}"}
    await _post_to_slack(SLACK_WEBHOOK_ALERTS, payload)
