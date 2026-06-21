import asyncio
import inspect
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from playwright.async_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from services.ghl_client import enrich_contact_from_custom_fields
from utils.data_formatter import format_date_to_mmddyyyy, split_phone_number

logger = logging.getLogger(__name__)

ARTIFACTS_DIR = Path("artifacts")
STORAGE_STATE_PATH = ARTIFACTS_DIR / "natgen_storage_state.json"

load_dotenv()

def log(level: str, msg: str) -> None:
    print(f"[new_bridge_bot][{level}] {msg}", flush=True)

def _normalize_contact_payload(payload: dict) -> dict:
    if isinstance(payload, dict) and isinstance(payload.get("contact"), dict):
        return payload["contact"]
    return payload

def _val(contact: dict, *keys: str) -> str:
    for k in keys:
        v = contact.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    raise RuntimeError(f"Missing contact field: {keys}")

def _retrieve_2fa_code_from_imessage(otp_sender: str, timeout: int = 90, check_interval: int = 2, initial_wait: int = 60) -> str | None:
    db_path = Path.home() / "Library" / "Messages" / "chat.db"
    if not db_path.exists():
        log("WARN", f"Messages DB not found: {db_path}")
        return None

    apple_epoch = 978307200
    start_time = time.time()

    if initial_wait > 0:
        log("INFO", f"Waiting {initial_wait}s before reading iMessage for OTP...")
        time.sleep(initial_wait)

    while (time.time() - start_time) < timeout:
        try:
            current_apple_time = int(time.time()) - apple_epoch
            threshold = current_apple_time - (10 * 60)
            threshold_ns = threshold * 1_000_000_000

            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT message.date, message.text
                FROM message
                JOIN handle ON message.handle_id = handle.ROWID
                WHERE
                  message.is_from_me = 0
                  AND handle.id = ?
                  AND (message.date > ? OR message.date > ?)
                  AND message.text IS NOT NULL
                ORDER BY message.date DESC
                LIMIT 100
                """,
                (otp_sender, threshold, threshold_ns),
            )
            rows = cursor.fetchall()
            conn.close()

            if rows:
                for _, txt in rows:
                    latest_text = txt or ""
                    match = re.search(r"\b(\d{6})\b", latest_text)
                    if match:
                        return match.group(1)

            elapsed = int(time.time() - start_time)
            log("INFO", f"Waiting for 2FA code from {otp_sender}... {elapsed}s")
            time.sleep(check_interval)
        except Exception as exc:
            log("WARN", f"2FA read error: {exc}")
            time.sleep(check_interval)

    log("WARN", "2FA code was not found before timeout. Dumping recent sender/code candidates...")
    _log_recent_otp_candidates_from_imessage()
    return None


async def full_flow(page: Page, username: str, password: str, otp_sender: str, contact: dict):

    # iMessage DB check
    db_path = Path.home() / "Library" / "Messages" / "chat.db"
    if not db_path.exists():
        log("WARN", f"Messages DB not found: {db_path}")
        return

    # --- LOGIN ---
    await page.goto("https://natgenagency.com/Login.aspx")
    await page.wait_for_load_state("domcontentloaded")

    if "MainMenu.aspx" not in page.url:
        # Username daalo aur submit karo
        await page.wait_for_selector("#Username", state="visible")
        await page.fill("#Username", username)
        await page.click("button[type='submit']")

        if "MainMenu.aspx" not in page.url:
            # Password page load hone ka wait karo phir fill karo
            await page.wait_for_selector("#Password", state="visible")
            await page.fill("#Password", password)
            await page.click("button[type='submit']")

            if "MainMenu.aspx" not in page.url:
                # 2FA aaya — SMS choose karo
                await page.click("#loginWith2faSms")
                await asyncio.sleep(30)
                code = _retrieve_2fa_code_from_imessage(otp_sender=otp_sender)
                await page.fill("#TwoFactorCode", code)
                await page.click("#verifyButton")
                await page.wait_for_url("**/MainMenu.aspx")

    log("INFO", "Login complete — on Main Menu")

    # --- STATE + PRODUCT SELECT ---
    await page.select_option("#ctl00_MainContent_wgtMainMenuNewQuote_ddlState", "GA")
    await page.select_option("#ctl00_MainContent_wgtMainMenuNewQuote_ddlProduct", "PKGProtect2")
    await page.click("#ctl00_MainContent_wgtMainMenuNewQuote_btnContinue")
    await page.wait_for_url("**/ClientSearch*", timeout=30000)

    log("INFO", "On Client Search page")

    # --- CLIENT SEARCH + ADD NEW ---
    await page.fill("#MainContent_txtFirstName", _val(contact, "firstName", "first_name"))
    await page.fill("#MainContent_txtLastName", _val(contact, "lastName", "last_name"))
    await page.fill("#MainContent_txtZipCode", _val(contact, "postalCode", "zip"))
    await page.click("#MainContent_btnSearch")
    await page.wait_for_load_state("networkidle")
    await page.wait_for_selector("#MainContent_btnAddNewClient", state="visible")
    await page.click("#MainContent_btnAddNewClient")
    await page.wait_for_url("**/ClientInfo*", timeout=30000)

    log("INFO", "On Client Info page — flow complete so far")
    






async def run_bot(contact:dict) -> dict:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    contact = _normalize_contact_payload(contact)
    enrich_contact_from_custom_fields(contact)

    username = os.environ.get("NATGEN_USERNAME", "")
    password = os.environ.get("NATGEN_PASSWORD", "")
    agent_id = os.environ.get("NATGEN_AGENT_ID", "")
    otp_sender = os.environ.get("NATGEN_2FA_SENDER", "43015")
    otp_timeout = int(os.environ.get("NATGEN_2FA_TIMEOUT", "30"))
    otp_initial_wait = int(os.environ.get("NATGEN_2FA_INITIAL_WAIT", "0"))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context_kwargs = {"accept_downloads": True, "viewport": None}
        if STORAGE_STATE_PATH.exists():
            context_kwargs["storage_state"] = str(STORAGE_STATE_PATH)
            log("INFO", f"Loading saved auth state from {STORAGE_STATE_PATH}")
        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()   

        results = {
            "premium": None,
            "pdf_path": None,
            "error": None,
            "home_premium": None,
            "auto_premium": None,
            "pay_plan": "",
        }

        try:
            logger.debug("bot is starting")
            await full_flow(page, username, password, otp_sender, contact)
        except Exception as exc:
            log("ERROR", f"Bot failed: {exc}")
            results["error"] = str(exc)

        return results









