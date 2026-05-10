"""Playwright bridge automation for NG360 bot."""

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

# Load variables from .env if present (without overriding exported shell vars).
load_dotenv()

# GA PKGProtect2 Client Info — occupation dropdown only allows these exact option values
# (GHL free-text occupations must be mapped). See artifacts/client_info_page1.html.
_NG360_OCCUPATION_CANONICAL = (
    "Athletes, Entertainers, High Profile Profession",
    "Journalist",
    "Military",
    "Politician",
    "Student",
    "Other",
)


def _occupation_value_for_ng360_dropdown(raw: str) -> str:
    """
    Map CRM / GHL occupation text to a National General ClientInfo dropdown value.
    Unknown values fall back to ``Other`` so Playwright can always select a valid option.
    """
    s = (raw or "").strip()
    if not s:
        return "Other"
    for canon in _NG360_OCCUPATION_CANONICAL:
        if s.casefold() == canon.casefold():
            return canon
    low = s.lower()
    if any(
        k in low
        for k in (
            "athlete",
            "entertain",
            "actor",
            "actress",
            "musician",
            "celebrit",
            "performer",
            "nfl",
            "nba",
            "mlb",
        )
    ):
        return "Athletes, Entertainers, High Profile Profession"
    if any(k in low for k in ("journal", "reporter", "news anchor", "correspondent")):
        return "Journalist"
    if any(
        k in low
        for k in (
            "military",
            "army",
            "navy",
            "marine",
            "air force",
            "coast guard",
            "national guard",
            "veteran",
            "retired military",
        )
    ):
        return "Military"
    if any(k in low for k in ("politic", "senator", "congress", "mayor", "governor")):
        return "Politician"
    if any(k in low for k in ("student", "college", "university", "undergrad", "graduate student")):
        return "Student"
    return "Other"


def _years_at_residence_portal_value(raw: Any) -> str:
    """
    Map years-at-residence to National General ``ddlYearsAtAddress`` option values.
    The portal only offers 0,1,2,3 and 99 (displayed as >3). Any 4+ must use ``99``.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return "3"
    try:
        n = int(float(str(raw).strip().replace(",", "")))
    except (TypeError, ValueError):
        return "3"
    if n < 0:
        return "0"
    if n <= 3:
        return str(n)
    return "99"


# Property page (National General) — dropdown option values for #MainContent_ddlNumberOfStories
_NG360_STORIES_VALUES = frozenset(
    {"1", "1.5", "2", "2.5", "3", "3.5", "4", "4.5", "5", "1.75", "2.75", "3.75", "4.75"}
)

# Substring hints -> exact ``ddlPriorInsuranceCompany`` option value (must exist in portal list).
_PRIOR_CARRIER_ALIAS_PREFIXES: tuple[tuple[str, str], ...] = (
    ("allstate", "Allstate Ins Co"),
    ("state farm", "State Farm Group"),
    ("geico", "GEICO"),
    ("progressive", "Progressive Group"),
    ("liberty mutual", "Liberty Mutual Insurance Cos"),
    ("farmers", "Farmers"),
    ("usaa", "USAA Group"),
    ("travelers", "Travelers Prop Cas Group"),
    ("nationwide", "Nationwide Group"),
    ("hartford", "Hartford Insurance Gr"),
    ("safeco", "Safeco Insurance Companies"),
    ("national general", "National General Ins Co"),
    ("natgen", "National General Ins Co"),
    ("chubb", "Chubb"),
    ("american family", "American Family"),
    ("erie", "Erie/Niagara"),
)

_PRIOR_CARRIER_FALLBACK_VALUE = "Other Standard"

_BRIDGEBOT_EXPECTED_CONTACT_FIELDS: tuple[str, ...] = (
    "id",
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
    "driverLicenseNumber",
    "prior_carrier_home",
    "year_built",
    "square_footage",
    "number_of_stories",
    "years_at_residence",
    "datePurchased",
    "effective_date",
    "prior_expiration",
    "years_continuous_ins",
    "vehicles",
)


def _year_built_portal_value(raw: Any) -> str:
    """4-digit year for ``#MainContent_txtYearBuilt``."""
    default = "1995"
    cy = datetime.now().year
    if raw is None or (isinstance(raw, str) and not str(raw).strip()):
        return default
    digits = re.sub(r"\D", "", str(raw).strip())
    if len(digits) >= 4:
        y = int(digits[:4])
    elif len(digits) == 2:
        y = 2000 + int(digits)
        if y > cy + 1:
            y = 1900 + int(digits)
    else:
        return default
    if y < 1800 or y > cy + 1:
        return default
    return str(y)


def _square_footage_portal_value(raw: Any) -> str:
    """Digits only or one decimal place for ``#MainContent_txtSquareFootage``."""
    default = "2200"
    if raw is None or (isinstance(raw, str) and not str(raw).strip()):
        return default
    s = str(raw).strip().replace(",", "")
    if re.fullmatch(r"\d+(?:\.\d)?", s):
        return s
    m = re.search(r"\d+(?:\.\d)?", s)
    return m.group(0) if m else default


def _number_of_stories_portal_value(raw: Any) -> str:
    """Map CRM text to ``ddlNumberOfStories`` option value; default ``2``."""
    default = "2"
    if raw is None or str(raw).strip() == "":
        return default
    s0 = str(raw).strip()
    if s0 in _NG360_STORIES_VALUES:
        return s0
    m = re.search(r"\d+(?:\.\d+)?", s0.replace(",", "."))
    s = m.group(0) if m else s0
    if s in _NG360_STORIES_VALUES:
        return s
    try:
        f = float(s)
        if float(int(f)) == f:
            si = str(int(f))
            if si in _NG360_STORIES_VALUES:
                return si
        for fmt in (lambda x: f"{x:g}", lambda x: str(x)):
            cand = fmt(f)
            if cand in _NG360_STORIES_VALUES:
                return cand
    except (TypeError, ValueError):
        pass
    low = s0.lower()
    if any(w in low for w in ("single", "one story", "1 story")):
        return "1"
    if "three" in low or s0.strip() == "3":
        return "3"
    return default


