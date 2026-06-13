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

from utils.data_formatter import format_date_to_mmddyyyy, split_phone_number

logger = logging.getLogger(__name__)

ARTIFACTS_DIR = Path("artifacts")
STORAGE_STATE_PATH = ARTIFACTS_DIR / "natgen_storage_state.json"

# Load variables from .env if present (without overriding exported shell vars).
load_dotenv()


class NG360BridgeBot:

    def __init__(self, contact: dict) -> None:
        self.contact = self._normalize_contact_payload(contact)
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
        await p.select_option("#MainContent_ucNamedInsured_ddlOccupation", self._val("occupation"))

        await p.select_option("#MainContent_ucContactInfo_ucPhoneNumber_ddlPhoneType", "Cell")
        await p.fill("#MainContent_ucContactInfo_ucPhoneNumber_txtAreaCode", area)
        await p.fill("#MainContent_ucContactInfo_ucPhoneNumber_txtPrefix", prefix)
        await p.fill("#MainContent_ucContactInfo_ucPhoneNumber_txtLineNumber", line)

        await p.fill("#MainContent_ucResidentialAddress_txtAddress", self._val("address1", "address"))
        await p.fill("#MainContent_ucResidentialAddress_txtCity", self._val("city"))
        await p.select_option("#MainContent_ddlYearsAtAddress", str(self.contact.get("years_at_residence", 5)))

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

    async def _fill_underwriting(self) -> None:
        p = self._p()
        expiry = self._date(str(self.contact.get("prior_expiration", "03/31/2026")))

        await p.select_option("#MainContent_ucPriorPolicyInformation_ddlPriorInsuranceCoverage", "Prior standard insurance")
        await p.select_option("#MainContent_ucPriorPolicyInformation_ddlPriorInsuranceCompany", str(self.contact.get("prior_carrier_home", "Allstate Ins Co")))
        await p.fill("#MainContent_ucPriorPolicyInformation_txtExpirationDate", expiry)
        await p.fill("#MainContent_ucPriorPolicyInformation_txtContinuousInsurance", str(self.contact.get("years_continuous_ins", 1)))
        await p.click("#MainContent_btnContinue")
        await p.wait_for_load_state("networkidle")

    async def _fill_coverage(self) -> None:
        p = self._p()
        await p.select_option("#MainContent_ddlPerils", "0.010")
        await p.select_option("#MainContent_ddlNamedStormDeductible", "0.020")
        await p.select_option("#MainContent_ddlWindstormDeductible", "0.020")
        await p.click("#MainContent_btnContinue")
        await p.wait_for_load_state("networkidle")

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
            for _ in range(2):
                await _select_if_present(selector, value, wait_postback=wait_postback, label_fallback=label)
                current = (await p.locator(selector).input_value()).strip()
                if current not in ("", "-1"):
                    return
            raise RuntimeError(f"Driver field still unselected after fill: {label}")

        async def _require_selected(selector: str, friendly: str) -> None:
            if await p.locator(selector).count() == 0:
                return
            current = (await p.locator(selector).input_value()).strip()
            if current in ("", "-1"):
                raise RuntimeError(f"Driver field still unselected after fill: {friendly}")

        # Match contractor brief sequence for DriverInfo page.
        await _select_if_present("#MainContent_ddlOperatorType", "Operator", wait_postback=True, label_fallback="Operator")
        await _set_required_select("#MainContent_ddlLossesIn5Years", "False", "Losses in 5 years")
        await _set_required_select("#MainContent_ddlDefensiveDriverCourse", "False", "Defensive Driver Course", wait_postback=True)
        await _select_if_present("#MainContent_ddlConnectedDriverOptIn", "False", label_fallback="No")
        await _set_required_select("#MainContent_ddlDriversLicenseStatus", "Active", "Driver License Status", wait_postback=True)
        await _select_if_present("#MainContent_ddlLicenseState", "GA")
        await _require_selected("#MainContent_ddlDriversLicenseStatus", "Driver License Status")
        await _require_selected("#MainContent_ddlDefensiveDriverCourse", "Defensive Driver Course")
        await _require_selected("#MainContent_ddlLossesIn5Years", "Losses in 5 Years")

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
            await p.click("#MainContent_btnSaveDriver")
            await p.wait_for_load_state("networkidle")
            if await p.locator("#lstErrors li").count() > 0:
                err = (await p.locator("#lstErrors").inner_text()).strip()
                if err:
                    raise RuntimeError(f"Driver save blocked by validation: {err}")

        continue_btn = p.locator("#MainContent_btnContinue, input[name$='btnContinue']").first
        # Edit form has Save/Cancel (no visible Next). Return to schedule page first.
        if not await continue_btn.is_visible() and await p.locator("#MainContent_btnCancel").count() > 0:
            await p.click("#MainContent_btnCancel")
            await p.wait_for_load_state("networkidle")
            try:
                await p.wait_for_selector("#MainContent_gvDrivers", state="visible", timeout=15000)
            except PlaywrightTimeoutError:
                pass

        # Remove extra prefill drivers that have a Delete action (typically uninitialized),
        # otherwise downstream Vehicle step can be blocked by driver validation.
        if await p.locator("#MainContent_gvDrivers").count() > 0:
            for _ in range(5):
                delete_links = p.locator("#MainContent_gvDrivers a:has-text('Delete')")
                if await delete_links.count() == 0:
                    break
                try:
                    await delete_links.first.click(timeout=10000)
                    await p.wait_for_load_state("networkidle")
                except Exception:
                    break

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
            if await continue_btn.is_visible():
                await continue_btn.click()
            else:
                await p.click("input[value='Next'], #MainContent_btnContinue")
            await p.wait_for_load_state("networkidle")

        await _click_next()
        if "DriverInfo" in p.url and await p.locator("#lstErrors li").count() > 0:
            err = (await p.locator("#lstErrors").inner_text()).strip()
            if "All driver information must be completed" in err:
                # Recovery path: remove any remaining deletable rows and retry Next once.
                for _ in range(5):
                    delete_links = p.locator("#MainContent_gvDrivers a:has-text('Delete')")
                    if await delete_links.count() == 0:
                        break
                    await delete_links.first.click(timeout=10000)
                    await p.wait_for_load_state("networkidle")
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
            await _force_select("#MainContent_ucVehicleInfo_ddlAutoRentedToOthers", "False", "No")
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
            "id": "smoke-test",
            "locationId": "Czwg7VWYU6myocqsb86R",
            "contactName": "Matthew Mgbeke",
            "firstName": "Matthew",
            "lastName": "Mgbeke",
            "email": "mattmgb2005@yahoo.com",
            "phone": "+16626076394",
            "city": "Acworth",
            "state": "GA",
            "postalCode": "30101",
            "address1": "230 Hickory Pointe Dr",
            "dateOfBirth": "1958-06-07",
            "gender": "M",
            "maritalStatus": "Single",
            "occupation": "Other",
            "driverLicenseNumber": "123456789",
            "vehicles": [
                {
                    "ownership_status": 3,
                    "annual_mileage": 10000,
                    "purchase_date": "03/01/2024",
                },
                {
                    "ownership_status": 3,
                    "annual_mileage": 10000,
                    "purchase_date": "03/01/2024",
                }
            ],
            "years_at_residence": 99,
            "prior_carrier_home": "Allstate Ins Co",
            "prior_expiration": "03/31/2026",
            "years_continuous_ins": 1,
            "customFields": [],
            "tags": [],
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
