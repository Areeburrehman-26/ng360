"""
FastAPI webhook server for the NG360 Bot - port 8004.

Pipeline:
  - Validate state eligibility (GA only)
  - Run duplicate-check (price and not_eligible fields)
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from dotenv import load_dotenv

from core.logging_setup import configure_logging
from core.queue_manager import Priority, queue_manager
from services.ghl_client import (
    GHLError,
    get_contact,
    has_instant_autofill_tag,
    has_existing_quote,
    is_marked_ineligible,
    record_ineligible_contact,
)

load_dotenv()
logger = logging.getLogger(__name__)


def _as_bool(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}

def _parse_eligible_states(raw_states: str) -> set[str]:
    states = {state.strip().upper() for state in raw_states.split(",") if state.strip()}
    return states or {"GA"}


def _extract_state(payload: dict, contact: dict) -> str:
    payload_state = str(payload.get("state", "")).strip().upper()
    if payload_state:
        return payload_state

    # Most GHL contacts expose state in `state`; fall back to common alternates.
    for key in ("state", "stateCode", "region"):
        value = str(contact.get(key, "")).strip().upper()
        if value:
            return value
    return ""


def _eligible_states_label() -> str:
    return ", ".join(sorted(ELIGIBLE_STATES))


WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT") or os.getenv("GHL_WEBHOOK_PORT", 8004))
ENABLE_NGROK = _as_bool(os.getenv("ENABLE_NGROK", "0"))
NGROK_DOMAIN = str(os.getenv("NGROK_DOMAIN", "")).strip()
NGROK_URL = str(os.getenv("NGROK_URL", "")).strip()
NGROK_AUTH_TOKEN = str(os.getenv("NGROK_AUTH_TOKEN", "")).strip()


def _resolve_public_base_url() -> str:
    if not ENABLE_NGROK:
        return ""
    if NGROK_URL:
        return NGROK_URL.rstrip("/")
    if NGROK_DOMAIN:
        return f"https://{NGROK_DOMAIN}".rstrip("/")
    return ""


PUBLIC_BASE_URL = _resolve_public_base_url()

ELIGIBLE_STATES = _parse_eligible_states(os.getenv("ELIGIBLE_STATES", "GA"))
STATE_CODES = {
    "GA": "10",  # National General GA state code
}

@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging("webhook_server")
    logger.info("[webhook_server] Starting NG360 Bot webhook server on port %d", WEBHOOK_PORT)
    if PUBLIC_BASE_URL:
        logger.info("[webhook_server] Public webhook URL: %s/webhook", PUBLIC_BASE_URL)
    else:
        logger.info("[webhook_server] No ngrok configured (ENABLE_NGROK=0) — internal only")
    await queue_manager.load_and_recover()
    logger.info("[webhook_server] Queue loaded and recovered — server ready")
    yield
    logger.info("[webhook_server] Lifespan shutdown — cleaning up")


app = FastAPI(title="NG360 Bot Webhook Server", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    public_webhook_url = f"{PUBLIC_BASE_URL}/webhook" if PUBLIC_BASE_URL else ""
    return {
        "status": "ok",
        "port": str(WEBHOOK_PORT),
        "ngrok_enabled": str(ENABLE_NGROK).lower(),
        "public_base_url": PUBLIC_BASE_URL,
        "public_webhook_url": public_webhook_url,
    }


@app.post("/webhook")
async def webhook(payload: dict) -> dict:
    # Support two payload shapes:
    # 1) Forwarded from HOA bot: { "contact": {...}, "contact_id": "..." }
    # 2) Direct GHL trigger:     { "contact_id": "...", "state": "GA" }
    embedded_contact = payload.get("contact") if isinstance(payload.get("contact"), dict) else None

    if embedded_contact:
        contact_id = str(
            embedded_contact.get("id") or payload.get("contact_id") or ""
        ).strip()
    else:
        contact_id = str(payload.get("contact_id", "")).strip()

    requested_state = str(payload.get("state", "")).strip().upper()

    if not contact_id:
        raise HTTPException(status_code=400, detail={"reason": "missing contact_id"})

    # Fast fail for explicit ineligible states without an API round trip.
    if requested_state and requested_state not in ELIGIBLE_STATES:
        try:
            await record_ineligible_contact(
                contact_id,
                reason=f"ineligible state '{requested_state}'",
            )
        except Exception as exc:
            logger.warning("[webhook_server] Could not mark contact %s ineligible: %s", contact_id, exc)
        return {
            "accepted": False,
            "reason": f"ineligible state '{requested_state}'",
        }

    # Use the embedded contact (forwarded from HOA) or fetch from GHL.
    if embedded_contact:
        contact = embedded_contact
        logger.info(
            "[webhook_server] Using embedded contact %s (forwarded from HOA bot)", contact_id
        )
    else:
        try:
            contact = await get_contact(contact_id)
        except GHLError as exc:
            raise HTTPException(status_code=502, detail={"reason": f"ghl fetch failed: {exc}"}) from exc

    state = _extract_state(payload, contact)
    if not state:
        return {
            "accepted": False,
            "reason": "missing state in payload/contact",
        }

    if state not in ELIGIBLE_STATES:
        try:
            await record_ineligible_contact(
                contact_id,
                reason=f"ineligible state '{state}'",
            )
        except Exception as exc:
            logger.warning("[webhook_server] Could not mark contact %s ineligible: %s", contact_id, exc)
        return {
            "accepted": False,
            "reason": f"ineligible state '{state}'",
        }

    if has_existing_quote(contact):
        logger.info(
            "[webhook_server] Contact %s skipped - price already set (existing quote)",
            contact_id,
        )
        return {
            "accepted": False,
            "reason": "existing quote",
        }

    if is_marked_ineligible(contact):
        return {
            "accepted": False,
            "reason": "contact marked not eligible",
        }

    if queue_manager.is_contact_already_queued(contact_id):
        return {
            "accepted": False,
            "reason": "already queued",
        }

    first_name = str(contact.get("firstName") or contact.get("first_name") or "").strip()
    last_name = str(contact.get("lastName") or contact.get("last_name") or "").strip()
    priority = Priority.EXTREME if has_instant_autofill_tag(contact) else Priority.HIGH
    await queue_manager.enqueue(
        contact_id=contact_id,
        priority=priority,
        first_name=first_name,
        last_name=last_name,
        state=state,
    )

    return {
        "accepted": True,
        "queued": True,
        "state_code": STATE_CODES.get(state, ""),
    }


@app.post("/manual-trigger")
async def manual_trigger(payload: dict) -> dict:
    contact_id = str(payload.get("contact_id", "")).strip()
    requested_state = str(payload.get("state", "")).strip().upper()
    if not contact_id:
        raise HTTPException(status_code=400, detail={"reason": "missing contact_id"})

    if requested_state and requested_state not in ELIGIBLE_STATES:
        try:
            await record_ineligible_contact(
                contact_id,
                reason=f"ineligible state '{requested_state}'",
            )
        except Exception as exc:
            logger.warning("[webhook_server] Could not mark contact %s ineligible: %s", contact_id, exc)
        raise HTTPException(
            status_code=400,
            detail={
                "reason": (
                    f"ineligible state '{requested_state}' - manual triggers require "
                    f"{_eligible_states_label()}"
                )
            },
        )

    try:
        contact = await get_contact(contact_id)
    except GHLError as exc:
        raise HTTPException(status_code=502, detail={"reason": f"ghl fetch failed: {exc}"}) from exc

    state = _extract_state(payload, contact)
    if not state:
        raise HTTPException(
            status_code=400,
            detail={"reason": "missing state in payload/contact"},
        )

    if state not in ELIGIBLE_STATES:
        try:
            await record_ineligible_contact(
                contact_id,
                reason=f"ineligible state '{state}'",
            )
        except Exception as exc:
            logger.warning("[webhook_server] Could not mark contact %s ineligible: %s", contact_id, exc)
        raise HTTPException(
            status_code=400,
            detail={
                "reason": f"ineligible state '{state}' - manual triggers require {_eligible_states_label()}"
            },
        )

    if has_existing_quote(contact):
        return {
            "accepted": False,
            "queued": False,
            "reason": "existing quote",
        }

    if queue_manager.is_contact_already_queued(contact_id):
        return {
            "accepted": False,
            "queued": False,
            "reason": "already queued",
        }

    await queue_manager.enqueue(
        contact_id=contact_id,
        priority=Priority.EXTREME if has_instant_autofill_tag(contact) else Priority.HIGH,
        first_name=str(contact.get("firstName") or "").strip(),
        last_name=str(contact.get("lastName") or "").strip(),
        state=state,
    )

    return {"accepted": True, "queued": True}


@app.get("/queue")
async def queue_status() -> dict:
    return await queue_manager.get_status()


def start():
    """Start the webhook server. Called by start_bot.sh via python -m core.webhook_server."""
    import uvicorn
    configure_logging("webhook_server")
    uvicorn.run("core.webhook_server:app", host="0.0.0.0", port=WEBHOOK_PORT, reload=False)


if __name__ == "__main__":
    start()