class NG360BridgeBot:

    def __init__(self, contact: dict) -> None:
        self.contact = enrich_contact_from_custom_fields(self._normalize_contact_payload(contact))
        self.username = os.getenv("NATGEN_USERNAME", "")
        self.password = os.getenv("NATGEN_PASSWORD", "")
        self.agent_id = os.getenv("NATGEN_AGENT_ID", "20050264")
        self.otp_sender = os.getenv("NATGEN_2FA_SENDER", "42668")
        self.otp_timeout = int(os.getenv("NATGEN_2FA_TIMEOUT", "90"))
        self.otp_initial_wait = int(os.getenv("NATGEN_2FA_INITIAL_WAIT", "60"))
        self.pause_on_error = os.getenv("BRIDGE_PAUSE_ON_ERROR", "true").lower() == "true"
        self.page: Page | None = None
        self.context: BrowserContext | None = None

    def _normalize_contact_payload(self, payload: dict) -> dict:
        # Accept either direct contact object or GHL duplicate-search shape: {"contact": {...}}.
        if isinstance(payload, dict) and isinstance(payload.get("contact"), dict):
            return payload["contact"]
        return payload

    # ── Main entry point ──────────────────────────────────────────────

    async def run(self) -> dict:
        self._log_input_field_summary()
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=False, args=["--start-maximized"])
        context_kwargs: dict[str, Any] = {"accept_downloads": True, "viewport": None}
        if STORAGE_STATE_PATH.exists():
            context_kwargs["storage_state"] = str(STORAGE_STATE_PATH)
            self._log("INFO", f"Loading saved auth state from {STORAGE_STATE_PATH}")
        context = await browser.new_context(**context_kwargs)
        self.context = context
        self.page = await context.new_page()
        await self.page.bring_to_front()

        steps = [
            ("Login", self._login),
            ("Select GA + PKGProtect2", self._select_state_and_product),
            ("Client search", self._search_and_add_customer),
            ("Client info page 1", self._fill_client_info_p1),
            ("Client info page 2", self._fill_client_info_p2),
            ("Prefill verification", self._handle_prefill),
            ("Property info", self._fill_property),
            ("Underwriting", self._fill_underwriting),
            ("Loss history", self._click_continue),
            ("Coverage info", self._fill_coverage),
            ("Driver info", self._fill_driver),
            ("Driver violations", self._click_continue),
            ("Vehicle info", self._fill_vehicles),
            ("Auto underwriting", self._fill_auto_underwriting),
        ]

        try:
            for name, fn in steps:
                self._log("STEP", name)
                # Session timeout guard: if portal redirected to login mid-flow, re-auth once.
                if name != "Login" and "Login.aspx" in self._p().url:
                    self._log("WARN", f"Session expired before '{name}', re-authenticating...")
                    await self._login()
                try:
                    await fn()
                    self._log("OK", name)
                except Exception as exc:
                    self._log("FAIL", f"{name}: {exc}")
                    await self._save_html(name)
                    await self._save_screenshot(name)
                    if self.pause_on_error:
                        await asyncio.to_thread(
                            input,
                            f"\n[PAUSE] Failed at '{name}'. Press Enter to exit...",
                        )
                    raise

            self._log("STEP", "Extract premiums")
            premiums = await self._extract_premiums()
            self._log("OK", f"Premiums: {premiums}")

            self._log("STEP", "Download PDF")
            pdf = await self._download_pdf()
            self._log("OK", f"PDF: {pdf or 'none'}")

            return {
                "success": True,
                "total_premium": premiums[0],
                "home_premium": premiums[1],
                "auto_premium": premiums[2],
                "pdf_path": pdf,
                "error": "",
                "premium": premiums[0],
                "drive_url": pdf,
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        finally:
            await asyncio.to_thread(
                input,
                "\n[PAUSE] Flow finished. Press Enter to close browser...",
            )
            await context.close()
            await browser.close()
            await pw.stop()
            self._log("OK", "Browser closed")

    def _log_input_field_summary(self) -> None:
        """
        Print which expected contact fields are present vs missing at run start.
        """
        present: list[str] = []
        missing: list[str] = []

        for key in _BRIDGEBOT_EXPECTED_CONTACT_FIELDS:
            if key == "vehicles":
                vehicles = self.contact.get("vehicles")
                has_vehicles = isinstance(vehicles, list) and any(isinstance(v, dict) for v in vehicles)
                (present if has_vehicles else missing).append(key)
                continue

            value = self.contact.get(key)
            if value is None:
                missing.append(key)
                continue
            if isinstance(value, str):
                if value.strip():
                    present.append(key)
                else:
                    missing.append(key)
                continue
            present.append(key)

        self._log("INFO", f"Input fields present ({len(present)}): {', '.join(present) if present else 'none'}")
        self._log("INFO", f"Input fields missing ({len(missing)}): {', '.join(missing) if missing else 'none'}")

    # ── Login ─────────────────────────────────────────────────────────

    async def _login(self) -> None:
        p = self._p()

        if not self.username.strip():
            raise RuntimeError(
                "NATGEN_USERNAME is empty. Set it before running, e.g. "
                "`export NATGEN_USERNAME='Trustwell0'`."
            )
        if not self.password.strip():
            raise RuntimeError(
                "NATGEN_PASSWORD is empty. Set it before running, e.g. "
                "`export NATGEN_PASSWORD='your_password'`."
            )

        # 1) Try cookie/session path first. If already authenticated, continue directly.
        await p.goto("https://natgenagency.com/", timeout=30000)
        await p.wait_for_load_state("domcontentloaded")
        if "MainMenu.aspx" in p.url:
            self._log("INFO", f"Session active; landed directly on Main Menu: {p.url}")
            await self._save_storage_state()
            return

        # 2) Not logged in yet; proceed with regular login flow.
        await p.goto("https://natgenagency.com/Login.aspx", timeout=30000)
        await self._prepare_login_form()
        await p.wait_for_selector("#txtUserID", state="visible", timeout=30000)
        self._log("INFO", f"Login page loaded: {p.url}")

        # 3) Type username slowly and click SIGN IN
        await self._type_slow("#txtUserID", self.username)
        entered_userid = await p.input_value("#txtUserID")
        self._log("INFO", f"Filled #txtUserID with: {entered_userid!r}")
        await p.click("#btnLogin", timeout=10000)
        self._log("INFO", "Clicked #btnLogin, waiting for next login state...")

        # 4) Decide what happened after submitting User ID.
        needs_password = await self._wait_for_post_userid_state()
        if not needs_password:
            self._log("INFO", "Reached Main Menu after User ID submit (no password screen)")
            await self._save_storage_state()
            return

        # 5) Type password and click SIGN IN
        await self._type_slow("#Password", self.password)
        await p.click("button[type='submit']", timeout=10000)
        self._log("INFO", "Password submitted, waiting for Main Menu or MFA...")

        # 6) Wait for either post-login destination.
        try:
            await p.wait_for_url("**/MainMenu.aspx", timeout=12000)
            self._log("INFO", "Reached Main Menu directly (no MFA challenge)")
            await self._save_storage_state()
            return
        except Exception:
            self._log("INFO", "Main Menu not reached yet; checking MFA flow")

        # 7) If 2FA appears, complete it automatically
        await self._handle_two_factor_if_present()

        # 8) Wait for main menu
        await p.wait_for_url("**/MainMenu.aspx", timeout=30000)
        await self._save_storage_state()

    async def _prepare_login_form(self) -> None:
        p = self._p()
        # Some sessions show "Enable Login" first; click it to reveal user-id form.
        await self._click_enable_login_if_present()
        if await p.locator("#txtUserID").count() == 0:
            await self._click_enable_login_if_present()

    async def _click_enable_login_if_present(self) -> bool:
        p = self._p()
        selectors = [
            "button:has-text('Enable Login')",
            "a:has-text('Enable Login')",
            "text=/enable\\s*login/i",
        ]
        for selector in selectors:
            loc = p.locator(selector).first
            try:
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(timeout=5000)
                    self._log("INFO", "Clicked Enable Login")
                    await p.wait_for_load_state("domcontentloaded")
                    return True
            except Exception:
                continue
        return False

    async def _wait_for_post_userid_state(self, timeout_ms: int = 60000) -> bool:
        """
        Returns True if password is required next.
        Returns False if user lands directly on Main Menu after user-id submit.
        """
        p = self._p()
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        while time.monotonic() < deadline:
            if "MainMenu.aspx" in p.url:
                return False
            if await p.locator("#Password").count() > 0 and await p.locator("#Password").first.is_visible():
                self._log("INFO", f"Password field detected on: {p.url}")
                return True
            await self._click_enable_login_if_present()
            await p.wait_for_timeout(400)

        title = await p.title()
        has_userid = await p.locator("#txtUserID").count() > 0
        has_password = await p.locator("#Password").count() > 0
        error_text = ""
        if await p.locator("#pnlErrorsAllstate").count() > 0:
            error_text = (await p.locator("#pnlErrorsAllstate").inner_text()).strip()
        self._log("WARN", f"Post-userid state not resolved in {timeout_ms/1000:.0f}s. url={p.url}")
        self._log("WARN", f"Page title: {title!r}")
        self._log("WARN", f"Field states: has_txtUserID={has_userid}, has_Password={has_password}")
        if error_text:
            self._log("WARN", f"Login error banner: {error_text!r}")
        raise PlaywrightTimeoutError("Timed out waiting for MainMenu or password page after userid submit.")

    # ── Quote flow steps ──────────────────────────────────────────────

    async def _select_state_and_product(self) -> None:
        p = self._p()
        await p.select_option("#ctl00_MainContent_wgtMainMenuNewQuote_ddlState", "GA")
        await p.select_option("#ctl00_MainContent_wgtMainMenuNewQuote_ddlProduct", "PKGProtect2")
        await p.click("#ctl00_MainContent_wgtMainMenuNewQuote_btnContinue")
        await p.wait_for_url("**/ClientSearch*", timeout=30000)

    async def _search_and_add_customer(self) -> None:
        p = self._p()
        await p.fill("#MainContent_txtFirstName", self._val("firstName", "first_name"))
        await p.fill("#MainContent_txtLastName", self._val("lastName", "last_name"))
        await p.fill("#MainContent_txtZipCode", self._val("postalCode", "zip"))
        await p.click("#MainContent_btnSearch")
        await p.wait_for_load_state("networkidle")
        await p.click("#MainContent_btnAddNewClient")
        await p.wait_for_url("**/ClientInfo*", timeout=30000)

    async def _fill_client_info_p1(self) -> None:
        p = self._p()
        dob = self._date(self._val("dateOfBirth", "date_of_birth"))
        area, prefix, line = split_phone_number(self._val("phone"))

        await p.fill("#MainContent_ucNamedInsured_txtDateOfBirth", dob)
        await p.select_option("#MainContent_ucNamedInsured_ddlGender", self._val("gender"))
        await p.select_option("#MainContent_ucNamedInsured_ddlMaritalStatus", self._val("maritalStatus", "marital_status"))

        occ_raw = self._val("occupation")
        occ_val = _occupation_value_for_ng360_dropdown(occ_raw)
        if occ_val != occ_raw.strip():
            self._log("INFO", f"Occupation mapped for NG360 dropdown: {occ_raw!r} -> {occ_val!r}")
        occ_fallback = await self._select_strict_dropdown(
            "#MainContent_ucNamedInsured_ddlOccupation", occ_val
        )
        # When "Other" is selected, some renders expose a free-text description — fill from CRM if present.
        if occ_val == "Other" or occ_fallback:
            other_txt = p.locator("#MainContent_ucNamedInsured_txtOtherOccupation")
            if await other_txt.count() > 0:
                try:
                    raw_note = occ_raw.strip()
                    if raw_note and raw_note.casefold() != "other":
                        await other_txt.fill(raw_note[:120])
                except Exception:
                    pass

        await p.select_option("#MainContent_ucContactInfo_ucPhoneNumber_ddlPhoneType", "Cell")
        await p.fill("#MainContent_ucContactInfo_ucPhoneNumber_txtAreaCode", area)
        await p.fill("#MainContent_ucContactInfo_ucPhoneNumber_txtPrefix", prefix)
        await p.fill("#MainContent_ucContactInfo_ucPhoneNumber_txtLineNumber", line)

        await p.fill("#MainContent_ucResidentialAddress_txtAddress", self._val("address1", "address"))
        await p.fill("#MainContent_ucResidentialAddress_txtCity", self._val("city"))
        y_raw = self.contact.get("years_at_residence", 3)
        y_val = _years_at_residence_portal_value(y_raw)
        if str(y_raw).strip() != y_val:
            self._log("INFO", f"Years at residence mapped for portal: {y_raw!r} -> {y_val!r}")
        await self._select_strict_dropdown(
            "#MainContent_ddlYearsAtAddress", y_val, fallback_value="3"
        )

        email = self._val("email")
        await p.select_option("#MainContent_ucContactInfo_ucEmailAddress_ddlEmailOption", "Provided")
        await p.fill("#MainContent_ucContactInfo_ucEmailAddress_txtEmailAddress", email)
        await p.fill("#MainContent_ucContactInfo_ucEmailAddress_txtEmailAddressConfirmation", email)

        await p.click("#MainContent_btnContinue")
        await p.wait_for_load_state("networkidle")

    async def _fill_client_info_p2(self) -> None:
        p = self._p()
        await p.select_option("#MainContent_ucGeneralInformation_ddlInputBy", self.agent_id)
        await p.click("#MainContent_btnContinue")
        await p.wait_for_url("**/Prefill*", timeout=30000)

    async def _handle_prefill(self) -> None:
        p = self._p()
        try:
            await p.wait_for_selector("#gvPrefillDriver, #gvPrefillAuto", timeout=5000)

            # Prefer built-in bulk action so the site applies all expected JS/postback logic.
            if await p.locator("#MainContent_ucPrefillDriver_btnAcceptAllDrivers").count() > 0:
                await p.click("#MainContent_ucPrefillDriver_btnAcceptAllDrivers")
                await p.wait_for_load_state("networkidle")

            # Driver prefill uses dropdown statuses. Any "-- Select --" blocks Next.
            # Enforce unresolved rows to Accept ("A") after bulk action as a safeguard.
            driver_statuses = p.locator("#gvPrefillDriver select[name*='ddlDriverStatus']")
            for i in range(await driver_statuses.count()):
                status = driver_statuses.nth(i)
                if await status.is_disabled():
                    continue
                current = (await status.input_value()).strip()
                if current == "-1":
                    await status.select_option("A")

            # Vehicle prefill can also require status dropdowns similar to drivers.
            vehicle_statuses = p.locator(
                "#gvPrefillAuto select[name*='ddl'][name*='Status'], "
                "#gvPrefillAuto select[id*='ddl'][id*='Status']"
            )
            for i in range(await vehicle_statuses.count()):
                status = vehicle_statuses.nth(i)
                if await status.is_disabled():
                    continue
                current = (await status.input_value()).strip()
                if current != "-1":
                    continue
                options = await status.evaluate(
                    """el => Array.from(el.options).map(o => String(o.value || '').trim())"""
                )
                preferred = ("A", "Accept", "1", "True", "Y")
                choice = next((v for v in preferred if v in options), None)
                if not choice:
                    choice = next((v for v in options if v not in ("", "-1")), None)
                if choice:
                    await status.select_option(choice)

            # Accept all prefilled vehicles when available.
            if await p.locator("#btnAcceptAllAutos").count() > 0:
                await p.click("#btnAcceptAllAutos")
            auto_accept = p.locator("#gvPrefillAuto input[type='radio'][name*='rbAccept']")
            for i in range(await auto_accept.count()):
                if not await auto_accept.nth(i).is_checked():
                    await auto_accept.nth(i).check()

            # License verification checkboxes (Steps 107-108 in JSON).
            license_cbs = p.locator(
                "#gvPrefillDriver input[type='checkbox'][id*='License' i], "
                "input[type='checkbox'][id*='chkLicense' i], "
                "input[type='checkbox'][id*='License' i]"
            )
            for i in range(await license_cbs.count()):
                cb = license_cbs.nth(i)
                try:
                    if not await cb.is_checked():
                        await cb.check(timeout=5000)
                except Exception:
                    pass
        except PlaywrightTimeoutError:
            self._log("WARN", "No prefill table, skipping")

        await p.click("#MainContent_btnContinue")
        await p.wait_for_load_state("networkidle")

        # Surface inline validation when Prefill blocks navigation.
        if "Prefill" in p.url and await p.locator("#lstErrors li").count() > 0:
            err = (await p.locator("#lstErrors").inner_text()).strip()
            if err:
                raise RuntimeError(f"Prefill validation blocked continue: {err}")

    async def _fill_property(self) -> None:
        p = self._p()
        fields = {
            "#MainContent_ddlResidenceClass": "Primary",
            "#MainContent_ddlOccupancy2": "OwnerOccupied",
            "#MainContent_ddlNamedInsuredType": "Owner",
            "#MainContent_ddlRoofType": "AS",
            "#MainContent_ddlPrimaryHeat": "Electric",
            "#MainContent_ddlRoofShape": "Gable, Slight Pitch",
            "#MainContent_ddlRoofHail": "False",
            "#MainContent_ddlOilTank": "None",
        }
        for sel, val in fields.items():
            await p.select_option(sel, val)

        yb = _year_built_portal_value(self.contact.get("year_built") or self.contact.get("yearBuilt"))
        await p.fill("#MainContent_txtYearBuilt", yb)

        sq = _square_footage_portal_value(
            self.contact.get("square_footage")
            or self.contact.get("squareFootage")
            or self.contact.get("sqft")
        )
        await p.fill("#MainContent_txtSquareFootage", sq)

        st = _number_of_stories_portal_value(
            self.contact.get("number_of_stories")
            or self.contact.get("num_stories")
            or self.contact.get("stories")
        )
        try:
            await p.select_option("#MainContent_ddlNumberOfStories", st)
        except Exception:
            await p.select_option("#MainContent_ddlNumberOfStories", "2")

        date_purchased = self.contact.get("datePurchased") or self.contact.get("date_purchased") or "04/15/2024"
        await p.fill("#MainContent_txtDatePurchased", self._date(str(date_purchased)))
        await p.fill("#MainContent_txtYearRoofRenovation", "2020")

        # Effective Date — brief requires calendar picker but allows direct typing as fallback.
        if await p.locator("#MainContent_txtEffectiveDate").count() > 0:
            eff_raw = self.contact.get("effective_date") or self.contact.get("effectiveDate") or ""
            if eff_raw:
                await p.fill("#MainContent_txtEffectiveDate", self._date(str(eff_raw)))
            elif not (await p.locator("#MainContent_txtEffectiveDate").input_value()).strip():
                tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%m/%d/%Y")
                await p.fill("#MainContent_txtEffectiveDate", tomorrow)

        await p.click("#MainContent_btnContinue")
        await p.wait_for_load_state("networkidle")
        if "PropertyInfo" in p.url and await p.locator("#lstErrors li").count() > 0:
            err = (await p.locator("#lstErrors").inner_text()).strip()
            if err:
                raise RuntimeError(f"Property validation blocked continue: {err}")

    async def _select_prior_insurance_company(self, p: Page) -> None:
        """
        Map CRM prior-carrier text to ``ddlPriorInsuranceCompany`` options (200+ exact values).
        Uses DOM option list + fuzzy match; falls back to Other Standard.
        """
        sel = "#MainContent_ucPriorPolicyInformation_ddlPriorInsuranceCompany"
        raw = (
            self.contact.get("prior_carrier_home")
            or self.contact.get("prior_carrier")
            or self.contact.get("current_insurer")
            or self.contact.get("current_home_carrier")
            or ""
        )
        raw_s = str(raw).strip()

        loc = p.locator(sel)
        await loc.wait_for(state="visible", timeout=15000)

        options: list[dict[str, str]] = await loc.evaluate(
            """el => Array.from(el.options)
                .filter(o => o.value && o.value !== '-1')
                .map(o => ({ v: o.value, t: (o.textContent || '').trim() }))"""
        )
        if not options:
            raise RuntimeError("Prior insurance company dropdown has no selectable options")

        def norm(s: str) -> str:
            s2 = str(s).replace("&amp;", " and ").replace("&", " and ")
            return re.sub(r"\s+", " ", s2.lower()).strip()

        nr = norm(raw_s) if raw_s else ""

        async def pick_by_value(value: str, log: str | None = None) -> bool:
            try:
                await loc.select_option(value=value, timeout=10000)
                if log:
                    self._log("INFO", log)
                return True
            except Exception:
                try:
                    await loc.select_option(label=value, timeout=5000)
                    if log:
                        self._log("INFO", log)
                    return True
                except Exception:
                    return False

        if not raw_s:
            ok = await pick_by_value(
                _PRIOR_CARRIER_FALLBACK_VALUE,
                f"Prior carrier not set; using {_PRIOR_CARRIER_FALLBACK_VALUE!r}",
            )
            if ok:
                return

        for o in options:
            if o["v"] == raw_s or o["t"] == raw_s:
                await loc.select_option(value=o["v"], timeout=10000)
                return
        for o in options:
            if norm(o["v"]) == nr or norm(o["t"]) == nr:
                self._log("INFO", f"Prior carrier matched (case-insensitive): {raw_s!r} -> {o['v']!r}")
                await loc.select_option(value=o["v"], timeout=10000)
                return

        for needle, canon in _PRIOR_CARRIER_ALIAS_PREFIXES:
            if needle in nr and await pick_by_value(canon, f"Prior carrier alias {needle!r} -> {canon!r}"):
                return

        if len(nr) >= 4:
            for o in options:
                nv, nt = norm(o["v"]), norm(o["t"])
                if nr in nt or nr in nv:
                    self._log("INFO", f"Prior carrier substring match: {raw_s!r} -> {o['v']!r}")
                    await loc.select_option(value=o["v"], timeout=10000)
                    return
            for o in options:
                nv, nt = norm(o["v"]), norm(o["t"])
                if len(nt) >= 6 and (nt in nr or (len(nv) >= 6 and nv in nr)):
                    self._log("INFO", f"Prior carrier label contained in CRM text: {raw_s!r} -> {o['v']!r}")
                    await loc.select_option(value=o["v"], timeout=10000)
                    return

        self._log("WARN", f"No prior carrier match for {raw_s!r}; using {_PRIOR_CARRIER_FALLBACK_VALUE!r}")
        if await pick_by_value(_PRIOR_CARRIER_FALLBACK_VALUE, None):
            return

        await loc.select_option(value=options[0]["v"], timeout=10000)
        self._log("WARN", f"Prior carrier fallback used first list entry: {options[0]['v']!r}")

    async def _fill_underwriting(self) -> None:
        p = self._p()
        expiry = self._date(str(self.contact.get("prior_expiration", "03/31/2026")))

        await p.select_option("#MainContent_ucPriorPolicyInformation_ddlPriorInsuranceCoverage", "Prior standard insurance")
        await self._select_prior_insurance_company(p)
        await p.fill("#MainContent_ucPriorPolicyInformation_txtExpirationDate", expiry)
        await p.fill("#MainContent_ucPriorPolicyInformation_txtContinuousInsurance", str(self.contact.get("years_continuous_ins", 1)))
        await p.click("#MainContent_btnContinue")
        await p.wait_for_load_state("networkidle")

    async def _answer_additional_coverage_questions(self, p: Page) -> None:
        """
        Coverage sometimes shows Additional Questions first (e.g. paved / year-round road access).
        Those must be answered before ``#MainContent_ddlPerils`` appears. Default: Yes / True.
        """
        combined = re.compile(
            r"(accessible|accesible).{0,80}(year|round|road)"
            r"|(paved|plowed|maintained.{0,40}road)"
            r"|(additional\s+questions.{0,120}(?:#?\s*1|question\s*1))"
            r"|(is\s+the\s+home\s+.{0,60}accessible)",
            re.I | re.S,
        )

        async def yes_select(sel: Any) -> bool:
            if await sel.count() == 0:
                return False
            try:
                tag = await sel.evaluate("el => el.tagName.toLowerCase()")
            except Exception:
                return False
            if tag != "select":
                return False
            cur = (await sel.input_value()).strip()
            if cur not in ("", "-1"):
                return True
            for val in ("True", "Yes", "Y", "1"):
                try:
                    await sel.select_option(value=val, timeout=5000)
                    self._log("INFO", f"Additional coverage question -> select value={val!r}")
                    await p.wait_for_load_state("networkidle")
                    return True
                except Exception:
                    continue
            for lab in ("Yes", "Y"):
                try:
                    await sel.select_option(label=lab, timeout=5000)
                    self._log("INFO", f"Additional coverage question -> select label={lab!r}")
                    await p.wait_for_load_state("networkidle")
                    return True
                except Exception:
                    continue
            return False

        async def yes_radio(li: Any) -> bool:
            if await li.count() == 0:
                return False
            for val in ("True", "Yes", "1"):
                r = li.locator(f"input[type='radio'][value='{val}']").first
                if await r.count() > 0:
                    await r.check(timeout=5000)
                    self._log("INFO", f"Additional coverage radio -> {val!r}")
                    await p.wait_for_load_state("networkidle")
                    return True
            yes_lbl = li.locator("label").filter(has_text=re.compile(r"^\s*yes\s*$", re.I)).first
            if await yes_lbl.count() > 0:
                await yes_lbl.click(timeout=5000)
                self._log("INFO", "Additional coverage radio -> clicked Yes label")
                await p.wait_for_load_state("networkidle")
                return True
            return False

        lbls = p.locator("label")
        n = await lbls.count()
        for i in range(min(n, 600)):
            lb = lbls.nth(i)
            try:
                txt = (await lb.inner_text()).strip()
            except Exception:
                continue
            if not txt or not combined.search(re.sub(r"\s+", " ", txt)):
                continue
            fid = await lb.get_attribute("for")
            filled = False
            if fid:
                tg = p.locator(f"#{fid}")
                filled = await yes_select(tg)
            if not filled:
                li = lb.locator("xpath=ancestor::li[1]")
                filled = await yes_radio(li)
                if not filled:
                    filled = await yes_select(li.locator("select").first)
            if filled:
                self._log("INFO", f"Additional coverage label matched: {txt[:100]!r}")

    async def _fill_coverage(self) -> None:
        p = self._p()
        await self._answer_additional_coverage_questions(p)

        perils_sel = "#MainContent_ddlPerils"
        storm_sel = "#MainContent_ddlNamedStormDeductible"
        wind_sel = "#MainContent_ddlWindstormDeductible"

        try:
            await p.wait_for_selector(perils_sel, state="visible", timeout=90000)
        except PlaywrightTimeoutError:
            self._log("WARN", "Perils dropdown not visible yet; retrying additional questions + wait")
            await self._answer_additional_coverage_questions(p)
            await p.wait_for_selector(perils_sel, state="visible", timeout=90000)

        try:
            await p.locator(perils_sel).scroll_into_view_if_needed()
        except Exception:
            pass

        async def pick_dropdown(selector: str, preferred: str, fallbacks: tuple[str, ...]) -> None:
            loc = p.locator(selector)
            for val in (preferred,) + fallbacks:
                try:
                    await loc.select_option(value=val, timeout=20000)
                    return
                except Exception:
                    continue
            opts: list[str] = await loc.evaluate(
                """el => [...el.options].filter(o => o.value && o.value !== '-1').map(o => o.value)"""
            )
            if opts:
                await loc.select_option(value=opts[0], timeout=20000)
                self._log("WARN", f"{selector}: using first available option {opts[0]!r}")

        await pick_dropdown(perils_sel, "0.010", ("0.01",))
        await pick_dropdown(storm_sel, "0.020", ("0.02",))
        await pick_dropdown(wind_sel, "0.020", ("0.02",))

        # Some products require Coverage A on this page before allowing navigation.
        cov_a = p.locator("#MainContent_ucCoreCoverages_rptCoreCoverages_txtCovA_0")
        if await cov_a.count() > 0:
            current_cov_a = (await cov_a.input_value()).strip()
            if not current_cov_a:
                await cov_a.fill("350000")
                try:
                    # ASP.NET pages often require blur/change to trigger postback validation.
                    await cov_a.evaluate(
                        """el => {
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                            el.blur();
                        }"""
                    )
                except Exception:
                    pass
                self._log("INFO", "Coverage A empty; defaulted to 350000")
                await p.wait_for_load_state("networkidle")

        await p.click("#MainContent_btnContinue")
        await p.wait_for_load_state("networkidle")

        # If still on coverage, surface server validation early.
        if "Coverage" in p.url and await p.locator("#lstErrors li").count() > 0:
            err = (await p.locator("#lstErrors").inner_text()).strip()
            if err:
                raise RuntimeError(f"Coverage validation blocked continue: {err}")

    async def _fill_driver(self) -> None:
        p = self._p()
        # Driver step can land on schedule first; enter a driver edit form.
        if await p.locator("#MainContent_gvDrivers").count() > 0:
            # Do not depend on Delete links here; some pages omit/re-render them.
            # Always enter primary driver edit directly from schedule.
            view_edit = p.locator("#MainContent_gvDrivers tbody tr a:has-text('View/Edit')").first
            await view_edit.wait_for(state="visible", timeout=15000)
            clicked = False
            for _ in range(3):
                try:
                    await view_edit.click(timeout=10000)
                    clicked = True
                    break
                except Exception:
                    await p.wait_for_timeout(400)
            if not clicked:
                # ASP.NET partial postbacks can detach/rebind anchor handlers; JS click fallback.
                await p.evaluate(
                    """
                    () => {
                        const el = Array.from(document.querySelectorAll("#MainContent_gvDrivers a"))
                          .find((a) => (a.textContent || "").includes("View/Edit"));
                        if (el) el.click();
                    }
                    """
                )
            await p.wait_for_load_state("networkidle")

        async def _select_if_present(
            selector: str,
            value: str,
            wait_postback: bool = False,
            label_fallback: str | None = None,
        ) -> None:
            if await p.locator(selector).count() == 0:
                return
            field = p.locator(selector)
            try:
                if not await field.first.is_visible():
                    return
            except Exception:
                return
            current = (await field.input_value()).strip()
            if current != value:
                try:
                    await field.select_option(value=value)
                except Exception:
                    if label_fallback:
                        await field.select_option(label=label_fallback)
                    else:
                        raise
                if wait_postback:
                    await p.wait_for_load_state("networkidle")

        async def _set_required_select(selector: str, value: str, label: str, wait_postback: bool = False) -> None:
            # Some DriverInfo fields are re-rendered by ASP.NET postbacks; retry once.
            if await p.locator(selector).count() == 0:
                return
            # Driver page includes conditional questions rendered but hidden via CSS.
            # Hidden controls should not be treated as required.
            try:
                if not await p.locator(selector).first.is_visible():
                    self._log("INFO", f"Driver field hidden; skipping required select: {label}")
                    return
            except Exception:
                return
            for _ in range(2):
                await _select_if_present(selector, value, wait_postback=wait_postback, label_fallback=label)
                current = (await p.locator(selector).input_value()).strip()
                if current not in ("", "-1"):
                    return
            raise RuntimeError(f"Driver field still unselected after fill: {label}")

        async def _force_active_license_status() -> None:
            """
            Always keep Driver License Status on Active.
            ASP.NET postbacks can reset this field mid-step.
            """
            sel = "#MainContent_ddlDriversLicenseStatus"
            if await p.locator(sel).count() == 0:
                return
            for _ in range(3):
                try:
                    await p.locator(sel).select_option(value="Active")
                except Exception:
                    try:
                        await p.locator(sel).select_option(label="Active")
                    except Exception:
                        pass
                current = (await p.locator(sel).input_value()).strip()
                if current == "Active":
                    return
                await p.wait_for_timeout(250)
            raise RuntimeError("Driver License Status could not be forced to Active")

        async def _force_losses_in_5_years_no() -> None:
            """
            Keep 'Have you had any losses in the past 5 years?' on No/False.
            This field can be reset by postbacks on DriverInfo.
            """
            sel = "#MainContent_ddlLossesIn5Years"
            if await p.locator(sel).count() == 0:
                return
            try:
                if not await p.locator(sel).first.is_visible():
                    return
            except Exception:
                return
            for _ in range(3):
                try:
                    await p.locator(sel).select_option(value="False")
                except Exception:
                    try:
                        await p.locator(sel).select_option(label="No")
                    except Exception:
                        pass
                current = (await p.locator(sel).input_value()).strip()
                if current == "False":
                    return
                await p.wait_for_timeout(250)
            raise RuntimeError("Losses in 5 Years could not be forced to No/False")

        async def _require_selected(selector: str, friendly: str) -> None:
            if await p.locator(selector).count() == 0:
                return
            try:
                if not await p.locator(selector).first.is_visible():
                    return
            except Exception:
                return
            current = (await p.locator(selector).input_value()).strip()
            if current in ("", "-1"):
                raise RuntimeError(f"Driver field still unselected after fill: {friendly}")

        async def _fill_driver_required_fields_once() -> None:
            # Match contractor brief sequence for DriverInfo page.
            await _select_if_present("#MainContent_ddlOperatorType", "Operator", wait_postback=True, label_fallback="Operator")
            await _set_required_select("#MainContent_ddlLossesIn5Years", "False", "Losses in 5 years")
            await _force_losses_in_5_years_no()
            await _set_required_select("#MainContent_ddlDefensiveDriverCourse", "False", "Defensive Driver Course", wait_postback=True)
            await _select_if_present("#MainContent_ddlConnectedDriverOptIn", "False", label_fallback="No")
            await _set_required_select("#MainContent_ddlDriversLicenseStatus", "Active", "Driver License Status", wait_postback=True)
            await _force_active_license_status()
            await _select_if_present("#MainContent_ddlLicenseState", "GA")
            await _force_active_license_status()

        async def _driver_missing_required_fields() -> list[str]:
            missing: list[str] = []
            checks = (
                ("#MainContent_ddlDriversLicenseStatus", "Driver License Status"),
                ("#MainContent_ddlDefensiveDriverCourse", "Defensive Driver Course"),
                ("#MainContent_ddlLossesIn5Years", "Losses in 5 Years"),
            )
            for selector, label in checks:
                if await p.locator(selector).count() == 0:
                    continue
                try:
                    if not await p.locator(selector).first.is_visible():
                        continue
                except Exception:
                    continue
                cur = (await p.locator(selector).input_value()).strip()
                if cur in ("", "-1"):
                    missing.append(label)
            return missing

        # Fill -> verify -> refill loop (up to 2 retries) for flaky postback resets.
        missing_after_fill: list[str] = []
        for attempt in range(3):
            await _fill_driver_required_fields_once()
            missing_after_fill = await _driver_missing_required_fields()
            if not missing_after_fill:
                break
            if attempt < 2:
                self._log(
                    "WARN",
                    f"Driver required fields reset after fill (attempt {attempt + 1}/3): {', '.join(missing_after_fill)}; retrying",
                )
                await p.wait_for_timeout(300)
        if missing_after_fill:
            raise RuntimeError(
                f"Driver field(s) still unselected after retries: {', '.join(missing_after_fill)}"
            )

        # When status is Active, carriers can require a state-specific license format.
        if await p.locator("#MainContent_txtDriversLicenseNumber").count() > 0:
            lic = (await p.locator("#MainContent_txtDriversLicenseNumber").input_value()).strip()
            if not lic:
                raw_license = (
                    self.contact.get("driverLicenseNumber")
                    or self.contact.get("driversLicenseNumber")
                    or self.contact.get("licenseNumber")
                    or self.contact.get("driver_license_number")
                    or ""
                )
                digits = re.sub(r"\D", "", str(raw_license))
                state = str(self.contact.get("state", "GA")).upper()
                if state == "GA":
                    # Georgia requires exactly 9 numeric digits.
                    lic_to_use = (digits or "123456789")[:9].zfill(9)
                else:
                    lic_to_use = digits or "123456789"
                await p.fill("#MainContent_txtDriversLicenseNumber", lic_to_use)

        if await p.locator("#MainContent_btnSaveDriver").count() > 0:
            # Final guard: status can be reset by late ASP.NET postbacks right before Save.
            await _force_active_license_status()
            await _force_losses_in_5_years_no()
            await p.click("#MainContent_btnSaveDriver")
            await p.wait_for_load_state("networkidle")
            if await p.locator("#lstErrors li").count() > 0:
                err = (await p.locator("#lstErrors").inner_text()).strip()
                if err:
                    self._log("WARN", f"Driver save returned validation error(s): {err}")
                    # Don't rely on exact server error wording; validate critical dropdowns directly.
                    needs_retry = False
                    if await p.locator("#MainContent_ddlDriversLicenseStatus").count() > 0:
                        st = (await p.locator("#MainContent_ddlDriversLicenseStatus").input_value()).strip()
                        if st in ("", "-1"):
                            needs_retry = True
                            self._log("WARN", "Driver license status reset by page; forcing Active")
                            await _force_active_license_status()
                    if await p.locator("#MainContent_ddlLossesIn5Years").count() > 0:
                        ls = (await p.locator("#MainContent_ddlLossesIn5Years").input_value()).strip()
                        if ls in ("", "-1"):
                            needs_retry = True
                            self._log("WARN", "Losses-in-5-years reset by page; forcing No/False")
                            await _force_losses_in_5_years_no()
                    if needs_retry:
                        self._log("WARN", "Re-saving driver after correcting required dropdown values")
                        await p.click("#MainContent_btnSaveDriver")
                        await p.wait_for_load_state("networkidle")
                        if await p.locator("#lstErrors li").count() > 0:
                            err = (await p.locator("#lstErrors").inner_text()).strip()
                    raise RuntimeError(f"Driver save blocked by validation: {err}")

        continue_btn = p.locator("#MainContent_btnContinue, input[name$='btnContinue']").first

        async def _safe_delete_driver_rows(max_attempts: int = 5) -> None:
            """
            Best-effort cleanup for placeholder driver rows.
            Delete postbacks can be flaky; never fail Driver step on delete timeout.
            """
            if await p.locator("#MainContent_gvDrivers").count() == 0:
                return
            for _ in range(max_attempts):
                delete_links = p.locator("#MainContent_gvDrivers a:has-text('Delete')")
                if await delete_links.count() == 0:
                    return
                try:
                    await delete_links.first.click(timeout=7000)
                except Exception:
                    # Fallback to JS click when Playwright click waits on flaky navigation.
                    try:
                        await p.evaluate(
                            """() => {
                                const el = document.querySelector("#MainContent_gvDrivers a");
                                if (el && /delete/i.test(el.textContent || "")) el.click();
                            }"""
                        )
                    except Exception:
                        self._log("WARN", "Driver row delete skipped due to unstable postback")
                        return
                try:
                    await p.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    # Some postbacks complete without reaching networkidle; continue best-effort.
                    await p.wait_for_timeout(500)

        # Remove extra prefill drivers that have a Delete action (typically uninitialized),
        # otherwise downstream Vehicle step can be blocked by driver validation.
        await _safe_delete_driver_rows()

        # T&C modal: brief says click the T&C link/legend to open, then check box.
        try:
            tc_triggers = p.locator(
                "#MainContent_pnlConnectedDriverAgreement legend, "
                "#MainContent_dvConnectedDriverAgreement, "
                "a:has-text('Terms and Conditions'), "
                "a:has-text('Terms & Conditions')"
            )
            if await tc_triggers.count() > 0:
                try:
                    await tc_triggers.first.click(timeout=5000)
                    await p.wait_for_timeout(500)
                except Exception:
                    pass

            agreement = p.locator("#MainContent_chkConnectedDriverAgreement")
            if await agreement.count() > 0:
                if not await agreement.is_checked():
                    await agreement.check(timeout=8000)
                    await p.wait_for_load_state("networkidle")
                if not await agreement.is_checked():
                    raise RuntimeError("Connected Driver Agreement checkbox did not remain checked")
                self._log("OK", "Connected Driver Agreement checked")
        except Exception as exc:
            self._log("WARN", f"Connected Driver Agreement skipped: {exc}")

        async def _click_next() -> None:
            # Driver pages can alternate between edit and schedule layouts.
            # Try common Next/Continue controls first, then try Save (never Cancel bounce-back).
            next_selectors = (
                "#MainContent_btnContinue",
                "input[name$='btnContinue']",
                "input[value='Next']",
                "button:has-text('Next')",
                "button:has-text('Continue')",
            )
            for sel in next_selectors:
                loc = p.locator(sel).first
                try:
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.click(timeout=10000)
                        await p.wait_for_load_state("networkidle")
                        return
                except Exception:
                    continue

            # If still in edit form with Save visible, persist first and then look for Continue.
            if await p.locator("#MainContent_btnSaveDriver").count() > 0:
                try:
                    await _force_active_license_status()
                    await _force_losses_in_5_years_no()
                    await p.click("#MainContent_btnSaveDriver", timeout=10000)
                    await p.wait_for_load_state("networkidle")
                except Exception:
                    pass
                for sel in ("#MainContent_btnContinue", "input[name$='btnContinue']", "input[value='Next']"):
                    loc = p.locator(sel).first
                    try:
                        if await loc.count() > 0 and await loc.is_visible():
                            await loc.click(timeout=10000)
                            await p.wait_for_load_state("networkidle")
                            return
                    except Exception:
                        continue

            raise RuntimeError("Driver step could not find a clickable Next/Continue control after save retry")

        await _click_next()
        if "DriverInfo" in p.url and await p.locator("#lstErrors li").count() > 0:
            err = (await p.locator("#lstErrors").inner_text()).strip()
            if "All driver information must be completed" in err:
                # Recovery path: remove any remaining deletable rows and retry Next once.
                await _safe_delete_driver_rows()
                await _click_next()

    async def _fill_vehicles(self) -> None:
        p = self._p()
        vehicles = [v for v in self.contact.get("vehicles", []) if isinstance(v, dict)]
        ui_vehicle_count = await p.locator("a[id^='MainContent_rpVehicles_btnViewEdit_']").count()
        num_from_contact = self.contact.get("num_vehicles")
        if num_from_contact is not None:
            try:
                num_from_contact = int(num_from_contact)
            except (ValueError, TypeError):
                num_from_contact = None
        total = max(num_from_contact or 0, len(vehicles), ui_vehicle_count, 1)

        async def _go_to_vehicle_list() -> None:
            for _ in range(5):
                if await p.locator("a[id^='MainContent_rpVehicles_btnViewEdit_']").count() > 0:
                    return
                if await p.locator("#MainContent_btnCancelVehicle").count() > 0:
                    await p.click("#MainContent_btnCancelVehicle")
                    await p.wait_for_load_state("networkidle")
                    continue
                if await p.locator("#MainContent_btnCancelVehicleCov").count() > 0:
                    await p.click("#MainContent_btnCancelVehicleCov")
                    await p.wait_for_load_state("networkidle")
                    continue
                break

        async def _edit_form_visible() -> bool:
            return (
                await p.locator("#MainContent_btnSaveVehicle").count() > 0
                and await p.locator("#MainContent_ucVehicleInfo_dvVehicleInput").count() > 0
            )

        async def _click_row_link(selector: str) -> None:
            last_err: Exception | None = None
            for _ in range(4):
                try:
                    await p.wait_for_load_state("domcontentloaded")
                except Exception:
                    pass
                link = p.locator(selector).first
                if await link.count() == 0:
                    await p.wait_for_timeout(300)
                    continue
                try:
                    await link.click(timeout=12000)
                    await p.wait_for_load_state("networkidle")
                    return
                except Exception as exc:
                    last_err = exc
                    # ASP.NET row links can ignore Playwright click during partial postback; JS fallback.
                    try:
                        await p.evaluate(
                            """(sel) => {
                                const el = document.querySelector(sel);
                                if (el) el.click();
                            }""",
                            selector,
                        )
                        await p.wait_for_load_state("networkidle")
                        return
                    except Exception:
                        pass
                    await p.wait_for_timeout(400)
            raise RuntimeError(f"Could not click row link {selector}: {last_err}")

        async def _active_edit_unit() -> int | None:
            try:
                # Some renders keep hdEditUnitNum blank while still on edit form.
                if await p.locator("#MainContent_hdEditUnitNum").count() > 0:
                    raw = (await p.locator("#MainContent_hdEditUnitNum").input_value()).strip()
                    if raw:
                        return int(raw)
                if await p.locator("#MainContent_hdVehicleNum").count() > 0:
                    raw_vehicle = (await p.locator("#MainContent_hdVehicleNum").input_value()).strip()
                    if raw_vehicle:
                        return int(raw_vehicle)
                return None
            except (ValueError, Exception):
                return None

        async def _wait_for_edit_unit(target: int, timeout_ms: int = 8000) -> bool:
            deadline = time.monotonic() + (timeout_ms / 1000.0)
            while time.monotonic() < deadline:
                try:
                    await p.wait_for_load_state("networkidle")
                except Exception:
                    pass
                active = await _active_edit_unit()
                if active == target:
                    return True
                await p.wait_for_timeout(300)
            return False

        async def _open_vehicle_edit_unit(idx: int) -> None:
            target_unit = idx + 1
            for attempt in range(5):
                await _go_to_vehicle_list()
                if await p.locator(f"#MainContent_rpVehicles_btnViewEdit_{idx}").count() == 0:
                    await p.wait_for_timeout(400)
                    continue
                selector = f"#MainContent_rpVehicles_btnViewEdit_{idx}"
                self._log("INFO", f"Vehicle {target_unit}: open attempt {attempt + 1} using primary click")
                try:
                    await p.locator(selector).first.scroll_into_view_if_needed()
                except Exception:
                    pass

                click_error: Exception | None = None
                try:
                    await _click_row_link(selector)
                except Exception as exc:
                    click_error = exc

                if await _wait_for_edit_unit(target_unit):
                    self._log("INFO", f"Vehicle {target_unit}: opened via primary click path")
                    return
                if await _edit_form_visible():
                    # Some renders keep hidden unit fields stale/blank; form visibility confirms edit mode.
                    self._log("INFO", f"Vehicle {target_unit}: opened via primary click (form visible fallback)")
                    return

                # JS click fallback on the exact row link.
                self._log("INFO", f"Vehicle {target_unit}: trying JS click fallback")
                try:
                    await p.evaluate(
                        """(vehicleIdx) => {
                            const id = `MainContent_rpVehicles_btnViewEdit_${vehicleIdx}`;
                            const link = document.getElementById(id);
                            if (link) link.click();
                        }""",
                        idx,
                    )
                except Exception as exc:
                    click_error = click_error or exc

                if await _wait_for_edit_unit(target_unit):
                    self._log("INFO", f"Vehicle {target_unit}: opened via JS click fallback")
                    return
                if await _edit_form_visible():
                    self._log("INFO", f"Vehicle {target_unit}: opened via JS click (form visible fallback)")
                    return

                # Last fallback: invoke ASP.NET __doPostBack from href if present.
                self._log("INFO", f"Vehicle {target_unit}: trying __doPostBack fallback")
                try:
                    await p.evaluate(
                        """(vehicleIdx) => {
                            const id = `MainContent_rpVehicles_btnViewEdit_${vehicleIdx}`;
                            const link = document.getElementById(id);
                            const href = (link && link.getAttribute("href")) || "";
                            const m = href.match(/__doPostBack\\('([^']+)'\\s*,\\s*'([^']*)'\\)/i);
                            if (m && typeof window.__doPostBack === "function") {
                                window.__doPostBack(m[1], m[2]);
                            }
                        }""",
                        idx,
                    )
                except Exception as exc:
                    click_error = click_error or exc

                if await _wait_for_edit_unit(target_unit):
                    self._log("INFO", f"Vehicle {target_unit}: opened via __doPostBack fallback")
                    return
                if await _edit_form_visible():
                    self._log("INFO", f"Vehicle {target_unit}: opened via __doPostBack (form visible fallback)")
                    return

                await _go_to_vehicle_list()
                if click_error:
                    self._log("WARN", f"Vehicle {target_unit} attempt {attempt + 1} failed: {click_error}")
            raise RuntimeError(
                f"Could not open View/Edit for Vehicle {target_unit} (still on edit unit {await _active_edit_unit()})"
            )

        async def _force_select(selector: str, value: str, label: str | None = None) -> None:
            if await p.locator(selector).count() == 0:
                return
            field = p.locator(selector)
            try:
                await field.select_option(value=value)
            except Exception:
                if label:
                    await field.select_option(label=label)
                else:
                    raise
            await p.wait_for_load_state("networkidle")

        async def _fill_vehicle_base(v: dict) -> None:
            async def _postback(event_target: str) -> None:
                try:
                    await p.evaluate(
                        """(target) => {
                            if (typeof window.__doPostBack === "function") {
                                window.__doPostBack(target, "");
                            }
                        }""",
                        event_target,
                    )
                    await p.wait_for_load_state("networkidle")
                except Exception:
                    pass

            async def _select_vehicle_dropdown(
                selector: str,
                *,
                preferred: str | None = None,
                postback_target: str | None = None,
            ) -> None:
                if await p.locator(selector).count() == 0:
                    return
                loc = p.locator(selector).first
                options = await loc.evaluate(
                    """el => Array.from(el.options || []).map(o => ({
                        value: (o.value || "").trim(),
                        label: (o.textContent || "").trim()
                    }))"""
                )
                valid = [o for o in options if o.get("value") and o.get("value") != "-1"]
                if not valid:
                    if postback_target:
                        await _postback(postback_target)
                    return

                chosen_value: str | None = None
                pref = (preferred or "").strip().lower()
                if pref:
                    for o in valid:
                        if o["value"].strip().lower() == pref:
                            chosen_value = o["value"]
                            break
                    if not chosen_value:
                        for o in valid:
                            if o["label"].strip().lower() == pref:
                                chosen_value = o["value"]
                                break
                    if not chosen_value:
                        for o in valid:
                            if pref in o["label"].strip().lower():
                                chosen_value = o["value"]
                                break
                if not chosen_value:
                    chosen_value = valid[0]["value"]

                await loc.select_option(value=chosen_value)
                await p.wait_for_load_state("networkidle")
                if postback_target:
                    await _postback(postback_target)

            year_raw = (
                v.get("year")
                or v.get("model_year")
                or v.get("vehicle_year")
                or v.get("Year")
                or ""
            )
            year_digits = re.sub(r"\D", "", str(year_raw))
            if len(year_digits) >= 4:
                year_to_use = year_digits[:4]
            else:
                year_to_use = str(datetime.now().year - 1)
            if await p.locator("#MainContent_ucVehicleInfo_txtYear").count() > 0:
                year_field = p.locator("#MainContent_ucVehicleInfo_txtYear").first
                await year_field.fill(year_to_use)
                await year_field.evaluate(
                    """el => {
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        el.blur();
                    }"""
                )
                await _postback("ctl00$MainContent$ucVehicleInfo$txtYear")

            await _select_vehicle_dropdown(
                "#MainContent_ucVehicleInfo_ddlSuspensionIndicator",
                preferred="False",
            )
            await _select_vehicle_dropdown(
                "#MainContent_ucVehicleInfo_ddlMake",
                preferred=str(v.get("make", "") or ""),
                postback_target="ctl00$MainContent$ucVehicleInfo$ddlMake",
            )
            await _select_vehicle_dropdown(
                "#MainContent_ucVehicleInfo_ddlModel",
                preferred=str(v.get("model", "") or ""),
                postback_target="ctl00$MainContent$ucVehicleInfo$ddlModel",
            )

            await _select_vehicle_dropdown(
                "#MainContent_ucVehicleInfo_ddlAutoRentedToOthers",
                preferred="False",
            )
            await _force_select("#MainContent_ucVehicleInfo_ddlVehicleWeightBetween14kAnd16k", "False", "No")
            await _force_select("#MainContent_ucVehicleInfo_ddlCamperIncluded", "False", "No")
            await _force_select(
                "#MainContent_ucVehicleInfo_ddlOwnershipStatus",
                str(v.get("ownership_status", 3)),
                "Own",
            )
            if await p.locator("#MainContent_ucVehicleInfo_txtAnnualMileage").count() > 0:
                await p.fill(
                    "#MainContent_ucVehicleInfo_txtAnnualMileage",
                    str(v.get("annual_mileage", 10000)),
                )

        async def _fill_vehicle_purchase(v: dict) -> None:
            purchase_raw = (
                v.get("purchase_date")
                or v.get("purchaseDate")
                or v.get("date_purchased")
                or v.get("datePurchased")
                or "03/01/2024"
            )
            purchase = self._date(str(purchase_raw))
            if await p.locator("#MainContent_ucVehicleInfo_txtPurchaseDate").count() > 0:
                field = p.locator("#MainContent_ucVehicleInfo_txtPurchaseDate")
                # Date picker fields can require keyboard-style entry plus blur/change.
                await field.click()
                await field.fill("")
                await field.type(purchase, delay=30)
                await field.evaluate(
                    """el => {
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        el.blur();
                    }"""
                )

        async def _ensure_purchase_if_visible(v: dict) -> None:
            if await p.locator("#MainContent_ucVehicleInfo_txtPurchaseDate").count() == 0:
                return
            current = (await p.locator("#MainContent_ucVehicleInfo_txtPurchaseDate").input_value()).strip()
            if current:
                return
            await _fill_vehicle_purchase(v)

        async def _click_until(
            selector: str,
            success_check,
            attempts: int = 4,
            delay_ms: int = 500,
            click_first: bool = False,
        ) -> None:
            async def _ok() -> bool:
                result = success_check()
                if inspect.isawaitable(result):
                    return bool(await result)
                return bool(result)

            last_err: Exception | None = None
            for attempt in range(attempts):
                if not (click_first and attempt == 0) and await _ok():
                    return
                try:
                    await p.locator(selector).first.click(timeout=12000)
                    await p.wait_for_load_state("networkidle")
                except Exception as exc:
                    last_err = exc
                if await _ok():
                    return
                await p.wait_for_timeout(delay_ms)
            details: list[str] = []
            if await p.locator("#lstErrors").count() > 0:
                err_text = (await p.locator("#lstErrors").inner_text()).strip()
                if err_text:
                    details.append(f"errors={err_text!r}")
            if await p.locator("#MainContent_ucVehicleInfo_txtPurchaseDate").count() > 0:
                purchase_now = (await p.locator("#MainContent_ucVehicleInfo_txtPurchaseDate").input_value()).strip()
                details.append(f"purchase_date={purchase_now!r}")
            suffix = f" Details: {', '.join(details)}" if details else ""
            raise RuntimeError(
                f"Action did not stick after {attempts} attempts: {selector}. Last error: {last_err}.{suffix}"
            )

        async def _save_vehicle_coverages() -> None:
            # Set all 5 coverages from the brief JSON mapping.
            coverage_fields = [
                ("[id*='ddlCovCOMP_']", "1000"),
                ("[id*='ddlCovRoadside_']", "-1"),
                ("[id*='ddlCovCUST_']", "-1"),
                ("[id*='ddlCovAV_']", "-1"),
                ("[id*='ddlCovRENT_']", "-1"),
            ]
            for cov_sel, cov_val in coverage_fields:
                cov_loc = p.locator(cov_sel).first
                if await cov_loc.count() > 0:
                    try:
                        await cov_loc.select_option(cov_val)
                    except Exception:
                        pass

            async def _cov_saved() -> bool:
                return (
                    await p.locator("#MainContent_btnSaveVehicleCov").count() == 0
                    or await p.locator("a[id^='MainContent_rpVehicles_btnViewEdit_']").count() > 0
                )
            await _click_until(
                "#MainContent_btnSaveVehicleCov",
                _cov_saved,
                click_first=True,
            )

        async def _veh_saved() -> bool:
            return (
                await p.locator("#MainContent_btnSaveVehicle").count() == 0
                or await p.locator("#MainContent_btnSaveVehicleCov").count() > 0
            )

        for idx in range(total):
            v = vehicles[idx] if idx < len(vehicles) else {}
            label = f"Vehicle {idx + 1}/{total}"

            row_selector = f"#MainContent_rpVehicles_btnViewEdit_{idx}"
            row_found = False
            for _ in range(8):
                if await p.locator(row_selector).count() > 0:
                    row_found = True
                    break
                await _go_to_vehicle_list()
                await p.wait_for_timeout(300)
            if not row_found:
                raise RuntimeError(f"{label}: View/Edit row not found")
            await _open_vehicle_edit_unit(idx)
            if not await _edit_form_visible():
                raise RuntimeError(f"{label}: edit form not visible after View/Edit")

            # Step 1: Fill base fields -> Save Vehicle -> page reload.
            await _fill_vehicle_base(v)
            await _ensure_purchase_if_visible(v)
            await _click_until("#MainContent_btnSaveVehicle", _veh_saved, click_first=True)
            self._log("OK", f"{label}: base saved")

            # Step 2: Fill purchase date -> Save Vehicle -> page reload.
            # After first save, portal often returns to list; reopen same vehicle edit explicitly.
            if await p.locator("#MainContent_ucVehicleInfo_txtPurchaseDate").count() == 0:
                await _go_to_vehicle_list()
                await _open_vehicle_edit_unit(idx)
            if await p.locator("#MainContent_ucVehicleInfo_txtPurchaseDate").count() > 0:
                await _fill_vehicle_purchase(v)
                await _click_until("#MainContent_btnSaveVehicle", _veh_saved, click_first=True)
                # If carrier still flags purchase date, retry with compact MMDDYYYY format once.
                if await p.locator("#MainContent_ucVehicleInfo_txtPurchaseDate.ctlError").count() > 0:
                    purchase_retry_raw = (
                        v.get("purchase_date")
                        or v.get("purchaseDate")
                        or v.get("date_purchased")
                        or v.get("datePurchased")
                        or "03/01/2024"
                    )
                    compact_purchase = self._date(str(purchase_retry_raw)).replace("/", "")
                    field = p.locator("#MainContent_ucVehicleInfo_txtPurchaseDate")
                    await field.click()
                    await field.fill("")
                    await field.type(compact_purchase, delay=30)
                    await field.evaluate(
                        """el => {
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                            el.blur();
                        }"""
                    )
                    await _click_until("#MainContent_btnSaveVehicle", _veh_saved, click_first=True)
                self._log("OK", f"{label}: purchase date saved")

            # Step 3: Set coverages -> Save Vehicle Coverages -> page reload.
            if await p.locator("#MainContent_btnSaveVehicleCov").count() > 0:
                await _save_vehicle_coverages()
                self._log("OK", f"{label}: coverages saved")
            else:
                await _go_to_vehicle_list()
                if await p.locator(f"#MainContent_rpVehicles_btnCoverages_{idx}").count() > 0:
                    try:
                        await _click_row_link(f"#MainContent_rpVehicles_btnCoverages_{idx}")
                        if await p.locator("#MainContent_btnSaveVehicleCov").count() > 0:
                            await _save_vehicle_coverages()
                            self._log("OK", f"{label}: coverages saved")
                    except Exception as exc:
                        # Do not block next vehicle processing if row-coverages click is flaky.
                        self._log("WARN", f"{label}: coverages click skipped ({exc})")

            await _go_to_vehicle_list()

        await _go_to_vehicle_list()
        next_btn = p.locator("#MainContent_btnContinue").first
        for _ in range(4):
            # Some flows land on an intermediate auto coverage grid before Auto Underwriting.
            # Set UM/UIM PD as required, then continue.
            if await p.locator("#MainContent_ucAutoPolicy_rptCoreCoverages_ddlCovUMUIMPD_7").count() > 0:
                umpd = p.locator("#MainContent_ucAutoPolicy_rptCoreCoverages_ddlCovUMUIMPD_7").first
                current = (await umpd.input_value()).strip()
                if current in ("", "-1"):
                    try:
                        await umpd.select_option("25000/250")
                    except Exception:
                        await umpd.select_option(label="Included - 25,000 (250 Ded)")
                    await p.wait_for_load_state("networkidle")
            try:
                await next_btn.click(timeout=15000)
                await p.wait_for_load_state("networkidle")
            except Exception:
                await p.wait_for_timeout(500)
            if "VehicleInfo" not in p.url:
                break

        # Recovery: if carrier points to a specific vehicle N, process that one again.
        if "VehicleInfo" in p.url and await p.locator("#lstErrors li").count() > 0:
            err = (await p.locator("#lstErrors").inner_text()).strip()
            m = re.search(r"Vehicle\s+(\d+)", err, flags=re.IGNORECASE)
            if m:
                recovery_idx = max(int(m.group(1)) - 1, 0)
                rv = vehicles[recovery_idx] if recovery_idx < len(vehicles) else {}
                self._log("WARN", f"Vehicle error recovery for Vehicle {recovery_idx + 1}: {err}")
                await _go_to_vehicle_list()
                if await p.locator(f"#MainContent_rpVehicles_btnViewEdit_{recovery_idx}").count() > 0:
                    await _open_vehicle_edit_unit(recovery_idx)
                    await _fill_vehicle_base(rv)
                    await _ensure_purchase_if_visible(rv)
                    await _click_until("#MainContent_btnSaveVehicle", _veh_saved, click_first=True)
                    if await p.locator("#MainContent_ucVehicleInfo_txtPurchaseDate").count() > 0:
                        await _fill_vehicle_purchase(rv)
                        await _click_until("#MainContent_btnSaveVehicle", _veh_saved, click_first=True)
                    if await p.locator("#MainContent_btnSaveVehicleCov").count() > 0:
                        await _save_vehicle_coverages()
                await _go_to_vehicle_list()
                if await p.locator(f"#MainContent_rpVehicles_btnCoverages_{recovery_idx}").count() > 0:
                    await _click_row_link(f"#MainContent_rpVehicles_btnCoverages_{recovery_idx}")
                    if await p.locator("#MainContent_btnSaveVehicleCov").count() > 0:
                        await _save_vehicle_coverages()
                await _go_to_vehicle_list()
                for _ in range(4):
                    await p.click("#MainContent_btnContinue")
                    await p.wait_for_load_state("networkidle")
                    if "VehicleInfo" not in p.url:
                        break

    async def _fill_auto_underwriting(self) -> None:
        p = self._p()
        await p.select_option("#MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_0", "Individual")
        await p.select_option("#MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_2", "False")
        await p.select_option("#MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_3", "False")
        await p.fill("#MainContent_ucAutoQuestions_rpParentQuestions_txtAnswer_4", "1")

        # Fill any remaining auto UW selects/inputs with safe defaults.
        for i in range(10):
            ddl = f"#MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_{i}"
            if await p.locator(ddl).count() > 0:
                cur = (await p.locator(ddl).input_value()).strip()
                if cur in ("", "-1"):
                    options = await p.locator(ddl).evaluate(
                        "el => Array.from(el.options).map(o => o.value)"
                    )
                    if "False" in options:
                        await p.locator(ddl).select_option("False")
                    elif len(options) > 1:
                        await p.locator(ddl).select_option(options[1])
            txt = f"#MainContent_ucAutoQuestions_rpParentQuestions_txtAnswer_{i}"
            if await p.locator(txt).count() > 0:
                cur = (await p.locator(txt).input_value()).strip()
                if not cur:
                    await p.fill(txt, "1")

        await asyncio.to_thread(
            input,
            "\n[PAUSE] Auto Underwriting ready. Press Enter to click Next...",
        )
        await p.click("#MainContent_btnContinue")
        await p.wait_for_load_state("networkidle")
        await asyncio.to_thread(
            input,
            "\n[PAUSE] Auto Underwriting submitted. Press Enter to continue...",
        )

    async def _click_continue(self) -> None:
        p = self._p()
        await p.click("#MainContent_btnContinue")
        await p.wait_for_load_state("networkidle")

    async def _handle_two_factor_if_present(self) -> None:
        p = self._p()

        # This site can show either:
        # 1) A "choose SMS / Email" 2FA page (#loginWith2faSms / #loginWith2faEmail), OR
        # 2) The direct SMS verification page (#TwoFactorCode / #verifyButton).
        has_choice_screen = await p.locator("#loginWith2faSms, #loginWith2faEmail").count() > 0
        has_code_screen = await p.locator("#TwoFactorCode, #verifyButton").count() > 0

        # If neither exists, 2FA is not currently shown.
        if not has_choice_screen and not has_code_screen:
            return

        self._log("INFO", f"2FA detected on {p.url}")

        # If we are on the choice screen, move to SMS code screen first.
        if has_choice_screen and not has_code_screen:
            if await p.locator("#loginWith2faSms").count() > 0:
                await p.click("#loginWith2faSms")
                self._log("INFO", "Clicked SMS 2FA option")
            elif await p.locator("#loginWith2faEmail").count() > 0:
                await p.click("#loginWith2faEmail")
                self._log("WARN", "SMS option missing, used Email 2FA option")
            else:
                raise RuntimeError("2FA choice page detected but no SMS/Email option found")

        # Wait until code entry controls are visible.
        await p.wait_for_selector("#TwoFactorCode, #verifyButton", state="visible", timeout=20000)

        code = self._retrieve_2fa_code_from_imessage(
            timeout=self.otp_timeout,
            initial_wait=self.otp_initial_wait,
        )
        if not code:
            raise RuntimeError("2FA code not found in iMessage within timeout")

        self._log("INFO", f"2FA code retrieved ({len(code)} digits)")
        await self._submit_two_factor_code(code)
        self._log("OK", "2FA code submitted")

    # ── Extract & Download ────────────────────────────────────────────

    async def _extract_premiums(self) -> tuple[str, str, str]:
        p = self._p()
        await p.select_option("#MainContent_ucPaymentMethod_ddlPayMethod", "AS")
        try:
            await p.select_option("#MainContent_ucPaymentMethod_ddlPayPlan", "PIF")
        except Exception:
            pass
        await p.click("#MainContent_ucRater_btnRate")
        await p.wait_for_load_state("networkidle")

        html = await p.content()

        # Detect ineligibility before extracting premiums.
        ineligible_patterns = re.compile(
            r"not\s+eligible|ineligible|application\s+declined|cannot\s+be\s+rated",
            re.IGNORECASE,
        )
        page_text = await p.locator("body").inner_text()
        ineligible_match = ineligible_patterns.search(page_text)
        if ineligible_match:
            snippet = page_text[max(0, ineligible_match.start() - 40):ineligible_match.end() + 80].strip()
            raise RuntimeError(f"not_eligible: {snippet}")

        amounts = re.findall(r"\$\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})?", html)
        if not amounts:
            return "$0.00", "$0.00", "$0.00"
        total = amounts[0]
        home = amounts[1] if len(amounts) > 1 else "$0.00"
        auto = amounts[2] if len(amounts) > 2 else "$0.00"
        return total, home, auto

    async def _download_pdf(self) -> str:
        p = self._p()
        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        cid = self.contact.get("id", "unknown")
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        pdf_path = str(ARTIFACTS_DIR / f"quote_{cid}_{ts}.pdf")

        try:
            async with p.expect_download(timeout=10000) as dl:
                await p.click("#MainContent_btnQuoteProposal")
            await (await dl.value).save_as(pdf_path)
            return pdf_path
        except Exception:
            pass

        try:
            async with p.expect_popup(timeout=10000) as pop:
                await p.click("#MainContent_btnQuoteProposal")
            popup = await pop.value
            await popup.wait_for_load_state("networkidle", timeout=10000)
            await popup.pdf(path=pdf_path, print_background=True)
            await popup.close()
            return pdf_path
        except Exception:
            pass

        try:
            await p.pdf(path=pdf_path, print_background=True)
            return pdf_path
        except Exception:
            pass

        # Retry: Re-Rate then Quote Proposal once more before giving up.
        self._log("WARN", "PDF first attempt failed, retrying after Re-Rate...")
        try:
            await p.click("#MainContent_ucRater_btnRate")
            await p.wait_for_load_state("networkidle")
            async with p.expect_download(timeout=15000) as dl:
                await p.click("#MainContent_btnQuoteProposal")
            await (await dl.value).save_as(pdf_path)
            return pdf_path
        except Exception:
            pass

        try:
            async with p.expect_popup(timeout=15000) as pop:
                await p.click("#MainContent_btnQuoteProposal")
            popup = await pop.value
            await popup.wait_for_load_state("networkidle", timeout=15000)
            await popup.pdf(path=pdf_path, print_background=True)
            await popup.close()
            return pdf_path
        except Exception as exc:
            raise RuntimeError("Could not capture quote PDF after retry") from exc

    def _retrieve_2fa_code_from_imessage(
        self,
        timeout: int = 90,
        check_interval: int = 2,
        initial_wait: int = 60,
    ) -> str | None:
        db_path = Path.home() / "Library" / "Messages" / "chat.db"
        if not db_path.exists():
            self._log("WARN", f"Messages DB not found: {db_path}")
            return None

        # Apple epoch starts at 2001-01-01.
        apple_epoch = 978307200
        start_time = time.time()

        # Wait for the OTP SMS to arrive before first DB read.
        if initial_wait > 0:
            self._log("INFO", f"Waiting {initial_wait}s before reading iMessage for OTP...")
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
                    (self.otp_sender, threshold, threshold_ns),
                )
                rows = cursor.fetchall()
                conn.close()

                if rows:
                    # Use the newest 6-digit code from recent inbound messages.
                    for _, txt in rows:
                        latest_text = txt or ""
                        match = re.search(r"\b(\d{6})\b", latest_text)
                        if match:
                            return match.group(1)

                elapsed = int(time.time() - start_time)
                self._log("INFO", f"Waiting for 2FA code from {self.otp_sender}... {elapsed}s")
                time.sleep(check_interval)
            except Exception as exc:
                self._log("WARN", f"2FA read error: {exc}")
                time.sleep(check_interval)

        self._log("WARN", "2FA code was not found before timeout. Dumping recent sender/code candidates...")
        self._log_recent_otp_candidates_from_imessage()
        return None

    def _log_recent_otp_candidates_from_imessage(self, lookback_minutes: int = 30, limit: int = 200) -> None:
        """
        Debug helper: print recent sender IDs and any detected 6-digit code.
        Useful when OTP cannot be found from the expected sender.
        """
        db_path = Path.home() / "Library" / "Messages" / "chat.db"
        if not db_path.exists():
            self._log("WARN", f"Messages DB not found for debug dump: {db_path}")
            return

        try:
            apple_epoch = 978307200
            current_apple_time = int(time.time()) - apple_epoch
            threshold = current_apple_time - (lookback_minutes * 60)
            threshold_ns = threshold * 1_000_000_000

            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT handle.id, message.text
                FROM message
                JOIN handle ON message.handle_id = handle.ROWID
                WHERE
                  message.is_from_me = 0
                  AND (message.date > ? OR message.date > ?)
                  AND message.text IS NOT NULL
                ORDER BY message.date DESC
                LIMIT ?
                """,
                (threshold, threshold_ns, limit),
            )
            rows = cursor.fetchall()
            conn.close()

            if not rows:
                self._log("WARN", "OTP debug dump: no recent inbound messages in lookback window")
                return

            seen: set[tuple[str, str]] = set()
            printed = 0
            for sender, txt in rows:
                sender_id = str(sender or "").strip()
                if not sender_id:
                    continue
                body = str(txt or "")
                match = re.search(r"\b(\d{6})\b", body)
                if not match:
                    continue
                code = match.group(1)
                key = (sender_id, code)
                if key in seen:
                    continue
                seen.add(key)
                printed += 1
                self._log("INFO", f"OTP candidate -> sender={sender_id}, code={code}")

            if printed == 0:
                self._log("WARN", "OTP debug dump: no 6-digit codes found in recent inbound messages")
        except Exception as exc:
            self._log("WARN", f"OTP debug dump failed: {exc}")

    async def _submit_two_factor_code(self, code: str) -> None:
        p = self._p()

        code_input = p.locator(
            "#TwoFactorCode, input[type='tel'], input[name*='code' i], input[id*='code' i], input[autocomplete='one-time-code']"
        ).first
        await code_input.wait_for(state="visible", timeout=20000)
        await code_input.fill(code)

        # Always remember this device so future runs can reuse session cookies.
        remember_machine = p.locator("#RememberMachine, input[name='RememberMachine'][type='checkbox']").first
        if await remember_machine.count() > 0:
            try:
                await remember_machine.check(timeout=5000)
                self._log("INFO", "Checked 'Remember this device' for 2FA")
            except Exception as exc:
                self._log("WARN", f"Could not check 'Remember this device': {exc}")

        submit = p.locator(
            "#verifyButton, button:has-text('Verify'), button:has-text('Submit'), button[type='submit'], input[type='submit']"
        ).first
        await submit.click(timeout=10000)
        try:
            await p.wait_for_url("**/MainMenu.aspx", timeout=30000)
        except Exception:
            # If still on 2FA page, surface inline validation text for faster debugging.
            if await p.locator("span[data-valmsg-for='TwoFactorCode']").count() > 0:
                err = (await p.locator("span[data-valmsg-for='TwoFactorCode']").first.inner_text()).strip()
                if err:
                    raise RuntimeError(f"2FA verify failed: {err}")
            raise

        await self._save_storage_state()

    # ── Helpers ───────────────────────────────────────────────────────

    def _p(self) -> Page:
        if not self.page:
            raise RuntimeError("Browser page not initialized")
        return self.page

    def _val(self, *keys: str) -> str:
        for k in keys:
            v = self.contact.get(k)
            if v is not None and str(v).strip():
                return str(v).strip()
        raise RuntimeError(f"Missing contact field: {keys}")

    def _date(self, raw: str) -> str:
        mmddyyyy = format_date_to_mmddyyyy(raw)
        return f"{mmddyyyy[0:2]}/{mmddyyyy[2:4]}/{mmddyyyy[4:8]}"

    async def _select_strict_dropdown(
        self,
        selector: str,
        value: str,
        *,
        fallback_value: str | None = "Other",
    ) -> bool:
        """
        Select by ``value`` or visible ``label``; on failure optionally fall back (default ``Other``).

        Returns:
            True if ``fallback_value`` was used because ``value`` did not match any option.
        """
        p = self._p()
        loc = p.locator(selector)
        await loc.wait_for(state="visible", timeout=15000)

        async def _try_pick(v: str) -> bool:
            try:
                await loc.select_option(value=v, timeout=10000)
                return True
            except Exception:
                pass
            try:
                await loc.select_option(label=v, timeout=10000)
                return True
            except Exception:
                return False

        if await _try_pick(value):
            return False
        if fallback_value is not None:
            if fallback_value != value:
                self._log(
                    "WARN",
                    f"Dropdown {selector}: no match for {value!r}, selecting fallback {fallback_value!r}",
                )
            if await _try_pick(fallback_value):
                return True
        # Years at residence only allows 0,1,2,3,99 — if still unmatched, force 3.
        if "YearsAtAddress" in selector and await _try_pick("3"):
            self._log(
                "WARN",
                f"Dropdown {selector}: fallback failed; selected last-resort '3'",
            )
            return True
        raise RuntimeError(
            f"Could not select dropdown option {value!r}"
            + (f" or fallback {fallback_value!r}" if fallback_value else "")
        )

    async def _type_slow(self, selector: str, text: str, delay: float = 120) -> None:
        field = self._p().locator(selector)
        await field.wait_for(state="visible", timeout=10000)
        await field.click()
        await field.fill("")
        await field.type(text, delay=delay, timeout=15000)

    async def _save_html(self, step: str) -> None:
        if not self.page:
            return
        try:
            ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
            name = re.sub(r"[^a-zA-Z0-9]+", "_", step).strip("_")
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            path = ARTIFACTS_DIR / f"fail_{name}_{ts}.html"
            path.write_text(await self.page.content(), encoding="utf-8")
            self._log("OK", f"HTML saved -> {path}")
        except Exception as exc:
            self._log("FAIL", f"Could not save HTML: {exc}")

    async def _save_screenshot(self, step: str) -> None:
        if not self.page:
            return
        try:
            ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
            name = re.sub(r"[^a-zA-Z0-9]+", "_", step).strip("_")
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            path = str(ARTIFACTS_DIR / f"fail_{name}_{ts}.png")
            await self.page.screenshot(path=path)
            self._log("OK", f"Screenshot saved -> {path}")
        except Exception as exc:
            self._log("FAIL", f"Could not save screenshot: {exc}")

    async def _save_storage_state(self) -> None:
        if not self.context:
            return
        try:
            ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
            await self.context.storage_state(path=str(STORAGE_STATE_PATH))
            self._log("OK", f"Auth state saved -> {STORAGE_STATE_PATH}")
        except Exception as exc:
            self._log("WARN", f"Could not save auth state: {exc}")

    @staticmethod
    def _log(level: str, msg: str) -> None:
        print(f"[bridge_bot][{level}] {msg}", flush=True)


# Backward-compatible alias
BridgeBot = NG360BridgeBot


# ── Smoke test ────────────────────────────────────────────────────────

async def _smoke_test() -> None:
    # GHL duplicate-search style payload: {"contact": {...}}.
    contact = {
        "contact": {
            "id": "qqcJSAf4ze0MVnvOONzn",
            "dateAdded": "2026-02-14T10:12:53.273Z",
            "type": "lead",
            "locationId": "Czwg7VWYU6myocqsb86R",
            "firstName": "John",
            "firstNameLowerCase": "john",
            "fullNameLowerCase": "john edwards",
            "lastName": "Edwards",
            "lastNameLowerCase": "edwards",
            "email": "chrkoltahoe@yahoo.com",
            "emailLowerCase": "chrkoltahoe@yahoo.com",
            "phone": "+19042386282",
            "address1": "159 College St",
            "city": "KINGSLAND",
            "state": "GA",
            "postalCode": "31548",
            "country": "US",
            "dateOfBirth": "1980-06-29",
            "timezone": "America/New_York",
            "followers": ["QFEe3vWj4EBUm7pZuRXK"],
            "tags": [
                "first sms sent",
                "texted recently",
                "wavv-no-answer",
                "customer replied",
                "sales question",
                "first call made",
            ],
            "dateUpdated": "2026-05-10T15:02:44.278Z",
            "assignedTo": "PDtxPdIWY1A6zhLjRGt2",
            "gender": "Male",
            "maritalStatus": "Married",
            "occupation": "OtherNonTechnical",
            "driverLicenseNumber": "123456789",
            "year_built": "1995",
            "square_footage": "2200",
            "number_of_stories": "2",
            "datePurchased": "2/14/2026",
            "effective_date": "",
            "prior_carrier_home": "PROGRESSIVE",
            "prior_expiration": "06/30/2026",
            "years_continuous_ins": 8,
            "years_at_residence": 48,
            "vehicles": [
                {
                    "year": "2019",
                    "make": "CHEVROLET",
                    "model": "SILVERADO K3500 HIGH COUNTRY  Distance Driven",
                    "submodel": "SILVERADO-PICKUP  Owned",
                    "vin_prefix": "1GNSKSKT0PR000000",
                    "ownership_status": 3,
                    "annual_mileage": 15000,
                    "purchase_date": "03/01/2024",
                },
                {
                    "year": "2023",
                    "make": "CHEVROLET",
                    "model": "TAHOE K1500 PREMIER  Distance Driven",
                    "submodel": "TAHOE-SPORT UTILITY VEHICLE  Owned",
                    "vin_prefix": "1GNSKSKT0PR000000",
                    "ownership_status": 3,
                    "annual_mileage": 15000,
                    "purchase_date": "03/01/2024",
                },
            ],
            "num_vehicles": 2,
            "customFields": [
                {"id": "0zTbyVEssvnAInozzBdz", "value": "6/29/1980"},
                {"id": "2vUp4BAImmIo8a8dsK7N", "value": "1GNSKSKT0PR000000"},
                {"id": "341K8Jgel1X5J5OrJm4m", "value": "CHEVROLET"},
                {"id": "8JFsLV5CMbvhzGyRNA5y", "value": "Good"},
                {"id": "AWMNPex2qSSme7YK5e4G", "value": "Male"},
                {"id": "C7istMeqbBJT06ZmM76P", "value": "CHEVROLET"},
                {"id": "DXe8dPAy1NzqkRYGsBw2", "value": "1GNSKSKT0PR000000"},
                {"id": "DdpYg3Lo5ioWcPyBnRgJ", "value": "2023"},
                {"id": "GbIVaeAE4oOe6xygsCp2", "value": "Married"},
                {"id": "IVPWhlbMASu1hAr6wd6H", "value": "SILVERADO-PICKUP  Owned"},
                {"id": "MWLLTPYeXjWbYvtl2sVV", "value": "Owned"},
                {"id": "MY5HS0NaK05DraTN2MZY", "value": "PROGRESSIVE"},
                {"id": "OtNh5tbboo0LMYzSemjk", "value": "48"},
                {"id": "WmXgvU3uyJaqYAVuUNy4", "value": "Commute_Work"},
                {"id": "aTgHXTOzecDIgTm94dtJ", "value": "TAHOE-SPORT UTILITY VEHICLE  Owned"},
                {"id": "angsInSy7gZlI1xoFz6E", "value": "Valid"},
                {"id": "auVJAvWpnnYYYzTo3oW9", "value": "2023"},
                {"id": "bHNx2ZBVrio8qURwWtex", "value": "15000"},
                {"id": "f4sPNa61EW2yFgEOnLgF", "value": "TAHOE K1500 PREMIER  Distance Driven"},
                {"id": "nklRSHs53FVK43manawu", "value": "Commute_Work"},
                {"id": "qclrLfVxDmRh3uv6bMUi", "value": "15000"},
                {"id": "seNja9CEi7TLhXew64WI", "value": "SILVERADO K3500 HIGH COUNTRY  Distance Driven"},
                {"id": "z9r6fDjPLXxMHOyD2avg", "value": "OtherNonTechnical"},
                {"id": "zAJbxgQsMSzsgrMawqow", "value": "2"},
                {"id": "aZHbdJXTOjdIssPV7sMa", "value": "2"},
                {"id": "jtIe9RfksX8OwZe1eZ45", "value": 1},
                {"id": "lFJu6utndWOIpT2PqAdX", "value": "0"},
                {"id": "D1gY1lj17CfHwUIKprgH", "value": 159.17},
                {"id": "c8kKkhrqtFZbQZDsSXhA", "value": "Progressive"},
                {"id": "PlMzlSNxxeatyDPujXJH", "value": 159.17},
                {"id": "jcjjpI2d6lKaHdYBvDqf", "value": 8},
            ],
            "additionalEmails": [],
            "additionalPhones": [],
        },
        "matchingField": "contactId",
    }

    print("[bridge_bot] Starting smoke test...")
    result = await NG360BridgeBot(contact).run()
    print("[bridge_bot] Result:")
    print(json.dumps(result, indent=2))
    print("[bridge_bot]", "PASS" if result.get("success") else "FAIL")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    asyncio.run(_smoke_test())
