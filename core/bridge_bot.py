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

_NG360_OCCUPATION_CANONICAL = (
    "Athletes, Entertainers, High Profile Profession",
    "Journalist",
    "Military",
    "Politician",
    "Student",
    "Other",
)

def _occupation_value_for_ng360_dropdown(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return "Other"
    for canon in _NG360_OCCUPATION_CANONICAL:
        if s.casefold() == canon.casefold():
            return canon
    low = s.lower()
    if any(k in low for k in ("athlete", "entertain", "actor", "actress", "musician", "celebrit", "performer", "nfl", "nba", "mlb")):
        return "Athletes, Entertainers, High Profile Profession"
    if any(k in low for k in ("journal", "reporter", "news anchor", "correspondent")):
        return "Journalist"
    if any(k in low for k in ("military", "army", "navy", "marine", "air force", "coast guard", "national guard", "veteran", "retired military")):
        return "Military"
    if any(k in low for k in ("politic", "senator", "congress", "mayor", "governor")):
        return "Politician"
    if any(k in low for k in ("student", "college", "university", "undergrad", "graduate student")):
        return "Student"
    return "Other"


def _driver_gender_portal_value(raw: str | None) -> str:
    """NG360 driver gender dropdown uses M / F. Default Male when unknown (reference UI)."""
    s = (raw or "").strip().casefold()
    if s in ("f", "female"):
        return "F"
    if s in ("m", "male"):
        return "M"
    return "M"


def _driver_marital_portal_value(raw: str | None) -> str:
    """Match portal option values exactly; default Single when missing."""
    allowed = ("Divorced", "Married", "Single", "Separated", "Unknown", "Widowed")
    s = (raw or "").strip()
    if not s:
        return "Single"
    for a in allowed:
        if a.casefold() == s.casefold():
            return a
    return "Single"


def _driver_education_portal_value(raw: str | None) -> str:
    """Education Level dropdown values: 2, 4, G, N, S. Default Four Year College (4)."""
    if raw is None or not str(raw).strip():
        return "4"
    s = str(raw).strip().casefold()
    if s in ("2", "two", "two year", "associate", "aa", "as"):
        return "2"
    if s in ("4", "four", "four year", "bachelor", "bs", "ba", "college"):
        return "4"
    if s in ("g", "graduate", "masters", "phd", "doctorate"):
        return "G"
    if s in ("n", "none", "no degree"):
        return "N"
    if s in ("s", "some college", "some"):
        return "S"
    return "4"


def _driver_connected_program_participate(raw: str | None) -> bool:
    """Default True (Program Participant Yes) when CRM is silent; matches reference flow."""
    if raw is None or not str(raw).strip():
        return True
    s = str(raw).strip().casefold()
    if s in ("0", "false", "no", "n", "off"):
        return False
    if s in ("1", "true", "yes", "y", "on"):
        return True
    return True


def _second_driver_first_name(contact: dict) -> str | None:
    return _contact_first(
        contact,
        "secondDriverFirstName",
        "driver2FirstName",
        "spouseFirstName",
        "spouse_first_name",
        "secondaryFirstName",
        "secondary_first_name",
        "householdDriverFirstName",
        "additionalDriverFirstName",
    )


def _second_driver_last_name(contact: dict) -> str | None:
    return _contact_first(
        contact,
        "secondDriverLastName",
        "driver2LastName",
        "spouseLastName",
        "spouse_last_name",
        "secondaryLastName",
        "secondary_last_name",
        "householdDriverLastName",
        "additionalDriverLastName",
    )


def _second_driver_middle_name(contact: dict) -> str | None:
    return _contact_first(
        contact,
        "secondDriverMiddleName",
        "driver2MiddleName",
        "spouseMiddleName",
        "spouse_middle_name",
        "secondaryMiddleName",
    )


def _second_driver_dob_raw(contact: dict) -> str | None:
    return _contact_first(
        contact,
        "secondDriverDateOfBirth",
        "driver2DateOfBirth",
        "spouseDateOfBirth",
        "spouse_date_of_birth",
        "secondaryDateOfBirth",
        "householdDriverDateOfBirth",
    )


def _parse_driver_name_parts(full_name: str) -> tuple[str, str, str | None] | None:
    parts = [p for p in re.split(r"\s+", (full_name or "").strip()) if p]
    if len(parts) < 2:
        return None
    if len(parts) == 2:
        return parts[0], parts[1], None
    return parts[0], parts[-1], " ".join(parts[1:-1]) or None


def _additional_driver_identity(
    contact: dict,
    driver_index: int,
    grid_row: dict | None,
) -> tuple[str, str, str]:
    """First, last, DOB for driver row 1+ (0 = primary). CRM, then grid, then backup. No middle name."""
    fn: str | None = None
    ln: str | None = None
    if driver_index == 1:
        fn = _second_driver_first_name(contact)
        ln = _second_driver_last_name(contact)
    if grid_row:
        if not fn:
            fn = (grid_row.get("first") or "").strip() or None
        if not ln:
            ln = (grid_row.get("last") or "").strip() or None
    if not fn or not ln:
        backup = _parse_driver_name_parts((grid_row or {}).get("raw", ""))
        if backup:
            fn, ln = backup[0], backup[1]
    if not fn or not ln:
        fn = fn or "Household"
        ln = ln or f"Driver{driver_index + 1}"

    dob_raw = _second_driver_dob_raw(contact) if driver_index == 1 else None
    dob = _date(dob_raw) if dob_raw else "6/15/1970"
    return fn.strip(), ln.strip(), dob


def _driver_name_looks_valid(value: str) -> bool:
    s = (value or "").strip()
    if not s:
        return False
    if s.casefold() in ("delete", "view/edit", "view", "edit"):
        return False
    if re.search(r"\d", s):
        return False
    return True


def _years_at_residence_portal_value(raw: Any) -> str:
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

_NG360_STORIES_VALUES = frozenset({"1", "1.5", "2", "2.5", "3", "3.5", "4", "4.5", "5", "1.75", "2.75", "3.75", "4.75"})

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

# Page 8 (Underwriting) — used only when GHL/contact has no value AND portal control is still unset.
_UW8_DEFAULT_PRIOR_COVERAGE = "Prior standard insurance"
_UW8_DEFAULT_PRIOR_CARRIER = "Allstate Ins Co"
_UW8_DEFAULT_PRIOR_EXPIRATION = "05/14/2026"
_UW8_DEFAULT_CONTINUOUS_YEARS = "4"
_UW8_SITE_ACCESS_VALUE = "FlatAreaEasyAccessRoads"  # portal option value for Flat Area/Easy Access Roads
_UW8_PRIOR_COVERAGE_CONTACT_KEYS = (
    "prior_insurance_coverage",
    "priorInsuranceCoverage",
    "prior_insurance",
    "priorInsurance",
)
_UW8_PRIOR_EXPIRATION_KEYS = ("prior_expiration", "priorExpiration", "prior_policy_expiration")
_UW8_CONTINUOUS_YEARS_KEYS = ("years_continuous_ins", "yearsContinuousPropertyInsurance", "continuous_insurance_years")

_BRIDGEBOT_EXPECTED_CONTACT_FIELDS: tuple[str, ...] = (
    "id", "firstName", "lastName", "postalCode", "dateOfBirth", "gender",
    "maritalStatus", "occupation", "phone", "address1", "city", "email",
    "driverLicenseNumber", "prior_carrier_home", "year_built", "square_footage",
    "number_of_stories", "years_at_residence", "datePurchased", "effective_date",
    "prior_expiration", "years_continuous_ins", "vehicles",
)

def _year_built_portal_value(raw: Any) -> str:
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


def _portal_year_int(raw: Any) -> int | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 4:
        y = int(digits[:4])
    elif len(digits) == 2:
        cy = datetime.now().year
        y = 2000 + int(digits)
        if y > cy + 1:
            y = 1900 + int(digits)
    else:
        return None
    cy = datetime.now().year
    if y < 1800 or y > cy + 1:
        return None
    return y


async def _page_7_read_year_built(page: Page) -> int | None:
    loc = page.locator("#MainContent_txtYearBuilt").first
    if await loc.count() == 0:
        return None
    try:
        return _portal_year_int(await loc.input_value())
    except Exception:
        return None


async def _page_7_ensure_roof_year_not_older_than_dwelling(
    page: Page,
    verify_fields: dict[str, str],
    contact: dict,
) -> None:
    """Set roof renovation year: trust portal if already filled, else use CRM or safe default."""
    _SAFE_ROOF_DEFAULT_YEARS_AGO = 5  # current_year - 5 always passes R0779 age limit

    yrroof_sel = "#MainContent_txtYearRoofRenovation"
    loc_roof = page.locator(yrroof_sel).first
    if await loc_roof.count() == 0:
        return

    roof_cur = (await loc_roof.input_value()).strip()

    # Portal already has a year — leave it alone, don't overwrite
    if _portal_year_int(roof_cur):
        log("INFO", f"Roof year already filled in portal ({roof_cur}) — skipping override")
        verify_fields[yrroof_sel] = roof_cur
        return

    # Portal is empty — try CRM first, then safe default
    roof_from_crm = _contact_first(
        contact,
        "year_roof_renovation",
        "roofRenovationYear",
        "yearRoofRenovation",
        "roof_year",
        "roofYear",
    )
    roof_y = _portal_year_int(roof_from_crm)
    if roof_y is None:
        roof_y = datetime.now().year - _SAFE_ROOF_DEFAULT_YEARS_AGO
        log("INFO", f"No roof year in portal or CRM — using safe default {roof_y}")

    # R0396: roof year must not be before year built
    yb = await _page_7_read_year_built(page)
    if yb is None:
        yb_raw = _contact_first(contact, "year_built", "yearBuilt")
        if yb_raw:
            yb = _portal_year_int(_year_built_portal_value(yb_raw))
        if yb is None:
            yb = 2020
    if roof_y < yb:
        log("INFO", f"Roof year {roof_y} is before year built {yb}; setting to {yb} (R0396)")
        roof_y = yb

    roof_s = str(roof_y)
    await loc_roof.fill(roof_s)
    verify_fields[yrroof_sel] = roof_s


def _square_footage_portal_value(raw: Any) -> str:
    default = "2200"
    if raw is None or (isinstance(raw, str) and not str(raw).strip()):
        return default
    s = str(raw).strip().replace(",", "")
    if re.fullmatch(r"\d+(?:\.\d)?", s):
        return s
    m = re.search(r"\d+(?:\.\d)?", s)
    return m.group(0) if m else default

def _number_of_stories_portal_value(raw: Any) -> str:
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

def _contact_first(contact: dict, *keys: str) -> str | None:
    """First non-empty contact value among keys, or None."""
    for k in keys:
        v = contact.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None

def _date(raw: str) -> str:
    mmddyyyy = format_date_to_mmddyyyy(raw)
    return f"{mmddyyyy[0:2]}/{mmddyyyy[2:4]}/{mmddyyyy[4:8]}"

def log(level: str, msg: str) -> None:
    print(f"[bridge_bot][{level}] {msg}", flush=True)


BOT_TOTAL_PAGES = 15

BOT_PAGE_NAMES: dict[int, str] = {
    0: "MFA (Two-Factor)",
    1: "Login",
    2: "Select State & Product",
    3: "Client Search",
    4: "Client Info (Part 1)",
    5: "Client Info (Part 2)",
    6: "Prefill",
    7: "Property Info",
    8: "Underwriting",
    9: "Loss History",
    10: "Coverage",
    11: "Driver Info",
    12: "Driver Violations",
    13: "Vehicles",
    14: "Auto Underwriting",
    15: "Premium Summary",
}

BOT_PAGE_URL_HINTS: dict[int, tuple[str, ...]] = {
    0: ("Login", "TwoFactor"),
    1: ("Login", "MainMenu"),
    2: ("MainMenu",),
    3: ("ClientSearch",),
    4: ("ClientInfo",),
    5: ("ClientInfo", "Prefill"),
    6: ("Prefill",),
    7: ("PropertyInfo",),
    8: ("Underwriting",),
    9: ("LossHistory",),
    10: ("Coverage",),
    11: ("DriverInfo",),
    12: ("DriverViolations",),
    13: ("VehicleInfo",),
    14: ("AutoUnderwriting",),
    15: ("PremiumSummary",),
}


def _debug_page_url(page: Page | None) -> str:
    if not page:
        return "n/a"
    try:
        return page.url or "n/a"
    except Exception:
        return "n/a"


def debug_step(page_num: int, step: str, detail: str = "", *, page: Page | None = None) -> None:
    name = BOT_PAGE_NAMES.get(page_num, f"Page {page_num}")
    suffix = f" | {detail}" if detail else ""
    url_bit = f" | url={_debug_page_url(page)}" if page else ""
    log("DEBUG", f"[PAGE {page_num:02d}/{BOT_TOTAL_PAGES}] {name} → {step}{suffix}{url_bit}")


def debug_page_start(page_num: int, page: Page | None = None, *, note: str = "") -> None:
    name = BOT_PAGE_NAMES.get(page_num, f"Page {page_num}")
    hints = BOT_PAGE_URL_HINTS.get(page_num, ())
    hint_txt = f" | expect_url~{','.join(hints)}" if hints else ""
    note_txt = f" | {note}" if note else ""
    log(
        "DEBUG",
        f"[PAGE {page_num:02d}/{BOT_TOTAL_PAGES}] ▶ START {name}{note_txt}{hint_txt} | url={_debug_page_url(page)}",
    )


def debug_page_done(page_num: int, page: Page | None = None, *, elapsed_s: float | None = None, note: str = "") -> None:
    name = BOT_PAGE_NAMES.get(page_num, f"Page {page_num}")
    elapsed_txt = f" | {elapsed_s:.1f}s" if elapsed_s is not None else ""
    note_txt = f" | {note}" if note else ""
    log(
        "DEBUG",
        f"[PAGE {page_num:02d}/{BOT_TOTAL_PAGES}] ✓ DONE {name}{elapsed_txt}{note_txt} | url={_debug_page_url(page)}",
    )


def debug_page_skip(page_num: int, reason: str, *, page: Page | None = None) -> None:
    name = BOT_PAGE_NAMES.get(page_num, f"Page {page_num}")
    log(
        "DEBUG",
        f"[PAGE {page_num:02d}/{BOT_TOTAL_PAGES}] ⊘ SKIP {name} | reason={reason} | url={_debug_page_url(page)}",
    )


def debug_page_fail(page_num: int, exc: BaseException, *, page: Page | None = None, elapsed_s: float | None = None) -> None:
    name = BOT_PAGE_NAMES.get(page_num, f"Page {page_num}")
    elapsed_txt = f" | {elapsed_s:.1f}s" if elapsed_s is not None else ""
    log(
        "DEBUG",
        f"[PAGE {page_num:02d}/{BOT_TOTAL_PAGES}] ✗ FAIL {name}{elapsed_txt} | {exc} | url={_debug_page_url(page)}",
    )


async def run_bot_page(page_num: int, page: Page, fn, *args, note: str = "", **kwargs):
    """Run one bot page handler with uniform debug start/done/fail logging."""
    debug_page_start(page_num, page, note=note)
    t0 = time.monotonic()
    try:
        result = fn(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        debug_page_done(page_num, page, elapsed_s=time.monotonic() - t0)
        return result
    except Exception as exc:
        debug_page_fail(page_num, exc, page=page, elapsed_s=time.monotonic() - t0)
        log("INFO", f"Waiting 5s after page {page_num} failure before continuing...")
        await asyncio.sleep(5)
        raise


def debug_post_flow(step: str, *, page: Page | None = None, detail: str = "") -> None:
    suffix = f" | {detail}" if detail else ""
    log("DEBUG", f"[POST-FLOW] {step}{suffix} | url={_debug_page_url(page)}")


async def _checkpoint_wait_for_enter(message: str) -> None:
    """Pause automation until the operator presses Enter in the terminal."""
    log("INFO", message)
    await asyncio.to_thread(
        input,
        f"\n[CHECKPOINT] {message}\nPress Enter to continue... ",
    )


async def _save_html(page: Page, step: str) -> None:
    if not page:
        return
    try:
        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^a-zA-Z0-9]+", "_", step).strip("_")
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = ARTIFACTS_DIR / f"fail_{name}_{ts}.html"
        path.write_text(await page.content(), encoding="utf-8")
        log("OK", f"HTML saved -> {path}")
    except Exception as exc:
        log("FAIL", f"Could not save HTML: {exc}")

async def _save_screenshot(page: Page, step: str) -> None:
    if not page:
        return
    try:
        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^a-zA-Z0-9]+", "_", step).strip("_")
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = str(ARTIFACTS_DIR / f"fail_{name}_{ts}.png")
        await page.screenshot(path=path)
        log("OK", f"Screenshot saved -> {path}")
    except Exception as exc:
        log("FAIL", f"Could not save screenshot: {exc}")

async def _type_slow(page: Page, selector: str, text: str, delay: float = 120) -> None:
    field = page.locator(selector)
    await field.wait_for(state="visible", timeout=10000)
    await field.click()
    await field.fill("")
    await field.type(text, delay=delay, timeout=15000)

async def _select_strict_dropdown(page: Page, selector: str, value: str, fallback_value: str | None = "Other") -> bool:
    loc = page.locator(selector)
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
            log("WARN", f"Dropdown {selector}: no match for {value!r}, selecting fallback {fallback_value!r}")
        if await _try_pick(fallback_value):
            return True
    if "YearsAtAddress" in selector and await _try_pick("3"):
        log("WARN", f"Dropdown {selector}: fallback failed; selected last-resort '3'")
        return True
    raise RuntimeError(f"Could not select dropdown option {value!r}" + (f" or fallback {fallback_value!r}" if fallback_value else ""))

async def _verify_and_refill(page: Page, fields: dict[str, str]) -> None:
    for sel, expected in fields.items():
        if expected is None:
            continue
        expected_str = str(expected).strip()
        if not expected_str:
            continue
        try:
            loc = page.locator(sel)
            if await loc.count() > 0 and await loc.is_visible():
                actual = await loc.input_value()
                if str(actual).strip() != expected_str:
                    log("WARN", f"Field {sel} was not filled correctly before save/continue (got {actual!r}, expected {expected_str!r}). Refilling...")
                    await loc.fill("")
                    await loc.type(expected_str, delay=100)
                    await page.wait_for_timeout(300)
                    actual_after = await loc.input_value()
                    if str(actual_after).strip() == expected_str:
                        log("OK", f"Field {sel} successfully refilled.")
                    else:
                        log("FAIL", f"Field {sel} STILL not correct after refill.")
        except Exception as exc:
            log("WARN", f"Could not verify/refill {sel}: {exc}")

# - PAGE 0: MFA ----------------------------

def _log_recent_otp_candidates_from_imessage(lookback_minutes: int = 30, limit: int = 200) -> None:
    db_path = Path.home() / "Library" / "Messages" / "chat.db"
    if not db_path.exists():
        log("WARN", f"Messages DB not found for debug dump: {db_path}")
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
            log("WARN", "OTP debug dump: no recent inbound messages in lookback window")
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
            log("INFO", f"OTP candidate -> sender={sender_id}, code={code}")

        if printed == 0:
            log("WARN", "OTP debug dump: no 6-digit codes found in recent inbound messages")
    except Exception as exc:
        log("WARN", f"OTP debug dump failed: {exc}")

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

async def _submit_two_factor_code(page: Page, code: str) -> None:
    code_input = page.locator(
        "#TwoFactorCode, input[type='tel'], input[name*='code' i], input[id*='code' i], input[autocomplete='one-time-code']"
    ).first
    await code_input.wait_for(state="visible", timeout=20000)
    await code_input.fill(code)

    remember_machine = page.locator("#RememberMachine, input[name='RememberMachine'][type='checkbox']").first
    if await remember_machine.count() > 0:
        try:
            await remember_machine.check(timeout=5000)
            log("INFO", "Checked 'Remember this device' for 2FA")
        except Exception as exc:
            log("WARN", f"Could not check 'Remember this device': {exc}")

    submit = page.locator(
        "#verifyButton, button:has-text('Verify'), button:has-text('Submit'), button[type='submit'], input[type='submit']"
    ).first
    await submit.click(timeout=10000)
    try:
        await page.wait_for_url("**/MainMenu.aspx", timeout=30000)
    except Exception:
        if await page.locator("span[data-valmsg-for='TwoFactorCode']").count() > 0:
            err = (await page.locator("span[data-valmsg-for='TwoFactorCode']").first.inner_text()).strip()
            if err:
                raise RuntimeError(f"2FA verify failed: {err}")
        raise

async def page_0_mfa(page: Page, otp_sender: str, otp_timeout: int, otp_initial_wait: int) -> None:
    debug_page_start(0, page)
    t0 = time.monotonic()
    has_choice_screen = await page.locator("#loginWith2faSms, #loginWith2faEmail").count() > 0
    has_code_screen = await page.locator("#TwoFactorCode, #verifyButton").count() > 0

    if not has_choice_screen and not has_code_screen:
        debug_page_skip(0, "no 2FA screen detected", page=page)
        return

    log("INFO", f"2FA detected on {page.url}")
    debug_step(0, "2FA challenge detected", page=page)

    if has_choice_screen and not has_code_screen:
        debug_step(0, "choose SMS or Email delivery", page=page)
        if await page.locator("#loginWith2faSms").count() > 0:
            await page.click("#loginWith2faSms")
            log("INFO", "Clicked SMS 2FA option")
        elif await page.locator("#loginWith2faEmail").count() > 0:
            await page.click("#loginWith2faEmail")
            log("WARN", "SMS option missing, used Email 2FA option")
        else:
            raise RuntimeError("2FA choice page detected but no SMS/Email option found")

    await page.wait_for_selector("#TwoFactorCode, #verifyButton", state="visible", timeout=20000)
    debug_step(0, "retrieve OTP from iMessage", f"sender={otp_sender}", page=page)

    code = _retrieve_2fa_code_from_imessage(
        otp_sender=otp_sender,
        timeout=otp_timeout,
        initial_wait=otp_initial_wait,
    )
    if not code:
        raise RuntimeError("2FA code not found in iMessage within timeout")

    log("INFO", f"2FA code retrieved ({len(code)} digits)")
    debug_step(0, "submit OTP", page=page)
    await _submit_two_factor_code(page, code)
    log("OK", "2FA code submitted")
    debug_page_done(0, page, elapsed_s=time.monotonic() - t0)


# - PAGE 1: LOGIN ---------------------------

async def _click_enable_login_if_present(page: Page) -> bool:
    selectors = ["button:has-text('Enable Login')", "a:has-text('Enable Login')", "text=/enable\\s*login/i"]
    for selector in selectors:
        loc = page.locator(selector).first
        try:
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(timeout=5000)
                log("INFO", "Clicked Enable Login")
                await page.wait_for_load_state("domcontentloaded")
                return True
        except Exception:
            continue
    return False

async def _wait_for_post_userid_state(page: Page, timeout_ms: int = 60000) -> bool:
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        if "MainMenu.aspx" in page.url:
            return False
        if await page.locator("#Password").count() > 0 and await page.locator("#Password").first.is_visible():
            log("INFO", f"Password field detected on: {page.url}")
            return True
        await _click_enable_login_if_present(page)
        await page.wait_for_timeout(400)

    title = await page.title()
    has_userid = await page.locator("#txtUserID").count() > 0
    has_password = await page.locator("#Password").count() > 0
    error_text = ""
    if await page.locator("#pnlErrorsAllstate").count() > 0:
        error_text = (await page.locator("#pnlErrorsAllstate").inner_text()).strip()
    log("WARN", f"Post-userid state not resolved in {timeout_ms/1000:.0f}s. url={page.url}")
    log("WARN", f"Page title: {title!r}")
    log("WARN", f"Field states: has_txtUserID={has_userid}, has_Password={has_password}")
    if error_text:
        log("WARN", f"Login error banner: {error_text!r}")
    raise PlaywrightTimeoutError("Timed out waiting for MainMenu or password page after userid submit.")

async def page_1_login(page: Page, username: str, password: str, otp_sender: str, otp_timeout: int, otp_initial_wait: int, context: BrowserContext) -> None:
    if not username.strip() or not password.strip():
        raise RuntimeError("NATGEN_USERNAME or NATGEN_PASSWORD is empty.")

    debug_step(1, "open agency site", page=page)
    await page.goto("https://natgenagency.com/", timeout=30000)
    await page.wait_for_load_state("domcontentloaded")
    if "MainMenu.aspx" in page.url:
        log("INFO", f"Session active; landed directly on Main Menu: {page.url}")
        debug_step(1, "existing session — skip login form", page=page)
        if context:
            await context.storage_state(path=str(STORAGE_STATE_PATH))
        return

    debug_step(1, "load login page", page=page)
    await page.goto("https://natgenagency.com/Login.aspx", timeout=30000)
    await _click_enable_login_if_present(page)
    if await page.locator("#txtUserID").count() == 0:
        await _click_enable_login_if_present(page)

    await page.wait_for_selector("#txtUserID", state="visible", timeout=30000)
    log("INFO", f"Login page loaded: {page.url}")

    await _type_slow(page, "#txtUserID", username)
    entered_userid = await page.input_value("#txtUserID")
    log("INFO", f"Filled #txtUserID with: {entered_userid!r}")
    debug_step(1, "submit user ID", page=page)
    await page.click("#btnLogin", timeout=10000)
    log("INFO", "Clicked #btnLogin, waiting for next login state...")

    needs_password = await _wait_for_post_userid_state(page)
    if not needs_password:
        log("INFO", "Reached Main Menu after User ID submit (no password screen)")
        debug_step(1, "main menu reached (no password)", page=page)
        if context:
            await context.storage_state(path=str(STORAGE_STATE_PATH))
        return

    debug_step(1, "submit password", page=page)
    await _type_slow(page, "#Password", password)
    await page.click("button[type='submit']", timeout=10000)
    log("INFO", "Password submitted, waiting for Main Menu or MFA...")

    try:
        await page.wait_for_url("**/MainMenu.aspx", timeout=12000)
        log("INFO", "Reached Main Menu directly (no MFA challenge)")
        debug_step(1, "main menu reached (no MFA)", page=page)
        if context:
            await context.storage_state(path=str(STORAGE_STATE_PATH))
        return
    except Exception:
        log("INFO", "Main Menu not reached yet; checking MFA flow")
        debug_step(1, "hand off to PAGE 00 MFA", page=page)

    await page_0_mfa(page, otp_sender, otp_timeout, otp_initial_wait)

    debug_step(1, "wait for main menu after MFA", page=page)
    await page.wait_for_url("**/MainMenu.aspx", timeout=30000)
    if context:
        await context.storage_state(path=str(STORAGE_STATE_PATH))

# - PAGE 2: SELECT STATE & PRODUCT ------------------

async def page_2_select_state_product(page: Page) -> None:
    debug_step(2, "select GA + PKGProtect2", page=page)
    await page.select_option("#ctl00_MainContent_wgtMainMenuNewQuote_ddlState", "GA")
    await page.select_option("#ctl00_MainContent_wgtMainMenuNewQuote_ddlProduct", "PKGProtect2")
    
    # Inline click next logic with fallbacks
    clicked = False
    for attempt in range(3):
        for sel in ["#ctl00_MainContent_wgtMainMenuNewQuote_btnContinue", "input[name$='btnContinue']", "button:has-text('Continue')"]:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=10000)
                    clicked = True
                    break
            except Exception:
                pass
        if clicked:
            break
        try:
            await page.evaluate("document.getElementById('ctl00_MainContent_wgtMainMenuNewQuote_btnContinue')?.click()")
            clicked = True
            break
        except Exception:
            pass
        await page.wait_for_timeout(1000)
    
    if not clicked:
        raise RuntimeError("Could not click Continue on Select State/Product page")

    debug_step(2, "wait for Client Search", page=page)
    await page.wait_for_url("**/ClientSearch*", timeout=30000)

# - PAGE 3: CLIENT SEARCH ----------------------â”€

async def page_3_search_add_customer(page: Page, contact: dict) -> None:
    debug_step(3, "fill search fields", "first/last/zip", page=page)
    # --- FILL: First Name (contact['firstName'] or contact['first_name']) ---
    await page.fill("#MainContent_txtFirstName", _val(contact, "firstName", "first_name"))
    
    # --- FILL: Last Name (contact['lastName'] or contact['last_name']) ---
    await page.fill("#MainContent_txtLastName", _val(contact, "lastName", "last_name"))
    
    # --- FILL: Zip Code (contact['postalCode'] or contact['zip']) ---
    await page.fill("#MainContent_txtZipCode", _val(contact, "postalCode", "zip"))
    
    await _verify_and_refill(page, {
        "#MainContent_txtFirstName": _val(contact, "firstName", "first_name"),
        "#MainContent_txtLastName": _val(contact, "lastName", "last_name"),
        "#MainContent_txtZipCode": _val(contact, "postalCode", "zip"),
    })
    
    await page.click("#MainContent_btnSearch")
    await page.wait_for_load_state("networkidle")
    
    debug_step(3, "click Add New Client", page=page)
    # Click Add New Client
    clicked = False
    for attempt in range(3):
        for sel in ["#MainContent_btnAddNewClient", "input[name$='btnAddNewClient']", "button:has-text('Add New Client')"]:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=10000)
                    clicked = True
                    break
            except Exception:
                pass
        if clicked:
            break
        try:
            await page.evaluate("document.getElementById('MainContent_btnAddNewClient')?.click()")
            clicked = True
            break
        except Exception:
            pass
        await page.wait_for_timeout(1000)
        
    if not clicked:
        raise RuntimeError("Could not click Add New Client")

    debug_step(3, "wait for Client Info", page=page)
    await page.wait_for_url("**/ClientInfo*", timeout=30000)

# - PAGE 4: CLIENT INFO P1 ----------------------

async def page_4_client_info_p1(page: Page, contact: dict) -> None:
    debug_step(4, "fill named insured + contact fields", page=page)
    dob = _date(_val(contact, "dateOfBirth", "date_of_birth"))
    area, prefix, line = split_phone_number(_val(contact, "phone"))

    # --- FILL: Date of Birth (contact['dateOfBirth'] or contact['date_of_birth']) ---
    await page.fill("#MainContent_ucNamedInsured_txtDateOfBirth", dob)
    
    # --- SELECT: Gender (contact['gender']) ---
    await page.select_option("#MainContent_ucNamedInsured_ddlGender", _val(contact, "gender"))
    
    # --- SELECT: Marital Status (contact['maritalStatus'] or contact['marital_status']) ---
    await page.select_option("#MainContent_ucNamedInsured_ddlMaritalStatus", _val(contact, "maritalStatus", "marital_status"))

    occ_raw = _val(contact, "occupation")
    occ_val = _occupation_value_for_ng360_dropdown(occ_raw)
    if occ_val != occ_raw.strip():
        log("INFO", f"Occupation mapped for NG360 dropdown: {occ_raw!r} -> {occ_val!r}")
    occ_fallback = await _select_strict_dropdown(page, "#MainContent_ucNamedInsured_ddlOccupation", occ_val)
    
    if occ_val == "Other" or occ_fallback:
        other_txt = page.locator("#MainContent_ucNamedInsured_txtOtherOccupation")
        if await other_txt.count() > 0:
            try:
                raw_note = occ_raw.strip()
                if raw_note and raw_note.casefold() != "other":
                    await other_txt.fill(raw_note[:120])
            except Exception:
                pass

    # --- SELECT: Phone Type (Always 'Cell') ---
    await page.select_option("#MainContent_ucContactInfo_ucPhoneNumber_ddlPhoneType", "Cell")
    
    # --- FILL: Phone Area Code (split from contact['phone']) ---
    await page.fill("#MainContent_ucContactInfo_ucPhoneNumber_txtAreaCode", area)
    
    # --- FILL: Phone Prefix (split from contact['phone']) ---
    await page.fill("#MainContent_ucContactInfo_ucPhoneNumber_txtPrefix", prefix)
    
    # --- FILL: Phone Line Number (split from contact['phone']) ---
    await page.fill("#MainContent_ucContactInfo_ucPhoneNumber_txtLineNumber", line)

    # --- FILL: Street Address (contact['address1'] or contact['address']) ---
    await page.fill("#MainContent_ucResidentialAddress_txtAddress", _val(contact, "address1", "address"))
    
    # --- FILL: City (contact['city']) ---
    await page.fill("#MainContent_ucResidentialAddress_txtCity", _val(contact, "city"))
    y_raw = contact.get("years_at_residence", 3)
    y_val = _years_at_residence_portal_value(y_raw)
    if str(y_raw).strip() != y_val:
        log("INFO", f"Years at residence mapped for portal: {y_raw!r} -> {y_val!r}")
    await _select_strict_dropdown(page, "#MainContent_ddlYearsAtAddress", y_val, fallback_value="3")

    email = _val(contact, "email")
    
    # --- SELECT: Email Option (Always 'Provided') ---
    await page.select_option("#MainContent_ucContactInfo_ucEmailAddress_ddlEmailOption", "Provided")
    
    # --- FILL: Email Address (contact['email']) ---
    await page.fill("#MainContent_ucContactInfo_ucEmailAddress_txtEmailAddress", email)
    
    # --- FILL: Email Confirmation (contact['email']) ---
    await page.fill("#MainContent_ucContactInfo_ucEmailAddress_txtEmailAddressConfirmation", email)

    await _verify_and_refill(page, {
        "#MainContent_ucNamedInsured_txtDateOfBirth": dob,
        "#MainContent_ucContactInfo_ucPhoneNumber_txtAreaCode": area,
        "#MainContent_ucContactInfo_ucPhoneNumber_txtPrefix": prefix,
        "#MainContent_ucContactInfo_ucPhoneNumber_txtLineNumber": line,
        "#MainContent_ucResidentialAddress_txtAddress": _val(contact, "address1", "address"),
        "#MainContent_ucResidentialAddress_txtCity": _val(contact, "city"),
        "#MainContent_ucContactInfo_ucEmailAddress_txtEmailAddress": email,
        "#MainContent_ucContactInfo_ucEmailAddress_txtEmailAddressConfirmation": email,
    })

    # Inline click next logic with fallbacks
    clicked = False
    for attempt in range(3):
        for sel in ["#MainContent_btnContinue", "input[name$='btnContinue']", "button:has-text('Continue')", "button:has-text('Next')"]:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=10000)
                    clicked = True
                    break
            except Exception:
                pass
        if clicked:
            break
        try:
            await page.evaluate("document.getElementById('MainContent_btnContinue')?.click()")
            clicked = True
            break
        except Exception:
            pass
        await page.wait_for_timeout(1000)
        
    if not clicked:
        raise RuntimeError("Could not click Continue on Client Info P1")
        
    await page.wait_for_load_state("networkidle")

# - PAGE 5: CLIENT INFO P2 ----------------------

async def page_5_client_info_p2(page: Page, agent_id: str) -> None:
    debug_step(5, "set Input By agent", f"agent_id={agent_id!r}", page=page)
    await page.select_option("#MainContent_ucGeneralInformation_ddlInputBy", agent_id)
    
    # Inline click next logic with fallbacks
    clicked = False
    for attempt in range(3):
        for sel in ["#MainContent_btnContinue", "input[name$='btnContinue']", "button:has-text('Continue')", "button:has-text('Next')"]:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=10000)
                    clicked = True
                    break
            except Exception:
                pass
        if clicked:
            break
        try:
            await page.evaluate("document.getElementById('MainContent_btnContinue')?.click()")
            clicked = True
            break
        except Exception:
            pass
        await page.wait_for_timeout(1000)
        
    if not clicked:
        raise RuntimeError("Could not click Continue on Client Info P2")

    debug_step(5, "wait for Prefill", page=page)
    await page.wait_for_url("**/Prefill*", timeout=30000)

# - PAGE 6: PREFILL -------------------------â”€

async def page_6_prefill(page: Page) -> None:
    debug_step(6, "accept prefill drivers/autos if present", page=page)
    try:
        await page.wait_for_selector("#gvPrefillDriver, #gvPrefillAuto", timeout=5000)

        if await page.locator("#MainContent_ucPrefillDriver_btnAcceptAllDrivers").count() > 0:
            await page.click("#MainContent_ucPrefillDriver_btnAcceptAllDrivers")
            await page.wait_for_load_state("networkidle")

        driver_statuses = page.locator("#gvPrefillDriver select[name*='ddlDriverStatus']")
        for i in range(await driver_statuses.count()):
            status = driver_statuses.nth(i)
            if await status.is_disabled():
                continue
            current = (await status.input_value()).strip()
            if current == "-1":
                await status.select_option("A")

        vehicle_statuses = page.locator("#gvPrefillAuto select[name*='ddl'][name*='Status'], #gvPrefillAuto select[id*='ddl'][id*='Status']")
        for i in range(await vehicle_statuses.count()):
            status = vehicle_statuses.nth(i)
            if await status.is_disabled():
                continue
            current = (await status.input_value()).strip()
            if current != "-1":
                continue
            options = await status.evaluate("el => Array.from(el.options).map(o => String(o.value || '').trim())")
            preferred = ("A", "Accept", "1", "True", "Y")
            choice = next((v for v in preferred if v in options), None)
            if not choice:
                choice = next((v for v in options if v not in ("", "-1")), None)
            if choice:
                await status.select_option(choice)

        if await page.locator("#btnAcceptAllAutos").count() > 0:
            await page.click("#btnAcceptAllAutos")
        auto_accept = page.locator("#gvPrefillAuto input[type='radio'][name*='rbAccept']")
        for i in range(await auto_accept.count()):
            if not await auto_accept.nth(i).is_checked():
                await auto_accept.nth(i).check()

        license_cbs = page.locator("#gvPrefillDriver input[type='checkbox'][id*='License' i], input[type='checkbox'][id*='chkLicense' i], input[type='checkbox'][id*='License' i]")
        for i in range(await license_cbs.count()):
            cb = license_cbs.nth(i)
            try:
                if not await cb.is_checked():
                    await cb.check(timeout=5000)
            except Exception:
                pass
    except PlaywrightTimeoutError:
        log("WARN", "No prefill table, skipping")

    # Inline click next logic with fallbacks
    clicked = False
    for attempt in range(3):
        for sel in ["#MainContent_btnContinue", "input[name$='btnContinue']", "button:has-text('Continue')", "button:has-text('Next')"]:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=10000)
                    clicked = True
                    break
            except Exception:
                pass
        if clicked:
            break
        try:
            await page.evaluate("document.getElementById('MainContent_btnContinue')?.click()")
            clicked = True
            break
        except Exception:
            pass
        await page.wait_for_timeout(1000)
        
    if not clicked:
        raise RuntimeError("Could not click Continue on Prefill")

    await page.wait_for_load_state("networkidle")

    if "Prefill" in page.url and await page.locator("#lstErrors li").count() > 0:
        err = (await page.locator("#lstErrors").inner_text()).strip()
        if err:
            raise RuntimeError(f"Prefill validation blocked continue: {err}")


# - PAGE 7: PROPERTY INFO ----------------------â”€

def _dropdown_selection_is_empty(cur: str) -> bool:
    return cur.strip() in ("", "-1")


async def _select_from_contact_or_fallback(
    page: Page,
    selector: str,
    contact: dict,
    keys: tuple[str, ...],
    *,
    fallback_value: str | None = None,
    extra_fallback_values: tuple[str, ...] = (),
    fallback_labels: tuple[str, ...] = (),
    only_if_empty: bool = True,
) -> None:
    loc = page.locator(selector).first
    if await loc.count() == 0:
        return
    cur = (await loc.input_value()).strip()
    if only_if_empty and not _dropdown_selection_is_empty(cur):
        return
    raw = _contact_first(contact, *keys)

    async def _try_pick(by: str, s: str) -> bool:
        if not s:
            return False
        try:
            if by == "value":
                await loc.select_option(value=s, timeout=8000)
            else:
                await loc.select_option(label=s, timeout=8000)
            return True
        except Exception:
            return False

    if raw:
        if await _try_pick("value", raw):
            return
        if await _try_pick("label", raw):
            return
    if fallback_value:
        if await _try_pick("value", fallback_value):
            return
        if await _try_pick("label", fallback_value):
            return
    for v in extra_fallback_values:
        if await _try_pick("value", v):
            return
        if await _try_pick("label", v):
            return
    for lbl in fallback_labels:
        if await _try_pick("label", lbl):
            return
    log("WARN", f"{selector}: could not select (contact={raw!r}, fallback={fallback_value!r})")


async def _select_number_of_stories_property(page: Page, contact: dict, *, only_if_empty: bool = True) -> None:
    sel = "#MainContent_ddlNumberOfStories"
    if await page.locator(sel).count() == 0:
        return
    loc = page.locator(sel).first
    cur = (await loc.input_value()).strip()
    if only_if_empty and not _dropdown_selection_is_empty(cur):
        return
    raw = _contact_first(contact, "number_of_stories", "num_stories", "stories")

    async def _try_pick(by: str, s: str) -> bool:
        if not s:
            return False
        try:
            if by == "value":
                await loc.select_option(value=s, timeout=8000)
            else:
                await loc.select_option(label=s, timeout=8000)
            return True
        except Exception:
            return False

    if raw:
        st = _number_of_stories_portal_value(raw)
        if await _try_pick("value", st):
            return
        if await _try_pick("label", raw):
            return
    for by, s in (
        ("value", "2"),
        ("label", "2 Storied"),
        ("label", "2 Story"),
        ("label", "2 Stories"),
    ):
        if await _try_pick(by, s):
            return
    log("WARN", f"{sel}: could not select stories (contact={raw!r})")


_MORTGAGEE_NAME_SELECTOR = (
    "#MainContent_uc1PropertyAI_txtSearchName, #MainContent_uc1PropertyAI_txtInterestName"
)
_MORTGAGEE_DDL_SELECTOR = "#MainContent_uc1PropertyAI_ddlInterestType"
_MORTGAGEE_SEARCH_PANEL = "#MainContent_uc1PropertyAI_pnlInterestSearch"


async def _webforms_dropdown_postback(
    page: Page,
    selector: str,
    *,
    value: str | None = None,
    label: str | None = None,
) -> bool:
    loc = page.locator(selector).first
    if await loc.count() == 0:
        return False

    picked = False
    if value:
        try:
            await loc.select_option(value=value, timeout=8000)
            picked = True
        except Exception:
            pass
    if not picked and label:
        try:
            await loc.select_option(label=label, timeout=8000)
            picked = True
        except Exception:
            pass
    if not picked and value:
        try:
            picked = await loc.evaluate(
                """(el, val) => {
                    for (const o of el.options) {
                        const t = (o.textContent || '').trim();
                        if (o.value === val || t === val || t.toLowerCase().includes('mortgagee')) {
                            el.value = o.value;
                            return true;
                        }
                    }
                    return false;
                }""",
                value,
            )
        except Exception:
            picked = False
    if not picked:
        return False

    name = await loc.get_attribute("name")
    if name:
        try:
            await page.evaluate(
                """n => { if (typeof __doPostBack === 'function') __doPostBack(n, ''); }""",
                name,
            )
        except Exception as exc:
            log("WARN", f"WebForms postback failed for {selector}: {exc}")
    try:
        await page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        await page.wait_for_timeout(800)
    return True


async def _page_7_wait_for_mortgagee_search_name(page: Page, timeout_ms: int = 30000):
    name_in = page.locator(_MORTGAGEE_NAME_SELECTOR).first
    await name_in.wait_for(state="visible", timeout=timeout_ms)
    return name_in


async def _page_7_additional_interest_already_on_quote(page: Page) -> bool:
    for sel in (
        "#MainContent_uc1PropertyAI_gvInterests tbody tr",
        "#MainContent_gvAdditionalInterests tbody tr",
        "#MainContent_uc1PropertyAI_gvSavedInterests tbody tr",
    ):
        rows = page.locator(sel)
        if await rows.count() > 0:
            debug_step(7, "additional interest already on quote — skip add", page=page)
            return True
    return False


async def _page_7_click_property_continue(page: Page) -> None:
    clicked = False
    for attempt in range(3):
        for sel in ["#MainContent_btnContinue", "input[name$='btnContinue']", "button:has-text('Continue')", "button:has-text('Next')"]:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=10000)
                    clicked = True
                    break
            except Exception:
                pass
        if clicked:
            break
        try:
            await page.evaluate("document.getElementById('MainContent_btnContinue')?.click()")
            clicked = True
            break
        except Exception:
            pass
        await page.wait_for_timeout(1000)
    if not clicked:
        raise RuntimeError("Could not click Continue on Property Info")


async def _page_7_open_mortgagee_bill_search(page: Page) -> None:
    """Open the Mortgagee-Bill search panel (Add Interest + interest-type postback if needed)."""
    try:
        await _page_7_wait_for_mortgagee_search_name(page, timeout_ms=5000)
        debug_step(7, "mortgagee-bill search panel already open", page=page)
        return
    except Exception:
        pass

    add_interest = page.locator("#MainContent_uc1PropertyAI_btnAddInterest").first
    if await add_interest.count() == 0:
        raise RuntimeError("Add Interest button not found on Property Info page")

    await add_interest.scroll_into_view_if_needed()
    await add_interest.wait_for(state="visible", timeout=30000)
    debug_step(7, "click Add Interest", page=page)
    await add_interest.click(timeout=15000)
    try:
        await page.wait_for_load_state("load", timeout=30000)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        await page.wait_for_timeout(800)

    try:
        await _page_7_wait_for_mortgagee_search_name(page, timeout_ms=8000)
        debug_step(7, "mortgagee-bill search opened after Add Interest", page=page)
        return
    except Exception:
        pass

    try:
        await page.wait_for_selector(
            f"{_MORTGAGEE_DDL_SELECTOR}, {_MORTGAGEE_SEARCH_PANEL}, {_MORTGAGEE_NAME_SELECTOR}",
            timeout=15000,
        )
    except Exception:
        debug_step(7, "warn: no search name or interest-type dropdown yet", page=page)

    ddl_type = page.locator(_MORTGAGEE_DDL_SELECTOR).first
    if await ddl_type.count() > 0:
        debug_step(7, "select Mortgagee-Bill interest type (WebForms postback)", page=page)
        for val, lbl in (
            ("Mortgagee-Bill", "Mortgagee-Bill"),
            ("MortgageeBill", "Mortgagee Bill"),
            ("Mortgagee", "Mortgagee"),
        ):
            if await _webforms_dropdown_postback(page, _MORTGAGEE_DDL_SELECTOR, value=val, label=lbl):
                try:
                    await _page_7_wait_for_mortgagee_search_name(page, timeout_ms=15000)
                    debug_step(7, "mortgagee-bill search opened", f"interest_type={val!r}", page=page)
                    return
                except Exception:
                    continue

    try:
        await _page_7_wait_for_mortgagee_search_name(page, timeout_ms=30000)
    except Exception as exc:
        ddl = page.locator(_MORTGAGEE_DDL_SELECTOR).first
        opts: list[str] = []
        if await ddl.count() > 0:
            try:
                opts = await ddl.evaluate(
                    "el => Array.from(el.options).map(o => `${o.value}|${(o.textContent||'').trim()}`)"
                )
            except Exception:
                pass
        raise RuntimeError(
            "Mortgagee-Bill name field not found after Add Interest / interest-type selection. "
            f"url={page.url!r} ddl_options={opts[:20]!r}"
        ) from exc


async def _page_7_additional_interest_mortgagee_bill_then_next(page: Page, contact: dict | None = None) -> None:
    """After main Property Info fields: Add Interest → Mortgagee-Bill → Search → first Select → Save → Next (strict order)."""
    debug_step(7, "begin mortgagee-bill additional interest", page=page)
    if await _page_7_additional_interest_already_on_quote(page):
        debug_step(7, "click Continue (interest already saved)", page=page)
        await _page_7_click_property_continue(page)
        return

    await _page_7_open_mortgagee_bill_search(page)
    name_in = await _page_7_wait_for_mortgagee_search_name(page, timeout_ms=15000)

    debug_step(7, "fill mortgagee-bill search name", page=page)
    await name_in.fill("test")

    if contact:
        zip_val = _contact_first(contact, "postalCode", "zip", "postal_code")
        if zip_val:
            zip_in = page.locator("#MainContent_uc1PropertyAI_txtSearchZip").first
            if await zip_in.count() > 0:
                try:
                    if await zip_in.is_visible():
                        await zip_in.fill(str(zip_val).strip()[:15])
                except Exception:
                    pass

    debug_step(7, "search mortgage company grid", page=page)
    await page.click("#MainContent_uc1PropertyAI_btnSearch", timeout=15000)
    await page.wait_for_load_state("networkidle")

    grid = page.locator("#MainContent_uc1PropertyAI_gvMortgageCompany")
    await grid.wait_for(state="visible", timeout=30000)
    first_select = grid.locator("tbody a").filter(has_text="Select").first
    await first_select.wait_for(state="visible", timeout=30000)
    debug_step(7, "select first mortgage company result", page=page)
    await first_select.click(timeout=15000)
    await page.wait_for_load_state("networkidle")

    save_btn = page.locator("#MainContent_uc1PropertyAI_btnSaveInterest").first
    await save_btn.wait_for(state="visible", timeout=30000)
    debug_step(7, "save additional interest", page=page)
    await save_btn.click(timeout=15000)
    await page.wait_for_load_state("networkidle")

    debug_step(7, "click Continue after mortgagee-bill", page=page)
    await _page_7_click_property_continue(page)


async def page_7_property(page: Page, contact: dict) -> None:
    debug_step(7, "fill property risk fields", page=page)
    verify_fields: dict[str, str] = {}

    await _select_from_contact_or_fallback(
        page,
        "#MainContent_ddlForm",
        contact,
        ("policy_form", "policyForm", "home_policy_form"),
        fallback_value="HO3",
        fallback_labels=("HO3000",),
    )
    await _select_from_contact_or_fallback(
        page,
        "#MainContent_ddlResidenceClass",
        contact,
        ("residence_class", "residenceClass"),
        fallback_value="Primary",
    )
    await _select_from_contact_or_fallback(
        page,
        "#MainContent_ddlOccupancy2",
        contact,
        ("occupancy", "occupancy_type", "occupancyType"),
        fallback_value="OwnerOccupied",
        fallback_labels=("Owner Occupied",),
    )
    await _select_from_contact_or_fallback(
        page,
        "#MainContent_ddlType",
        contact,
        ("structure", "structure_type", "structureType"),
        fallback_value="Dwelling",
        fallback_labels=("Dwelling",),
    )

    yb_sel = "#MainContent_txtYearBuilt"
    loc_yb = page.locator(yb_sel).first
    if await loc_yb.count() > 0 and not (await loc_yb.input_value()).strip():
        yb_raw = _contact_first(contact, "year_built", "yearBuilt")
        yb_val = _year_built_portal_value(yb_raw) if yb_raw else "2020"
        await loc_yb.fill(yb_val)
        verify_fields[yb_sel] = yb_val

    await _select_from_contact_or_fallback(
        page,
        "#MainContent_ddlNumberOfFamilies",
        contact,
        ("number_of_families", "numberOfFamilies", "num_families"),
        fallback_value="1",
        fallback_labels=("Single Family",),
    )
    # Retry until the number-of-families value is actually confirmed selected (portal can reset it)
    _nof_raw = _contact_first(contact, "number_of_families", "numberOfFamilies", "num_families") or "1"
    _nof_loc = page.locator("#MainContent_ddlNumberOfFamilies").first
    if await _nof_loc.count() > 0:
        for _nof_attempt in range(1, 7):
            _nof_current = (await _nof_loc.input_value()).strip()
            if _nof_current not in ("", "-1"):
                if _nof_attempt > 1:
                    log("INFO", f"Number of families confirmed as {_nof_current!r} (attempt {_nof_attempt})")
                break
            log("WARN", f"Number of families still unselected after attempt {_nof_attempt}; retrying with value={_nof_raw!r}")
            try:
                await _nof_loc.select_option(value=_nof_raw)
            except Exception:
                try:
                    await _nof_loc.select_option(label=_nof_raw)
                except Exception:
                    pass
            await page.wait_for_timeout(500)
        else:
            log("WARN", "Number of families could not be confirmed after 6 attempts")

    await _select_from_contact_or_fallback(
        page,
        "#MainContent_ddlConstruction",
        contact,
        ("construction", "construction_type", "constructionType"),
        fallback_value="Frame",
        fallback_labels=("Frame",),
    )

    await _select_from_contact_or_fallback(
        page,
        "#MainContent_ddlNamedInsuredType",
        contact,
        ("named_insured_type", "namedInsuredType"),
        fallback_value="Owner",
        fallback_labels=("Owner",),
    )

    dp_sel = "#MainContent_txtDatePurchased"
    loc_dp = page.locator(dp_sel).first
    if await loc_dp.count() > 0 and not (await loc_dp.input_value()).strip():
        dp_raw = _contact_first(contact, "datePurchased", "date_purchased")
        dp_val = _date(str(dp_raw)) if dp_raw else _date("8/1/2023")
        await loc_dp.fill(dp_val)
        verify_fields[dp_sel] = dp_val

    sq_sel = "#MainContent_txtSquareFootage"
    loc_sq = page.locator(sq_sel).first
    if await loc_sq.count() > 0 and not (await loc_sq.input_value()).strip():
        sq_raw = _contact_first(contact, "square_footage", "squareFootage", "sqft")
        sq_val = _square_footage_portal_value(sq_raw) if sq_raw else "2756"
        await loc_sq.fill(sq_val)
        verify_fields[sq_sel] = sq_val

    await _select_number_of_stories_property(page, contact)

    await _select_from_contact_or_fallback(
        page,
        "#MainContent_ddlRoofType",
        contact,
        ("roof_type", "roofType"),
        fallback_value="AS",
        fallback_labels=("Architectural Shingles",),
    )
    await _select_from_contact_or_fallback(
        page,
        "#MainContent_ddlPrimaryHeat",
        contact,
        ("primary_heat", "primaryHeat", "primary_heat_type"),
        fallback_value="Electric",
        fallback_labels=("Electric",),
    )
    await _select_from_contact_or_fallback(
        page,
        "#MainContent_ddlSecondaryHeat",
        contact,
        ("secondary_heat", "secondaryHeat", "secondary_heat_type"),
        fallback_value="None",
        fallback_labels=("None",),
    )
    await _select_from_contact_or_fallback(
        page,
        "#MainContent_ddlStoveOnPremise",
        contact,
        ("solid_fuel_stoves", "solidFuelBurningStoves", "solid_fuel_burning_stoves"),
        fallback_value="False",
        extra_fallback_values=("No", "N"),
        fallback_labels=("No",),
    )
    await _select_from_contact_or_fallback(
        page,
        "#MainContent_ddlRoofShape",
        contact,
        ("roof_shape", "roofShape"),
        fallback_value="Gable, Slight Pitch",
    )
    await _select_from_contact_or_fallback(
        page,
        "#MainContent_ddlRoofHail",
        contact,
        ("roof_hail", "roofHail", "roof_hail_resistant"),
        fallback_value="False",
        fallback_labels=("No",),
    )
    await _select_from_contact_or_fallback(
        page,
        "#MainContent_ddlPropertyUnderConstruction",
        contact,
        (
            "property_under_construction_renovation",
            "under_construction_renovation",
            "major_renovation",
            "majorRenovation",
        ),
        fallback_value="False",
        extra_fallback_values=("No", "N"),
        fallback_labels=("No",),
    )
    await _select_from_contact_or_fallback(
        page,
        "#MainContent_ddlSmokeDetectorsFireExtinguishers",
        contact,
        ("smoke_detectors_fire_extinguishers", "smokeDetectors", "working_smoke_detectors"),
        fallback_value="B",
        fallback_labels=("Both",),
    )
    await _select_from_contact_or_fallback(
        page,
        "#MainContent_ddlDeadBoltLocks",
        contact,
        ("dead_bolt_locks", "deadBoltLocks", "deadbolt_locks"),
        fallback_value="True",
        extra_fallback_values=("Yes", "Y"),
        fallback_labels=("Yes",),
    )
    await _select_from_contact_or_fallback(
        page,
        "#MainContent_ddlBurglarAlarm",
        contact,
        ("burglar_alarm", "burglarAlarm"),
        fallback_value="L",
        fallback_labels=("Local",),
    )
    await _select_from_contact_or_fallback(
        page,
        "#MainContent_ddlFireAlarmSystem",
        contact,
        ("fire_alarm_system", "fireAlarmSystem", "fire_alarm"),
        fallback_value="N",
        fallback_labels=("None",),
    )
    await _select_from_contact_or_fallback(
        page,
        "#MainContent_ddlSprinklerSystem",
        contact,
        ("sprinkler_system", "sprinklerSystem"),
        fallback_value="N",
        fallback_labels=("None",),
    )
    await _select_from_contact_or_fallback(
        page,
        "#MainContent_ddlWaterShutoffSystem",
        contact,
        ("automatic_water_shutoff", "automaticWaterShutoff", "auto_water_shutoff"),
        fallback_value="False",
        extra_fallback_values=("No", "N"),
        fallback_labels=("No",),
    )

    await _select_from_contact_or_fallback(
        page,
        "#MainContent_ddlOilTank",
        contact,
        ("oil_tank", "oilTank"),
        fallback_value="None",
    )

    yrroof_sel = "#MainContent_txtYearRoofRenovation"
    await _page_7_ensure_roof_year_not_older_than_dwelling(page, verify_fields, contact)

    eff_to_use = None
    eff_sel = "#MainContent_txtEffectiveDate"
    loc_eff = page.locator(eff_sel).first
    if await loc_eff.count() > 0:
        eff_cur = (await loc_eff.input_value()).strip()
        eff_raw = contact.get("effective_date") or contact.get("effectiveDate") or ""
        if eff_raw:
            eff_to_use = _date(str(eff_raw))
            await loc_eff.fill(eff_to_use)
            verify_fields[eff_sel] = eff_to_use
        elif not eff_cur:
            eff_to_use = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%m/%d/%Y")
            await loc_eff.fill(eff_to_use)
            verify_fields[eff_sel] = eff_to_use

    await _verify_and_refill(page, verify_fields)

    debug_step(7, "hand off to mortgagee-bill sub-flow", page=page)
    await _page_7_additional_interest_mortgagee_bill_then_next(page, contact)

    await page.wait_for_load_state("networkidle")
    if "PropertyInfo" in page.url and await page.locator("#lstErrors li").count() > 0:
        err = (await page.locator("#lstErrors").inner_text()).strip()
        if err:
            raise RuntimeError(f"Property validation blocked continue: {err}")

# - PAGE 8: UNDERWRITING -----------------------

async def _select_prior_insurance_company(page: Page, contact: dict) -> None:
    sel = "#MainContent_ucPriorPolicyInformation_ddlPriorInsuranceCompany"
    raw = (
        contact.get("prior_carrier_home")
        or contact.get("prior_carrier")
        or contact.get("current_insurer")
        or contact.get("current_home_carrier")
        or ""
    )
    raw_s = str(raw).strip()

    loc = page.locator(sel)
    await loc.wait_for(state="visible", timeout=15000)

    try:
        ui_carrier = (await loc.input_value()).strip()
    except Exception:
        ui_carrier = ""
    if not _dropdown_selection_is_empty(ui_carrier):
        log("INFO", f"Prior carrier already set on portal ({ui_carrier!r}); leaving unchanged")
        return

    options: list[dict[str, str]] = await loc.evaluate(
        """el => Array.from(el.options).filter(o => o.value && o.value !== '-1').map(o => ({ v: o.value, t: (o.textContent || '').trim() }))"""
    )
    if not options:
        raise RuntimeError("Prior insurance company dropdown has no selectable options")

    def norm(s: str) -> str:
        s2 = str(s).replace("&amp;", " and ").replace("&", " and ")
        return re.sub(r"\s+", " ", s2.lower()).strip()

    nr = norm(raw_s) if raw_s else ""

    async def pick_by_value(value: str, log_msg: str | None = None) -> bool:
        try:
            await loc.select_option(value=value, timeout=10000)
            if log_msg:
                log("INFO", log_msg)
            return True
        except Exception:
            try:
                await loc.select_option(label=value, timeout=5000)
                if log_msg:
                    log("INFO", log_msg)
                return True
            except Exception:
                return False

    if not raw_s:
        if await pick_by_value(_UW8_DEFAULT_PRIOR_CARRIER, f"Prior carrier not in CRM; using default {_UW8_DEFAULT_PRIOR_CARRIER!r}"):
            return

    for o in options:
        if o["v"] == raw_s or o["t"] == raw_s:
            await loc.select_option(value=o["v"], timeout=10000)
            return
    for o in options:
        if norm(o["v"]) == nr or norm(o["t"]) == nr:
            log("INFO", f"Prior carrier matched (case-insensitive): {raw_s!r} -> {o['v']!r}")
            await loc.select_option(value=o["v"], timeout=10000)
            return

    for needle, canon in _PRIOR_CARRIER_ALIAS_PREFIXES:
        if needle in nr and await pick_by_value(canon, f"Prior carrier alias {needle!r} -> {canon!r}"):
            return

    if len(nr) >= 4:
        for o in options:
            nv, nt = norm(o["v"]), norm(o["t"])
            if nr in nt or nr in nv:
                log("INFO", f"Prior carrier substring match: {raw_s!r} -> {o['v']!r}")
                await loc.select_option(value=o["v"], timeout=10000)
                return
        for o in options:
            nv, nt = norm(o["v"]), norm(o["t"])
            if len(nt) >= 6 and (nt in nr or (len(nv) >= 6 and nv in nr)):
                log("INFO", f"Prior carrier label contained in CRM text: {raw_s!r} -> {o['v']!r}")
                await loc.select_option(value=o["v"], timeout=10000)
                return

    log("WARN", f"No prior carrier match for {raw_s!r}; trying {_PRIOR_CARRIER_FALLBACK_VALUE!r} then {_UW8_DEFAULT_PRIOR_CARRIER!r}")
    if await pick_by_value(_PRIOR_CARRIER_FALLBACK_VALUE, None):
        return
    if await pick_by_value(_UW8_DEFAULT_PRIOR_CARRIER, None):
        return

    await loc.select_option(value=options[0]["v"], timeout=10000)
    log("WARN", f"Prior carrier fallback used first list entry: {options[0]['v']!r}")


async def _page_8_fill_prior_policy_fields(page: Page, contact: dict) -> dict[str, str]:
    """Fill prior-policy controls only when CRM has no value and the portal field is still unset."""
    verify: dict[str, str] = {}

    cov_sel = "#MainContent_ucPriorPolicyInformation_ddlPriorInsuranceCoverage"
    loc_cov = page.locator(cov_sel).first
    if await loc_cov.count() > 0:
        await loc_cov.wait_for(state="visible", timeout=15000)
        ui_cov = (await loc_cov.input_value()).strip()
        crm_cov = _contact_first(contact, *_UW8_PRIOR_COVERAGE_CONTACT_KEYS)
        if _dropdown_selection_is_empty(ui_cov):
            if crm_cov:
                try:
                    await loc_cov.select_option(value=crm_cov, timeout=8000)
                except Exception:
                    try:
                        await loc_cov.select_option(label=crm_cov, timeout=8000)
                    except Exception:
                        log("WARN", f"Could not apply CRM prior insurance coverage {crm_cov!r}; using default")
                        await loc_cov.select_option(value=_UW8_DEFAULT_PRIOR_COVERAGE, timeout=8000)
            else:
                await loc_cov.select_option(value=_UW8_DEFAULT_PRIOR_COVERAGE, timeout=8000)

    await _select_prior_insurance_company(page, contact)

    exp_sel = "#MainContent_ucPriorPolicyInformation_txtExpirationDate"
    loc_exp = page.locator(exp_sel).first
    if await loc_exp.count() > 0:
        await loc_exp.wait_for(state="visible", timeout=10000)
        ui_exp = (await loc_exp.input_value()).strip()
        crm_exp = _contact_first(contact, *_UW8_PRIOR_EXPIRATION_KEYS)
        if not ui_exp:
            if crm_exp:
                expiry_val = _date(str(crm_exp))
            else:
                expiry_val = _UW8_DEFAULT_PRIOR_EXPIRATION
            await loc_exp.fill(expiry_val)
            verify[exp_sel] = expiry_val

    yrs_sel = "#MainContent_ucPriorPolicyInformation_txtContinuousInsurance"
    loc_yrs = page.locator(yrs_sel).first
    if await loc_yrs.count() > 0:
        await loc_yrs.wait_for(state="visible", timeout=10000)
        ui_yrs = (await loc_yrs.input_value()).strip()
        crm_yrs = _contact_first(contact, *_UW8_CONTINUOUS_YEARS_KEYS)
        if not ui_yrs:
            if crm_yrs:
                years_val = str(crm_yrs).strip()
            else:
                years_val = _UW8_DEFAULT_CONTINUOUS_YEARS
            await loc_yrs.fill(years_val)
            verify[yrs_sel] = years_val

    return verify


_UW_QUESTION_RULES = [
    # (keywords, want_val, crm_keys)
    (["paperless", "electronic"], "True", ("uw_go_paperless", "go_paperless", "paperless")),
    (["site access", "underwriting site"], _UW8_SITE_ACCESS_VALUE, ("uw_site_access", "site_access", "underwriting_site_access")),
    (["trampoline", "unnetted", "flat ground"], "False", ("uw_trampoline", "trampoline_on_premise")),
    (["pool", "swimming", "diving board", "slide"], "N", ("uw_swimming_pool", "swimming_pool", "pool_on_premises")),
    (["lapse", "without coverage"], "False", ("uw_coverage_lapse", "coverage_lapse_12_months")),
    (["flood", "wave wash", "sinkhole"], "False", ("uw_flood_area", "wave_wash_sinkhole_area", "property_flood_zone")),
    (["debris", "unregistered", "inoperable"], "No", ("uw_debris", "debris_on_premises")),
    (["commercial", "business", "day care", "farming", "farm"], "False", ("uw_commercial_exposure", "commercial_exposure")),
    (["dog", "animal", "bite", "exotic"], "False", ("uw_animals", "animals_on_premise")),
    (["loss", "claim", "damage"], "False", ("uw_prior_loss", "prior_losses")),
    (["polybutylene", "knob", "tube", "fuse"], "False", ("uw_wiring",)),
    (["foreclosure", "bankrupt"], "False", ("uw_foreclosure",)),
]


async def _page_8_fill_additional_questions(page: Page, contact: dict) -> None:
    """Additional Questions: fill from CRM if available, otherwise use keyword-matched defaults."""
    base = "#MainContent_ucUnderwritingQuestions_rpParentQuestions_ddlAnswer_"

    # Discover all parent question dropdowns on the page
    all_parent_selects = await page.locator(f"select[id^='MainContent_ucUnderwritingQuestions_rpParentQuestions_ddlAnswer_']").all()
    log("INFO", f"Underwriting: found {len(all_parent_selects)} parent question dropdowns")

    for loc_idx, loc in enumerate(all_parent_selects):
        try:
            if not await loc.is_visible():
                continue
        except Exception:
            continue
        try:
            cur = (await loc.input_value()).strip()
        except Exception:
            cur = ""

        # Read valid options
        valid_options = []
        try:
            options = await loc.locator("option").all()
            for opt in options:
                val = (await opt.get_attribute("value") or "").strip()
                lbl = (await opt.text_content() or "").strip()
                if val in ("", "-1") or "select" in lbl.lower() or "choose" in lbl.lower():
                    continue
                valid_options.append({"value": val, "label": lbl})
        except Exception as e:
            log("WARN", f"Underwriting question {loc_idx}: could not read options: {e}")
            continue

        if not valid_options:
            continue

        # Extract question text to determine rule
        q_text = ""
        try:
            q_text = await loc.evaluate("el => { let tr = el.closest('tr, li'); return tr ? tr.innerText : el.parentElement.innerText; }")
            q_text = q_text.lower()
        except Exception:
            pass

        want_val = "False"  # Safe default for unknown questions
        crm_keys: tuple = ()
        for kw_list, w_val, c_keys in _UW_QUESTION_RULES:
            if any(kw in q_text for kw in kw_list):
                want_val = w_val
                crm_keys = c_keys
                break

        # Check if a value is already selected (non-empty and not "-1")
        if not _dropdown_selection_is_empty(cur):
            # If it's the trampoline question and currently Yes/True, force it to No
            if "trampoline" in q_text and cur.lower() not in ("false", "no", "n", "0"):
                log("INFO", f"Trampoline question currently {cur!r}, forcing to {want_val!r}")
            else:
                continue

        selected = False

        # 1. Try to select the CRM value first
        crm_val = _contact_first(contact, *crm_keys) if crm_keys else None
        if crm_val:
            crm_val_str = str(crm_val).strip().lower()
            # Try to match the CRM value with option values or labels
            for opt in valid_options:
                if opt["value"].lower() == crm_val_str or opt["label"].lower() == crm_val_str:
                    try:
                        await loc.select_option(value=opt["value"])
                        selected = True
                        log("INFO", f"Underwriting {loc_idx}: selected CRM value {opt['label']!r}")
                        break
                    except Exception:
                        pass
            # Try Boolean translation for CRM value
            if not selected:
                if crm_val_str in ("yes", "true", "y", "1"):
                    for opt in valid_options:
                        if opt["value"].lower() in ("true", "yes", "y", "1") or opt["label"].lower() in ("true", "yes", "y", "1"):
                            try:
                                await loc.select_option(value=opt["value"])
                                selected = True
                                log("INFO", f"Underwriting {loc_idx}: selected CRM Yes/True equivalent")
                                break
                            except Exception:
                                pass
                elif crm_val_str in ("no", "false", "n", "0"):
                    for opt in valid_options:
                        if opt["value"].lower() in ("false", "no", "n", "0") or opt["label"].lower() in ("false", "no", "n", "0"):
                            try:
                                await loc.select_option(value=opt["value"])
                                selected = True
                                log("INFO", f"Underwriting {loc_idx}: selected CRM No/False equivalent")
                                break
                            except Exception:
                                pass

        # 2. Fallback: use the configured want_val default, then prefer No/False for safety
        if not selected:
            wv_lower = want_val.strip().lower()
            for opt in valid_options:
                if opt["value"].lower() == wv_lower or opt["label"].lower() == wv_lower:
                    try:
                        await loc.select_option(value=opt["value"])
                        selected = True
                        log("INFO", f"Underwriting {loc_idx}: Selected configured default {opt['label']!r}")
                        break
                    except Exception:
                        pass

            if not selected:
                chosen = next(
                    (o for o in valid_options if o["value"].lower() in ("false", "no", "n", "0") or o["label"].lower() in ("false", "no", "n", "0")),
                    valid_options[0],
                )
                try:
                    await loc.select_option(value=chosen["value"])
                    selected = True
                    log("INFO", f"Underwriting {loc_idx} (fallback): Selected {chosen['label']!r}")
                except Exception:
                    pass

        if selected:
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                await page.wait_for_timeout(400)


async def _page_8_fill_child_questions(page: Page, *, retries: int = 3) -> None:
    """
    After parent questions are answered, check for any visible child sub-questions.
    Q6a (trampoline sub-question) should NOT appear because the trampoline parent is always
    forced to No/False before this function runs. If Q6a appears anyway, we log a warning
    and leave it at its current value rather than force Yes (which could cause underwriting issues).
    Also scans for any other child sub-questions (not in the 9 known parent IDs) and answers Yes.
    Retries up to `retries` times with a 2s wait between passes.
    """
    # Q6a: trampoline sub-question — should not appear since parent trampoline is always No.
    # If it does appear unexpectedly, warn and do not change it.
    for attempt in range(1, retries + 1):
        q6a_found = False
        child_locs = await page.locator("select[id*='_rpChildQuestions_']").all()
        for c in child_locs:
            if await c.is_visible():
                q6a_found = True
                
        if not q6a_found:
            break

        await page.wait_for_timeout(2000)


async def _page_8_answer_empty_page_selects(page: Page) -> int:
    """
    Scan the ENTIRE page for any visible, unanswered <select> elements
    that are not in the Prior Policy section (those are already handled).
    Returns the number of selects that were filled.
    """
    _SKIP_IDS = {
        "MainContent_ucPriorPolicyInformation_ddlPriorInsuranceCoverage",
        "MainContent_ucPriorPolicyInformation_ddlPriorInsuranceCompany",
    }
    filled = 0
    all_selects = page.locator("select")
    n = await all_selects.count()
    for idx in range(n):
        loc = all_selects.nth(idx)
        try:
            if not await loc.is_visible():
                continue
            sel_id = (await loc.get_attribute("id") or "").strip()
            if any(skip in sel_id for skip in _SKIP_IDS):
                continue
            cur = (await loc.input_value()).strip()
            if not _dropdown_selection_is_empty(cur):
                continue  # already answered
            # Read valid options
            options = await loc.locator("option").all()
            valid_opts = []
            for opt in options:
                val = (await opt.get_attribute("value") or "").strip()
                lbl = (await opt.text_content() or "").strip()
                if val in ("", "-1") or "select" in lbl.lower() or "choose" in lbl.lower():
                    continue
                valid_opts.append({"value": val, "label": lbl})
            if not valid_opts:
                continue
            # Prefer False/No for safety for generic empty questions
            chosen = next(
                (o for o in valid_opts if o["value"].lower() in ("false", "no", "n", "0") or o["label"].lower() in ("false", "no", "n", "0")),
                valid_opts[0],
            )
            await loc.select_option(value=chosen["value"], timeout=8000)
            log("INFO", f"Answered empty select {sel_id!r} -> {chosen['label']!r}")
            filled += 1
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                await page.wait_for_timeout(500)
        except Exception as exc:
            log("WARN", f"Could not answer select idx={idx}: {exc}")
    return filled


async def _page_8_click_continue(page: Page) -> None:
    """Click Continue on the Underwriting page; if validation errors mention unanswered
    sub-questions (like 6a), find and answer them, then retry — up to 3 rounds."""
    for round_num in range(1, 4):
        # Click the Continue button
        clicked = False
        for sel in ["#MainContent_btnContinue", "input[name$='btnContinue']", "button:has-text('Continue')", "button:has-text('Next')"]:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=10000)
                    clicked = True
                    break
            except Exception:
                pass
        if not clicked:
            try:
                await page.evaluate("document.getElementById('MainContent_btnContinue')?.click()")
                clicked = True
            except Exception:
                pass
        if not clicked:
            raise RuntimeError("Could not click Continue on Underwriting")

        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            await page.wait_for_timeout(1000)

        # Check for validation errors about unanswered Additional Questions
        error_texts: list[str] = []
        try:
            error_items = page.locator("#lstErrors li")
            ec = await error_items.count()
            for ei in range(ec):
                txt = (await error_items.nth(ei).inner_text()).strip()
                error_texts.append(txt)
        except Exception:
            pass

        addl_q_errors = [t for t in error_texts if "additional question" in t.lower()]
        if not addl_q_errors:
            # No additional-question errors — continue was accepted (or different error, not our concern here)
            return

        log("WARN", f"Round {round_num}: validation errors about unanswered questions: {addl_q_errors}")
        await page.wait_for_timeout(1500)
        filled = await _page_8_answer_empty_page_selects(page)
        log("INFO", f"Round {round_num}: answered {filled} previously empty select(s) — retrying Continue")
        if filled == 0:
            log("WARN", "No empty selects found to answer — cannot resolve validation error")
            raise RuntimeError(f"Underwriting validation error after {round_num} rounds: {addl_q_errors[0]}")

    raise RuntimeError("Could not pass Underwriting validation after 3 rounds of answering sub-questions")


async def page_8_underwriting(page: Page, contact: dict) -> None:
    debug_step(8, "fill prior policy + underwriting questions", page=page)
    verify_fields = await _page_8_fill_prior_policy_fields(page, contact)
    await _page_8_fill_additional_questions(page, contact)
    await _page_8_fill_child_questions(page)

    if verify_fields:
        await _verify_and_refill(page, verify_fields)

    await _page_8_click_continue(page)

# - PAGE 9: LOSS HISTORY (click continue) --------------â”€

async def page_9_loss_history(page: Page) -> None:
    debug_step(9, "click Continue through loss history", page=page)
    # Inline click next logic with fallbacks
    clicked = False
    for attempt in range(3):
        for sel in ["#MainContent_btnContinue", "input[name$='btnContinue']", "button:has-text('Continue')", "button:has-text('Next')"]:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=10000)
                    clicked = True
                    break
            except Exception:
                pass
        if clicked:
            break
        try:
            await page.evaluate("document.getElementById('MainContent_btnContinue')?.click()")
            clicked = True
            break
        except Exception:
            pass
        await page.wait_for_timeout(1000)
        
    if not clicked:
        raise RuntimeError("Could not click Continue on Loss History")

    await page.wait_for_load_state("networkidle")

# - PAGE 10: COVERAGE ------------------------â”€

async def _answer_additional_coverage_questions(page: Page) -> None:
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
                log("INFO", f"Additional coverage question -> select value={val!r}")
                await page.wait_for_load_state("networkidle")
                return True
            except Exception:
                continue
        for lab in ("Yes", "Y"):
            try:
                await sel.select_option(label=lab, timeout=5000)
                log("INFO", f"Additional coverage question -> select label={lab!r}")
                await page.wait_for_load_state("networkidle")
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
                log("INFO", f"Additional coverage radio -> {val!r}")
                await page.wait_for_load_state("networkidle")
                return True
        yes_lbl = li.locator("label").filter(has_text=re.compile(r"^\s*yes\s*$", re.I)).first
        if await yes_lbl.count() > 0:
            await yes_lbl.click(timeout=5000)
            log("INFO", "Additional coverage radio -> clicked Yes label")
            await page.wait_for_load_state("networkidle")
            return True
        return False

    lbls = page.locator("label")
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
            tg = page.locator(f"#{fid}")
            filled = await yes_select(tg)
        if not filled:
            li = lb.locator("xpath=ancestor::li[1]")
            filled = await yes_radio(li)
            if not filled:
                filled = await yes_select(li.locator("select").first)
        if filled:
            log("INFO", f"Additional coverage label matched: {txt[:100]!r}")

_COVERAGE_A_DEFAULT = "452937"
_COVERAGE_A_CONTACT_KEYS: tuple[str, ...] = (
    "coverage_a",
    "coverageA",
    "dwelling_limit",
    "dwellingLimit",
    "dwelling_coverage",
    "dwellingCoverage",
)


def _coverage_a_digits(raw: str | None) -> str | None:
    if raw is None or not str(raw).strip():
        return None
    digits = re.sub(r"\D", "", str(raw))
    if not digits or int(digits) <= 0:
        return None
    return digits


def _coverage_a_from_contact(contact: dict | None) -> str | None:
    if not contact:
        return None
    raw = _contact_first(contact, *_COVERAGE_A_CONTACT_KEYS)
    return _coverage_a_digits(raw)


async def _resolve_coverage_a_amount(
    page: Page, selector: str, contact: dict | None
) -> tuple[str, str]:
    """Portal prefill first, then GHL, then backup. Returns (amount_digits, source)."""
    portal_digits: str | None = None
    loc = page.locator(selector).first
    if await loc.count() > 0:
        try:
            portal_digits = _coverage_a_digits(await loc.input_value())
        except Exception:
            portal_digits = None
    if portal_digits:
        return portal_digits, "portal"

    ghl_digits = _coverage_a_from_contact(contact)
    if ghl_digits:
        return ghl_digits, "ghl"

    return _COVERAGE_A_DEFAULT, "default"


async def _coverage_fill_amount_and_change(page: Page, selector: str, amount: str) -> None:
    loc = page.locator(selector).first
    if await loc.count() == 0:
        return
    try:
        await loc.fill(amount)
    except Exception as exc:
        log("WARN", f"Coverage amount fill failed ({selector}): {exc}")
        try:
            await loc.evaluate("(el, v) => { el.value = v }", amount)
        except Exception:
            return
    try:
        await loc.evaluate(
            """el => {
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.blur();
            }"""
        )
    except Exception as exc:
        log("WARN", f"Coverage amount change events failed ({selector}): {exc}")
    try:
        await page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        await page.wait_for_timeout(800)


async def _coverage_select_value_postback(page: Page, selector: str, value: str) -> None:
    loc = page.locator(selector).first
    if await loc.count() == 0:
        return
    try:
        await loc.select_option(value=value, timeout=15000)
    except Exception as exc:
        log("WARN", f"Could not select value {value!r} on {selector}: {exc}")
        return
    try:
        await page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        await page.wait_for_timeout(800)


_PERILS_DEDUCTIBLE_SEL = "#MainContent_ddlPerils"
_PERILS_DEDUCTIBLE_PREFERRED = "0.010"


async def _page_10_read_dropdown_value(page: Page, selector: str) -> str:
    loc = page.locator(selector)
    if await loc.count() == 0:
        return ""
    try:
        return (await loc.first.input_value()).strip()
    except Exception:
        return ""


async def _page_10_ensure_perils_deductible(page: Page) -> None:
    """All Perils Deductible is required; ddlPerils has no onchange — set value + verify before Continue."""
    debug_step(
        10,
        "selecting All Perils Deductible",
        f"preferred={_PERILS_DEDUCTIBLE_PREFERRED}",
        page=page,
    )
    preferred = _PERILS_DEDUCTIBLE_PREFERRED
    fallbacks = ("0.005", "0.020", "1000.000", "500.000")

    for attempt in range(4):
        try:
            await page.wait_for_selector(_PERILS_DEDUCTIBLE_SEL, state="visible", timeout=20000)
        except PlaywrightTimeoutError:
            log("WARN", f"All Perils Deductible not visible (attempt {attempt + 1})")
            await page.wait_for_timeout(600)
            continue

        loc = page.locator(_PERILS_DEDUCTIBLE_SEL).first
        if await loc.count() == 0:
            log("WARN", f"All Perils Deductible locator empty (attempt {attempt + 1})")
            await page.wait_for_timeout(600)
            continue

        try:
            await loc.scroll_into_view_if_needed(timeout=8000)
        except Exception:
            pass

        cur = await _page_10_read_dropdown_value(page, _PERILS_DEDUCTIBLE_SEL)
        if cur and cur not in ("-1", ""):
            log("INFO", f"All Perils Deductible already {cur!r}")
            return

        picked = False
        label_by_value = {"0.010": "1%", "0.005": ".5%", "0.020": "2%", "0.030": "3%", "0.050": "5%"}
        for val in (preferred,) + fallbacks:
            try:
                await loc.select_option(value=val, timeout=10000)
                picked = True
            except Exception:
                lbl = label_by_value.get(val)
                if lbl:
                    try:
                        await loc.select_option(label=lbl, timeout=5000)
                        picked = True
                    except Exception:
                        picked = False
            if not picked:
                try:
                    picked = await loc.evaluate(
                        """(el, val) => {
                            for (const o of el.options) {
                                if (o.value === val) {
                                    el.value = val;
                                    return true;
                                }
                            }
                            return false;
                        }""",
                        val,
                    )
                except Exception:
                    picked = False
            if picked:
                try:
                    await loc.evaluate(
                        """el => {
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                            el.blur();
                        }"""
                    )
                except Exception:
                    pass
                log("INFO", f"All Perils Deductible set to {val!r}")
                break

        if not picked:
            try:
                opts: list[str] = await loc.evaluate(
                    """el => [...el.options]
                        .filter(o => o.value && o.value !== '-1' && !o.disabled)
                        .map(o => o.value)"""
                )
                if opts:
                    await loc.select_option(value=opts[0], timeout=10000)
                    log("WARN", f"All Perils Deductible: fell back to first option {opts[0]!r}")
                    picked = True
            except Exception as exc:
                log("WARN", f"All Perils Deductible fallback failed: {exc}")

        cur = await _page_10_read_dropdown_value(page, _PERILS_DEDUCTIBLE_SEL)
        if cur and cur not in ("-1", ""):
            return

        await page.wait_for_timeout(500)

    cur = await _page_10_read_dropdown_value(page, _PERILS_DEDUCTIBLE_SEL)
    raise RuntimeError(
        f"Could not select All Perils Deductible ({_PERILS_DEDUCTIBLE_SEL!r}); current value={cur!r}"
    )


async def page_10_coverage(page: Page, contact: dict) -> None:
    debug_step(10, "answer additional coverage questions", page=page)
    log("INFO", "Starting page_10_coverage: Checking additional coverage questions...")
    await _answer_additional_coverage_questions(page)

    perils_sel = "#MainContent_ddlPerils"
    cov_a_sel = "#MainContent_ucCoreCoverages_rptCoreCoverages_txtCovA_0"

    log("INFO", f"Waiting for {cov_a_sel} and {perils_sel} to be visible")
    try:
        await page.wait_for_selector(cov_a_sel, state="visible", timeout=30000)
        await page.wait_for_selector(perils_sel, state="visible", timeout=30000)
    except PlaywrightTimeoutError:
        log("WARN", "Coverage controls not visible yet; retrying additional questions + wait")
        await _answer_additional_coverage_questions(page)
        try:
            await page.wait_for_selector(cov_a_sel, state="visible", timeout=30000)
            await page.wait_for_selector(perils_sel, state="visible", timeout=30000)
        except Exception as e:
            log("WARN", f"Coverage controls still not visible: {e}")

    try:
        await page.locator(perils_sel).scroll_into_view_if_needed()
    except Exception as e:
        log("WARN", f"Could not scroll {perils_sel} into view: {e}")

    cov_a_amount, cov_a_source = await _resolve_coverage_a_amount(page, cov_a_sel, contact)
    if cov_a_source == "portal":
        log("INFO", f"Coverage A: keeping portal prefill {cov_a_amount!r}")
    elif cov_a_source == "ghl":
        log("INFO", f"Coverage A: filling from GHL {cov_a_amount!r}")
        await _coverage_fill_amount_and_change(page, cov_a_sel, cov_a_amount)
    else:
        log("INFO", f"Coverage A: filling backup default {cov_a_amount!r}")
        await _coverage_fill_amount_and_change(page, cov_a_sel, cov_a_amount)

    log("INFO", "Forcing Coverage B/C/D and Fair Rental to target percentages (portal postbacks)")
    await _coverage_select_value_postback(
        page, "#MainContent_ucCoreCoverages_rptCoreCoverages_ddlCovB_1", "0.0500"
    )
    await _coverage_select_value_postback(
        page, "#MainContent_ucCoreCoverages_rptCoreCoverages_ddlCovC_2", "0.2500"
    )
    await _coverage_select_value_postback(
        page, "#MainContent_ucCoreCoverages_rptCoreCoverages_ddlCovD_3", "0.1500"
    )
    await _coverage_select_value_postback(
        page, "#MainContent_ucCoreCoverages_rptCoreCoverages_ddlFairRentalValue_4", "0.0000"
    )

    await _page_10_ensure_perils_deductible(page)

    cov_a = page.locator(cov_a_sel)
    if await cov_a.count() > 0:
        log("INFO", "Verifying Coverage A")
        await _verify_and_refill(
            page, {"#MainContent_ucCoreCoverages_rptCoreCoverages_txtCovA_0": cov_a_amount}
        )

    # Inline click next logic with fallbacks
    log("INFO", "Attempting to click Continue/Next on Coverage page")
    navigated = False
    
    for attempt in range(3):
        log("INFO", f"Continue click attempt {attempt + 1}")
        click_success = False
        for sel in ["#MainContent_btnContinue", "input[name$='btnContinue']", "button:has-text('Continue')", "button:has-text('Next')"]:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    log("INFO", f"Clicking {sel} using playwright")
                    await btn.click(timeout=10000)
                    click_success = True
                    break
            except Exception as e:
                log("WARN", f"Failed clicking {sel}: {e}")
                
        if not click_success:
            log("INFO", "Playwright click failed, trying JS evaluation")
            try:
                await page.evaluate("document.getElementById('MainContent_btnContinue')?.click()")
                log("INFO", "Clicked #MainContent_btnContinue via JS")
                click_success = True
            except Exception as e:
                log("WARN", f"JS click failed: {e}")
                
        if click_success:
            log("INFO", "Wait for networkidle after click...")
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception as e:
                log("WARN", f"Wait for networkidle timed out or failed: {e}")
                
            if "Coverage" not in page.url:
                log("INFO", "Successfully navigated away from Coverage page")
                navigated = True
                break
            else:
                log("WARN", "Still on Coverage page after clicking. Checking for validation errors...")
                if await page.locator("#lstErrors li").count() > 0:
                    err = (await page.locator("#lstErrors").inner_text()).strip()
                    if err:
                        log("WARN", f"Coverage validation errors found: {err}")
                        if "perils" in err.lower():
                            await _page_10_ensure_perils_deductible(page)
        
        await page.wait_for_timeout(1000)
        
    if not navigated:
        if "Coverage" in page.url and await page.locator("#lstErrors li").count() > 0:
            err = (await page.locator("#lstErrors").inner_text()).strip()
            raise RuntimeError(f"Coverage validation blocked continue: {err}")
        else:
            raise RuntimeError("Could not navigate away from Coverage page after 3 click attempts")
            
    log("INFO", "Completed page_10_coverage")

# - PAGE 11: DRIVER INFO -----------------------

async def page_11_driver(page: Page, contact: dict) -> None:
    debug_step(11, "fill all drivers on quote (primary + additional)", page=page)

    async def _driver_field_visible(selector: str) -> bool:
        loc = page.locator(selector).first
        if await loc.count() == 0:
            return False
        try:
            return await loc.is_visible()
        except Exception:
            return False

    async def _ensure_primary_driver_form() -> None:
        """Open driver 1 edit form when the grid is shown; wait until name fields are visible."""
        if await _driver_field_visible("#MainContent_txtFirstName"):
            return
        if await page.locator("#MainContent_gvDrivers").count() == 0:
            await page.wait_for_selector("#MainContent_txtFirstName", state="visible", timeout=20000)
            return
        view_edit = page.locator("#MainContent_gvDrivers tbody tr a:has-text('View/Edit')").first
        await view_edit.wait_for(state="visible", timeout=15000)
        clicked = False
        for _ in range(3):
            try:
                await view_edit.click(timeout=10000)
                clicked = True
                break
            except Exception:
                await page.wait_for_timeout(400)
        if not clicked:
            await page.evaluate(
                """() => {
                    const el = Array.from(document.querySelectorAll("#MainContent_gvDrivers a"))
                      .find((a) => (a.textContent || "").includes("View/Edit"));
                    if (el) el.click();
                }"""
            )
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            await page.wait_for_timeout(600)
        await page.wait_for_selector("#MainContent_txtFirstName", state="visible", timeout=20000)

    await _ensure_primary_driver_form()

    async def _fill_text_if_visible(selector: str, value: str) -> None:
        loc = page.locator(selector).first
        if await loc.count() == 0:
            return
        try:
            if not await loc.is_visible():
                return
        except Exception:
            return
        await loc.fill(value)

    async def _force_years_experience_four() -> None:
        """Years experience must be 4 for this flow (portal often shows 0 after postbacks)."""
        sel = "#MainContent_txtYearsExperience"
        loc = page.locator(sel).first
        if await loc.count() == 0:
            return
        try:
            if not await loc.is_visible():
                return
        except Exception:
            return
        await loc.fill("4")
        try:
            await loc.evaluate(
                """el => {
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.blur();
                }"""
            )
        except Exception:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            await page.wait_for_timeout(400)

    async def _fill_primary_driver_from_contact() -> None:
        """Populate first-driver edit form from GHL; use reference defaults only when CRM is empty."""
        await page.wait_for_selector("#MainContent_txtFirstName", state="visible", timeout=20000)
        fn = _contact_first(contact, "firstName", "first_name")
        ln = _contact_first(contact, "lastName", "last_name")
        if fn:
            await _fill_text_if_visible("#MainContent_txtFirstName", fn)
        if ln:
            await _fill_text_if_visible("#MainContent_txtLastName", ln)
        mid = _contact_first(contact, "middleName", "middle_name")
        if mid:
            await _fill_text_if_visible("#MainContent_txtMiddleName", mid)

        dob_raw = _contact_first(contact, "dateOfBirth", "date_of_birth")
        dob = _date(dob_raw) if dob_raw else "1/1/1990"
        await _fill_text_if_visible("#MainContent_txtDateOfBirth", dob)
        # Fire focusout (NOT change — change triggers __doPostBack which reloads the page)
        # focusout runs DefensiveDriverCourseVisibilityToggle via jQuery without any postback
        try:
            await page.evaluate(
                """() => {
                    const el = document.getElementById('MainContent_txtDateOfBirth');
                    if (el) el.dispatchEvent(new Event('focusout', {bubbles: true}));
                }"""
            )
            await page.wait_for_timeout(300)
        except Exception:
            pass

        g = _driver_gender_portal_value(_contact_first(contact, "gender"))
        await _select_if_present("#MainContent_ddlGender", g, wait_postback=True)
        ms = _driver_marital_portal_value(_contact_first(contact, "maritalStatus", "marital_status"))
        await _select_if_present("#MainContent_ddlMaritalStatus", ms, wait_postback=True)

        occ_raw = _contact_first(contact, "occupation") or ""
        occ_val = _occupation_value_for_ng360_dropdown(occ_raw)
        occ_sel = page.locator("#MainContent_ddlOccupation").first
        if await occ_sel.count() > 0:
            try:
                if await occ_sel.is_visible():
                    try:
                        await occ_sel.select_option(value=occ_val, timeout=10000)
                    except Exception:
                        await occ_sel.select_option(label=occ_val, timeout=5000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=20000)
                    except Exception:
                        await page.wait_for_timeout(600)
            except Exception:
                pass

        detail = page.locator("#MainContent_txtOccupationDetail").first
        if await detail.count() > 0:
            try:
                if await detail.is_visible():
                    raw_note = (occ_raw or "").strip()
                    if occ_val == "Other" or raw_note:
                        note = raw_note[:120] if raw_note and raw_note.casefold() != "other" else ""
                        if note:
                            await detail.fill(note)
            except Exception:
                pass

        edu = _driver_education_portal_value(
            _contact_first(contact, "educationLevel", "education_level", "education")
        )
        await _select_if_present("#MainContent_ddlEducationLevel", edu, wait_postback=False)

        hh = page.locator("#MainContent_ddlDriverHouseholdMember").first
        if await hh.count() > 0:
            try:
                if await hh.is_visible():
                    cur = (await hh.input_value()).strip()
                    if cur in ("", "-1"):
                        try:
                            await hh.select_option(value="True")
                        except Exception:
                            await hh.select_option(label="Yes")
            except Exception:
                pass

    async def _select_if_present(selector: str, value: str, wait_postback: bool = False, label_fallback: str | None = None) -> None:
        if await page.locator(selector).count() == 0:
            return
        field = page.locator(selector)
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
                await page.wait_for_load_state("networkidle")

    async def _set_required_select(selector: str, value: str, label: str, wait_postback: bool = False) -> None:
        if await page.locator(selector).count() == 0:
            return
        try:
            if not await page.locator(selector).first.is_visible():
                log("INFO", f"Driver field hidden; skipping required select: {label}")
                return
        except Exception:
            return
        for _ in range(2):
            await _select_if_present(selector, value, wait_postback=wait_postback, label_fallback=label)
            current = (await page.locator(selector).input_value()).strip()
            if current not in ("", "-1"):
                return
        raise RuntimeError(f"Driver field still unselected after fill: {label}")

    async def _force_active_license_status() -> None:
        sel = "#MainContent_ddlDriversLicenseStatus"
        if await page.locator(sel).count() == 0:
            return
        for _ in range(3):
            try:
                await page.locator(sel).select_option(value="Active")
            except Exception:
                try:
                    await page.locator(sel).select_option(label="Active")
                except Exception:
                    pass
            current = (await page.locator(sel).input_value()).strip()
            if current == "Active":
                return
            await page.wait_for_timeout(250)
        raise RuntimeError("Driver License Status could not be forced to Active")

    async def _force_losses_in_5_years_no() -> None:
        sel = "#MainContent_ddlLossesIn5Years"
        if await page.locator(sel).count() == 0:
            return
        try:
            if not await page.locator(sel).first.is_visible():
                return
        except Exception:
            return
        for _ in range(3):
            try:
                await page.locator(sel).select_option(value="False")
            except Exception:
                try:
                    await page.locator(sel).select_option(label="No")
                except Exception:
                    pass
            current = (await page.locator(sel).input_value()).strip()
            if current == "False":
                return
            await page.wait_for_timeout(250)
        raise RuntimeError("Losses in 5 Years could not be forced to No/False")

    async def _force_defensive_driver_course_no() -> None:
        """Find defensive driver dropdown, choose No, verify. Retry up to 4 times."""
        dd_id = "MainContent_ddlDefensiveDriverCourse"
        sel = f"#{dd_id}"

        if await page.locator(sel).count() == 0:
            log("INFO", "Defensive Driver Course dropdown not found; skipping")
            return

        for attempt in range(1, 5):
            # Use JavaScript to: enable the dropdown, find the "No" option, select it, fire change event
            await page.evaluate(
                """(ddId) => {
                    const el = document.getElementById(ddId);
                    if (!el) return;
                    el.disabled = false;
                    el.removeAttribute('disabled');
                    for (let i = 0; i < el.options.length; i++) {
                        const t = el.options[i].text.trim().toLowerCase();
                        const v = el.options[i].value.trim().toLowerCase();
                        if (t === 'no' || v === 'false' || v === 'no' || v === 'n') {
                            el.selectedIndex = i;
                            break;
                        }
                    }
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }""",
                dd_id,
            )
            await page.wait_for_timeout(500)

            # Check if it worked
            try:
                val = (await page.locator(sel).input_value()).strip().lower()
            except Exception:
                val = ""
            if val in ("false", "no", "n", "0"):
                log("INFO", f"Defensive Driver Course set to No (attempt {attempt})")
                # Clear the date fields so backend doesn't complain
                try:
                    await page.evaluate("document.getElementById('MainContent_txtDefensiveDriverCourseCompletionDate').value = '';")
                except Exception:
                    pass
                try:
                    await page.evaluate("document.getElementById('MainContent_txtDefensiveDriverCourseExpirationDate').value = '';")
                except Exception:
                    pass
                return

            log("WARN", f"Defensive Driver Course is {val!r} after attempt {attempt}, retrying...")

        log("WARN", "Defensive Driver Course could not be set to No after 4 attempts - continuing anyway")

    async def _apply_dynamic_drive_program() -> None:
        """Program Participant from CRM; default Yes. When Yes, cell + email from GHL (reference UI)."""
        sel = "#MainContent_ddlConnectedDriverOptIn"
        if await page.locator(sel).count() == 0:
            return
        try:
            if not await page.locator(sel).first.is_visible():
                return
        except Exception:
            return
        raw = _contact_first(
            contact,
            "connected_driver_opt_in",
            "dynamicDriveParticipant",
            "dynamic_drive_participant",
            "program_participant",
        )
        participate = _driver_connected_program_participate(raw)
        val = "True" if participate else "False"
        await _select_if_present(sel, val, wait_postback=True, label_fallback="Yes" if participate else "No")
        if not participate:
            return
        phone = _contact_first(contact, "phone", "phoneNumber", "mobile")
        if phone:
            try:
                area, prefix, line = split_phone_number(phone)
                await _fill_text_if_visible("#MainContent_txtAreaCode", area)
                await _fill_text_if_visible("#MainContent_txtPrefix", prefix)
                await _fill_text_if_visible("#MainContent_txtLineNumber", line)
            except Exception as exc:
                log("WARN", f"Could not parse primary phone for Dynamic Drive cell fields: {exc}")
        em = _contact_first(contact, "email")
        if em:
            await _fill_text_if_visible("#MainContent_txtEmail", em)

    async def _fill_driver_required_once(*, second: bool = False) -> None:
        await _select_if_present("#MainContent_ddlOperatorType", "Operator", wait_postback=True, label_fallback="Operator")
        await _set_required_select("#MainContent_ddlLossesIn5Years", "False", "Losses in 5 years")
        await _force_losses_in_5_years_no()
        # Force defensive driver No regardless of visibility
        if await page.locator("#MainContent_ddlDefensiveDriverCourse").count() > 0:
            await _force_defensive_driver_course_no()
        if second:
            await _select_if_present(
                "#MainContent_ddlConnectedDriverOptIn",
                "False",
                wait_postback=True,
                label_fallback="No",
            )
            await _set_required_select("#MainContent_ddlDriversLicenseStatus", "Active", "Driver License Status", wait_postback=True)
            await _force_active_license_status()
            await _select_if_present("#MainContent_ddlLicenseState", "GA")
            await _force_active_license_status()
        else:
            await _apply_dynamic_drive_program()
            await _set_required_select("#MainContent_ddlDriversLicenseStatus", "Active", "Driver License Status", wait_postback=True)
            await _force_active_license_status()
            await _select_if_present("#MainContent_ddlLicenseState", "GA")
            await _force_active_license_status()

    async def _driver_missing_required_fields() -> list[str]:
        missing: list[str] = []
        # License status and losses: only flag if unselected (visible field, empty/default)
        for selector, label in (
            ("#MainContent_ddlDriversLicenseStatus", "Driver License Status"),
            ("#MainContent_ddlLossesIn5Years", "Losses in 5 Years"),
        ):
            if await page.locator(selector).count() == 0:
                continue
            try:
                if not await page.locator(selector).first.is_visible():
                    continue
            except Exception:
                continue
            cur = (await page.locator(selector).input_value()).strip()
            if cur in ("", "-1"):
                missing.append(label)
        # Defensive driver: flag if visible and NOT set to No — catches both empty and "Yes"
        dd_sel = "#MainContent_ddlDefensiveDriverCourse"
        if await page.locator(dd_sel).count() > 0:
            try:
                if await page.locator(dd_sel).first.is_visible():
                    dd_val = (await page.locator(dd_sel).input_value()).strip()
                    if dd_val not in ("False", "No"):
                        missing.append("Defensive Driver Course (must be No)")
            except Exception:
                pass
        return missing

    if await _driver_field_visible("#MainContent_txtFirstName"):
        await _fill_primary_driver_from_contact()
    else:
        log("WARN", "Driver name field not visible; skipping CRM prefill for driver form")

    missing_after_fill: list[str] = []
    for attempt in range(3):
        await _fill_driver_required_once(second=False)
        await _force_years_experience_four()
        missing_after_fill = await _driver_missing_required_fields()
        if not missing_after_fill:
            break
        if attempt < 2:
            log("WARN", f"Driver required fields reset after fill (attempt {attempt + 1}/3): {', '.join(missing_after_fill)}; retrying")
            await page.wait_for_timeout(300)
    if missing_after_fill:
        raise RuntimeError(f"Driver field(s) still unselected after retries: {', '.join(missing_after_fill)}")

    await _force_years_experience_four()

    lic_to_use: str | None = None
    if await page.locator("#MainContent_txtDriversLicenseNumber").count() > 0:
        lic = (await page.locator("#MainContent_txtDriversLicenseNumber").input_value()).strip()
        lic_clean = re.sub(r"\D", "", lic) if lic else ""
        if not lic_clean or "*" in lic:
            raw_license = (
                contact.get("driverLicenseNumber")
                or contact.get("driversLicenseNumber")
                or contact.get("licenseNumber")
                or contact.get("driver_license_number")
                or ""
            )
            digits = re.sub(r"\D", "", str(raw_license))
            state = str(contact.get("state", "GA")).upper()
            if state == "GA":
                lic_to_use = (digits or "123456789")[:9].zfill(9)
            else:
                lic_to_use = digits or "123456789"
            await page.fill("#MainContent_txtDriversLicenseNumber", lic_to_use)

    if await page.locator("#MainContent_btnSaveDriver").count() > 0:
        if lic_to_use:
            await _verify_and_refill(page, {"#MainContent_txtDriversLicenseNumber": lic_to_use})
        await _force_active_license_status()
        await _force_losses_in_5_years_no()
        await _force_defensive_driver_course_no()
        await _force_years_experience_four()
        await page.click("#MainContent_btnSaveDriver")
        await page.wait_for_load_state("networkidle")
        if await page.locator("#lstErrors li").count() > 0:
            err = (await page.locator("#lstErrors").inner_text()).strip()
            if err:
                log("WARN", f"Driver save returned validation error(s): {err}")
                needs_retry = False
                if await page.locator("#MainContent_ddlDriversLicenseStatus").count() > 0:
                    st = (await page.locator("#MainContent_ddlDriversLicenseStatus").input_value()).strip()
                    if st in ("", "-1"):
                        needs_retry = True
                        log("WARN", "Driver license status reset by page; forcing Active")
                        await _force_active_license_status()
                if await page.locator("#MainContent_ddlLossesIn5Years").count() > 0:
                    ls = (await page.locator("#MainContent_ddlLossesIn5Years").input_value()).strip()
                    if ls in ("", "-1"):
                        needs_retry = True
                        log("WARN", "Losses-in-5-years reset by page; forcing No/False")
                        await _force_losses_in_5_years_no()
                if await page.locator("#MainContent_ddlDefensiveDriverCourse").count() > 0:
                    dd = (await page.locator("#MainContent_ddlDefensiveDriverCourse").input_value()).strip()
                    if dd.lower() not in ("false", "no", "n", "0", "none"):
                        needs_retry = True
                        log("WARN", "Defensive Driver Course not No after save; forcing No/False")
                        await _force_defensive_driver_course_no()
                if needs_retry:
                    log("WARN", "Re-saving primary driver after correcting required dropdown values")
                    await _force_years_experience_four()
                    await page.click("#MainContent_btnSaveDriver")
                    await page.wait_for_load_state("networkidle")
                    if await page.locator("#lstErrors li").count() > 0:
                        err = (await page.locator("#lstErrors").inner_text()).strip()
                        if err:
                            raise RuntimeError(f"Primary driver save still blocked after retry: {err}")
                    # Retry succeeded — do not raise
                else:
                    raise RuntimeError(f"Driver save blocked by validation: {err}")

    log("INFO", "Primary driver form saved; additional drivers on the quote are no longer auto-deleted.")

    async def _driver_grid_rows() -> list[dict]:
        return await page.evaluate(
            """() => {
                const table = document.getElementById('MainContent_gvDrivers');
                if (!table) return [];
                const rows = Array.from(table.querySelectorAll('tbody tr'));
                const out = [];
                const isAction = (t) => /view\\s*\\/\\s*edit|^delete$/i.test(t);
                const isDate = (t) => /^\\d{1,2}\\/\\d{1,2}\\/(\\d{2}|\\d{4})$/.test(t);
                const isNoise = (t) => /uninitializ/i.test(t);
                for (const tr of rows) {
                    const cells = Array.from(tr.querySelectorAll('td'))
                        .map((td) => (td.innerText || '').replace(/\\s+/g, ' ').trim())
                        .filter((t) => t && !isAction(t) && !isNoise(t));
                    if (cells.length < 1) continue;
                    const lowAll = cells.join(' ').toLowerCase();
                    if (lowAll === 'name' || (lowAll.includes('driver') && cells.length < 2)) continue;

                    let first = '';
                    let last = '';
                    for (const cell of cells) {
                        if (isDate(cell)) continue;
                        const parts = cell.split(/\\s+/).filter(Boolean);
                        if (parts.length >= 2 && !/\\d/.test(parts[0])) {
                            first = parts[0];
                            last = parts[parts.length - 1];
                            break;
                        }
                        if (!first && parts.length === 1 && !isDate(parts[0])) {
                            first = parts[0];
                        } else if (first && !last && parts.length === 1 && !isDate(parts[0])) {
                            last = parts[0];
                        }
                    }
                    if (first && last) {
                        out.push({ first, last, raw: `${first} ${last}` });
                    }
                }
                return out;
            }"""
        )

    async def _driver_name_from_page_header() -> tuple[str, str] | None:
        try:
            text = await page.evaluate(
                """() => {
                    const nodes = document.querySelectorAll('h1, h2, h3, legend, .page-title, span[id*="lbl"]');
                    for (const el of nodes) {
                        const t = (el.innerText || '').trim();
                        if (/driver information/i.test(t)) return t;
                    }
                    return document.title || '';
                }"""
            )
        except Exception:
            return None
        m = re.search(r"Driver Information\s*[-–]\s*(.+)", text or "", re.I)
        if not m:
            return None
        parsed = _parse_driver_name_parts(m.group(1).strip())
        if not parsed:
            return None
        return parsed[0], parsed[1]

    async def _clear_middle_name() -> None:
        try:
            mid_loc = page.locator("#MainContent_txtMiddleName").first
            if await mid_loc.count() > 0:
                await mid_loc.fill("")
        except Exception:
            pass

    async def _open_driver_row_view_edit(row_index: int) -> bool:
        ve = page.locator("#MainContent_gvDrivers tbody tr a:has-text('View/Edit')")
        if await ve.count() <= row_index:
            return False
        for _ in range(3):
            try:
                await ve.nth(row_index).click(timeout=10000)
                await page.wait_for_load_state("networkidle")
                await page.wait_for_selector("#MainContent_txtFirstName", state="visible", timeout=20000)
                return True
            except Exception:
                await page.wait_for_timeout(400)
        return False

    async def _fill_and_save_additional_driver(driver_index: int, grid_rows: list[dict]) -> None:
        """Fill driver row 1+ from CRM, grid name, or backup; save before returning to list."""
        grid_row = grid_rows[driver_index] if driver_index < len(grid_rows) else None
        crm_fn, crm_ln, dob_a = _additional_driver_identity(contact, driver_index, grid_row)

        await _clear_middle_name()

        existing_fn = ""
        existing_ln = ""
        try:
            existing_fn = (await page.locator("#MainContent_txtFirstName").input_value()).strip()
            existing_ln = (await page.locator("#MainContent_txtLastName").input_value()).strip()
        except Exception:
            pass

        header_names = await _driver_name_from_page_header()
        if header_names and _driver_name_looks_valid(header_names[0]) and _driver_name_looks_valid(header_names[1]):
            fn_a, ln_a = header_names
        elif _driver_name_looks_valid(existing_fn) and _driver_name_looks_valid(existing_ln):
            fn_a, ln_a = existing_fn, existing_ln
        elif _driver_name_looks_valid(crm_fn) and _driver_name_looks_valid(crm_ln):
            fn_a, ln_a = crm_fn, crm_ln
        else:
            fn_a, ln_a = crm_fn, crm_ln

        log(
            "INFO",
            f"Filling additional driver row {driver_index + 1}: {fn_a} {ln_a} | DOB={dob_a}",
        )
        await _fill_text_if_visible("#MainContent_txtFirstName", fn_a)
        await _fill_text_if_visible("#MainContent_txtLastName", ln_a)
        await _clear_middle_name()
        await _fill_text_if_visible("#MainContent_txtDateOfBirth", dob_a)
        # Fire focusout (NOT change — change triggers __doPostBack which reloads the page and resets values)
        # focusout runs DefensiveDriverCourseVisibilityToggle via jQuery, showing the field for 55+ drivers
        try:
            await page.evaluate(
                """() => {
                    const el = document.getElementById('MainContent_txtDateOfBirth');
                    if (el) el.dispatchEvent(new Event('focusout', {bubbles: true}));
                }"""
            )
            await page.wait_for_timeout(300)
        except Exception:
            pass

        await _select_if_present("#MainContent_ddlGender", "F", wait_postback=True, label_fallback="Female")
        await _select_if_present("#MainContent_ddlMaritalStatus", "Single", wait_postback=True)
        await _select_if_present(
            "#MainContent_ddlRelationshipStatus",
            "Relative",
            wait_postback=True,
            label_fallback="Relative",
        )
        occ2 = page.locator("#MainContent_ddlOccupation").first
        if await occ2.count() > 0:
            try:
                if await occ2.is_visible():
                    try:
                        await occ2.select_option(value="Other", timeout=10000)
                    except Exception:
                        await occ2.select_option(label="Other", timeout=5000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=20000)
                    except Exception:
                        await page.wait_for_timeout(600)
            except Exception:
                pass
        try:
            edu_loc = page.locator("#MainContent_ddlEducationLevel").first
            if await edu_loc.count() > 0 and await edu_loc.is_visible():
                await edu_loc.select_option(value="-1", timeout=8000)
        except Exception:
            log("WARN", f"Driver {driver_index + 1}: could not set Education Level to -- Select --")

        hh2 = page.locator("#MainContent_ddlDriverHouseholdMember").first
        if await hh2.count() > 0:
            try:
                if await hh2.is_visible():
                    try:
                        await hh2.select_option(value="True")
                    except Exception:
                        await hh2.select_option(label="Yes")
            except Exception:
                pass

        sec_missing: list[str] = []
        for attempt in range(3):
            await _fill_driver_required_once(second=True)
            await _force_years_experience_four()
            sec_missing = await _driver_missing_required_fields()
            if not sec_missing:
                break
            if attempt < 2:
                log(
                    "WARN",
                    f"Driver {driver_index + 1} required fields retry {attempt + 1}: {', '.join(sec_missing)}",
                )
                await page.wait_for_timeout(300)
        if sec_missing:
            raise RuntimeError(
                f"Driver {driver_index + 1} ({fn_a} {ln_a}) required field(s) still unset: {', '.join(sec_missing)}"
            )

        await _force_years_experience_four()

        lic_add: str | None = None
        if await page.locator("#MainContent_txtDriversLicenseNumber").count() > 0:
            lic_f = page.locator("#MainContent_txtDriversLicenseNumber").first
            lic_v = (await lic_f.input_value()).strip()
            lic_clean2 = re.sub(r"\D", "", lic_v) if lic_v else ""
            if not lic_clean2 or "*" in lic_v:
                raw2 = (
                    _contact_first(
                        contact,
                        "secondDriverLicenseNumber",
                        "spouseDriverLicenseNumber",
                        "spouse_license_number",
                        "driver2LicenseNumber",
                        "householdDriverLicenseNumber",
                    )
                    or contact.get("driverLicenseNumber")
                    or contact.get("driversLicenseNumber")
                    or contact.get("licenseNumber")
                    or contact.get("driver_license_number")
                    or ""
                )
                digits2 = re.sub(r"\D", "", str(raw2))
                st2 = str(contact.get("state", "GA")).upper()
                base = (digits2 or "123456789")[:9]
                try:
                    n = (int(base) + driver_index) % 1000000000
                    base = str(n).zfill(9)
                except ValueError:
                    base = "123456789"
                if st2 == "GA":
                    lic_add = base.zfill(9)
                else:
                    lic_add = base or "123456789"
                await page.fill("#MainContent_txtDriversLicenseNumber", lic_add)

        if await page.locator("#MainContent_btnSaveDriver").count() > 0:
            if lic_add:
                await _verify_and_refill(page, {"#MainContent_txtDriversLicenseNumber": lic_add})
            await _clear_middle_name()
            await _force_active_license_status()
            await _force_losses_in_5_years_no()
            # Always force defensive driver No (works for both visible and hidden elements)
            await _force_defensive_driver_course_no()
            await _force_years_experience_four()
            # Final verification before save: confirm license status and defensive driver are correct
            lic_status_val = (await page.locator("#MainContent_ddlDriversLicenseStatus").input_value()).strip()
            if lic_status_val != "Active":
                log("WARN", f"Driver {driver_index + 1}: license status is {lic_status_val!r} just before save; forcing Active again")
                await _force_active_license_status()
            dd_val = (await page.locator("#MainContent_ddlDefensiveDriverCourse").input_value()).strip() if await page.locator("#MainContent_ddlDefensiveDriverCourse").count() > 0 else "n/a"
            if dd_val.lower() not in ("false", "no", "n", "0", "none", "n/a"):
                log("WARN", f"Driver {driver_index + 1}: defensive driver is {dd_val!r} just before save; forcing No again")
                await _force_defensive_driver_course_no()
            await page.click("#MainContent_btnSaveDriver")
            await page.wait_for_load_state("networkidle")
            if await page.locator("#lstErrors li").count() > 0:
                err2 = (await page.locator("#lstErrors").inner_text()).strip()
                if err2:
                    # After a failed save the page reloads — for 55+ drivers the defensive driver
                    # field is now visible ($(document).ready ran DefensiveDriverCourseVisibilityToggle).
                    # Re-force it and retry the save once before giving up.
                    if "defensive driver" in err2.lower():
                        log("WARN", f"Driver {driver_index + 1}: defensive driver required after save — field now visible; re-forcing and retrying")
                        await _force_defensive_driver_course_no()
                        await _force_active_license_status()
                        await _force_losses_in_5_years_no()
                        await _force_years_experience_four()
                        await page.click("#MainContent_btnSaveDriver")
                        await page.wait_for_load_state("networkidle")
                        if await page.locator("#lstErrors li").count() > 0:
                            err2 = (await page.locator("#lstErrors").inner_text()).strip()
                            if err2:
                                raise RuntimeError(
                                    f"Driver {driver_index + 1} ({fn_a} {ln_a}) save blocked after defensive-driver retry: {err2}"
                                )
                    else:
                        raise RuntimeError(
                            f"Driver {driver_index + 1} ({fn_a} {ln_a}) save blocked by validation: {err2}"
                        )
        log("INFO", f"Additional driver row {driver_index + 1} saved ({fn_a} {ln_a})")

    async def _process_all_additional_drivers() -> None:
        """Every driver row after the first: View/Edit, fill (CRM / grid / backup), Save."""
        ve = page.locator("#MainContent_gvDrivers tbody tr a:has-text('View/Edit')")
        n = await ve.count()
        if n < 2:
            return
        grid_rows = await _driver_grid_rows()
        log("INFO", f"Driver grid has {n} row(s); filling {n - 1} additional driver(s)")
        for driver_index in range(1, n):
            if not await _open_driver_row_view_edit(driver_index):
                log("WARN", f"Could not open View/Edit for driver row {driver_index + 1}")
                continue
            await _fill_and_save_additional_driver(driver_index, grid_rows)

    await _process_all_additional_drivers()

    navigated = False
    for attempt in range(4):
        log("INFO", f"Driver Agreement & Continue attempt {attempt + 1}")
        
        incomplete_count = 0
        try:
            incomplete_count = await page.locator("#MainContent_gvDrivers tbody tr", has_text=re.compile(r"uninitializ|incomplete", re.I)).count()
        except Exception:
            pass

        if incomplete_count > 0:
            log("INFO", "Incomplete driver on grid — re-opening and filling all additional drivers before checking terms.")
            try:
                await _process_all_additional_drivers()
            except Exception as fill_exc:
                log("WARN", f"Additional driver refill failed: {fill_exc}")
            continue
        
        try:
            tc_triggers = page.locator(
                "#MainContent_pnlConnectedDriverAgreement legend, "
                "#MainContent_dvConnectedDriverAgreement, "
                "a:has-text('Terms and Conditions'), "
                "a:has-text('Terms & Conditions')"
            )
            if await tc_triggers.count() > 0:
                try:
                    await tc_triggers.first.click(timeout=5000)
                    await page.wait_for_timeout(500)
                except Exception:
                    pass

            agreement = page.locator("#MainContent_chkConnectedDriverAgreement")
            if await agreement.count() > 0:
                if not await agreement.is_checked():
                    try:
                        await agreement.check(timeout=5000, force=True)
                    except Exception:
                        await page.evaluate("document.getElementById('MainContent_chkConnectedDriverAgreement').click()")
                    await page.wait_for_timeout(500)
                    await page.wait_for_load_state("networkidle")
                    
                if not await agreement.is_checked():
                    label = page.locator("label[for='MainContent_chkConnectedDriverAgreement']")
                    if await label.count() > 0:
                        await label.click(force=True)
                        await page.wait_for_timeout(500)
                        await page.wait_for_load_state("networkidle")

                if await agreement.is_checked():
                    log("OK", "Connected Driver Agreement checked")
                else:
                    log("WARN", "Connected Driver Agreement checkbox did not remain checked")
        except Exception as exc:
            log("WARN", f"Connected Driver Agreement skipped: {exc}")

        click_success = False
        next_selectors = ("#MainContent_btnContinue", "input[name$='btnContinue']", "input[value='Next']", "button:has-text('Next')", "button:has-text('Continue')")
        for sel in next_selectors:
            loc = page.locator(sel).first
            try:
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(timeout=10000)
                    click_success = True
                    break
            except Exception:
                continue
                
        if not click_success:
            try:
                await page.evaluate("document.getElementById('MainContent_btnContinue')?.click()")
                click_success = True
            except Exception:
                pass
                
        if click_success:
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
                
            if "DriverInfo" not in page.url:
                log("INFO", "Successfully navigated away from DriverInfo page")
                navigated = True
                break
            else:
                log("WARN", "Still on DriverInfo page after clicking Continue.")
                
                # Check for driver save requirement as a fallback
                if await page.locator("#MainContent_btnSaveDriver").count() > 0:
                    try:
                        await _force_active_license_status()
                        await _force_losses_in_5_years_no()
                        await _force_defensive_driver_course_no()
                        await _force_years_experience_four()
                        await page.click("#MainContent_btnSaveDriver", timeout=10000)
                        await page.wait_for_load_state("networkidle")
                    except Exception:
                        pass
                
                if await page.locator("#lstErrors li").count() > 0:
                    err = (await page.locator("#lstErrors").inner_text()).strip()
                    if err:
                        log("WARN", f"DriverInfo validation errors found: {err}")
                        if "All driver information must be completed" in err:
                            log(
                                "INFO",
                                "Incomplete driver on grid — re-opening and filling all additional drivers.",
                            )
                            try:
                                await _process_all_additional_drivers()
                            except Exception as fill_exc:
                                log("WARN", f"Additional driver refill failed: {fill_exc}")
                            
        await page.wait_for_timeout(1000)

    if not navigated:
        if "DriverInfo" in page.url and await page.locator("#lstErrors li").count() > 0:
            err = (await page.locator("#lstErrors").inner_text()).strip()
            raise RuntimeError(f"Driver validation blocked continue: {err}")
        else:
            raise RuntimeError("Could not click Continue on Drivers")

# - PAGE 12: DRIVER VIOLATIONS (click continue) -----------â”€

async def page_12_driver_violations(page: Page) -> None:
    debug_step(12, "click Continue through driver violations", page=page)
    # Inline click next logic with fallbacks
    clicked = False
    for attempt in range(3):
        for sel in ["#MainContent_btnContinue", "input[name$='btnContinue']", "button:has-text('Continue')", "button:has-text('Next')"]:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=10000)
                    clicked = True
                    break
            except Exception:
                pass
        if clicked:
            break
        try:
            await page.evaluate("document.getElementById('MainContent_btnContinue')?.click()")
            clicked = True
            break
        except Exception:
            pass
        await page.wait_for_timeout(1000)
        
    if not clicked:
        raise RuntimeError("Could not click Continue on Driver Violations")

    await page.wait_for_load_state("networkidle")
# - PAGE 13: VEHICLES ------------------------â”€

_VEHICLE_UMPD_TARGET_LABEL = "Included - 25,000 (250 Ded)"
_GEMINI_VEHICLE_MODEL = "gemini-2.5-flash"

_VEHICLE_FIELD_BACKUPS: dict[str, Any] = {
    "purchase_date": "03/01/2023",
    "auto_rented_to_others": "False",
    "vehicle_weight_14k_16k": "False",
    "agreed_value": "False",
    "camper_included": "False",
    "suspension_indicator": "False",
    "base_list_price": "59450",
    "ownership_status": "3",
    "annual_mileage": "10000",
}


def _ghl_vehicle_description(v: dict) -> str:
    parts = [
        str(v.get("year") or "").strip(),
        str(v.get("make") or "").strip(),
        str(v.get("model") or "").strip(),
        str(v.get("submodel") or "").strip(),
    ]
    return " ".join(p for p in parts if p)


def _strip_crm_vehicle_noise(text: str) -> str:
    s = re.sub(r"\s+", " ", (text or "")).strip()
    s = re.sub(r"(?i)\bdistance\s+driven\b", "", s).strip()
    s = re.sub(r"(?i)\bowned\b", "", s).strip()
    s = re.sub(r"(?i)\b[A-Z]+-(?:PICKUP|SPORT\s+UTILITY\s+VEHICLE)\b", "", s).strip()
    return re.sub(r"\s+", " ", s).strip()


def _ghl_vehicle_core_text(v: dict) -> str:
    """Normalized year/make/model line for matching (trim + CRM noise removed)."""
    year = str(v.get("year") or "").strip()
    make = _strip_crm_vehicle_noise(str(v.get("make") or ""))
    model = _strip_crm_vehicle_noise(str(v.get("model") or ""))
    model = re.split(r"\s{2,}", model)[0].strip()
    return " ".join(p for p in (year, make, model) if p)


def _ghl_vehicle_match_text(v: dict) -> str:
    return _ghl_vehicle_core_text(v)


_VEHICLE_TRIM_TOKENS: frozenset[str] = frozenset(
    {
        "lt",
        "ls",
        "ltz",
        "premier",
        "platinum",
        "custom",
        "sport",
        "utility",
        "vehicle",
        "pickup",
        "owned",
        "distance",
        "driven",
        "chev",
        "chevy",
        "chevrolet",
    }
)


_VEHICLE_TOKEN_ALIASES: dict[str, str] = {
    "chev": "chevrolet",
    "chevy": "chevrolet",
}


def _vehicle_match_tokens(text: str) -> set[str]:
    raw = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
    expanded: set[str] = set()
    for tok in raw:
        if tok in {"add", "coverages", "view", "edit", "delete", "distance", "driven", "owned"}:
            continue
        expanded.add(tok)
        alias = _VEHICLE_TOKEN_ALIASES.get(tok)
        if alias:
            expanded.add(alias)
    return expanded


def _extract_year_from_text(text: str) -> str:
    m = re.search(r"\b(19|20)\d{2}\b", text or "")
    return m.group(0) if m else ""


_VIN_MODEL_YEAR_CODES: dict[str, int] = {
    **{str(d): 2000 + d for d in range(1, 10)},
    **{c: 2010 + i for i, c in enumerate("ABCDEFGHJKLMNPRSTVWXY")},
}


def _vin_model_year(vin: str) -> str:
    v = _normalize_vin(vin)
    if len(v) < 10:
        return ""
    code = v[9].upper()
    year = _VIN_MODEL_YEAR_CODES.get(code)
    if not year:
        return ""
    return str(year)


def _portal_vehicle_year(row: dict) -> str:
    name = str(row.get("name") or "")
    y = _extract_year_from_text(name)
    if y:
        return y
    return _vin_model_year(str(row.get("vin") or ""))


def _ghl_vehicle_year(v: dict) -> str:
    raw = str(v.get("year") or "").strip()
    if re.fullmatch(r"\d{4}", raw):
        return raw
    return _extract_year_from_text(_ghl_vehicle_description(v))


def _vehicle_years_compatible(portal_row: dict, ghl_vehicle: dict) -> bool:
    portal_year = _portal_vehicle_year(portal_row)
    ghl_year = _ghl_vehicle_year(ghl_vehicle)
    if not portal_year or not ghl_year:
        return True
    if portal_year == ghl_year:
        return True
    p_name = str(portal_row.get("name") or "")
    ta = _vehicle_line_tokens(p_name)
    tb = _vehicle_line_tokens(_ghl_vehicle_core_text(ghl_vehicle))
    if not _vehicle_bodies_compatible(ta, tb):
        return False
    if _vehicle_match_score(portal_row, ghl_vehicle) >= 0.40:
        log(
            "WARN",
            f"CRM year {ghl_year} != portal year {portal_year} but same model line "
            f"({p_name} ~ {_ghl_vehicle_core_text(ghl_vehicle)})",
        )
        return True
    return False


def _vehicle_line_tokens(text: str) -> set[str]:
    return _vehicle_match_tokens(text) - _VEHICLE_TRIM_TOKENS


def _vehicle_body_tokens(tokens: set[str]) -> set[str]:
    bodies: set[str] = set()
    for t in tokens:
        if re.fullmatch(r"k?\d{4}", t):
            bodies.add(t.lstrip("k"))
        elif t in {"1500", "2500", "3500"}:
            bodies.add(t)
    return bodies


def _vehicle_bodies_compatible(ta: set[str], tb: set[str]) -> bool:
    ba, bb = _vehicle_body_tokens(ta), _vehicle_body_tokens(tb)
    if not ba or not bb:
        return True
    return bool(ba & bb)


def _vehicle_line_similarity(a: str, b: str) -> float:
    """Match model line ignoring trim (LT vs PREMIER) and CRM junk."""
    ta = _vehicle_line_tokens(a)
    tb = _vehicle_line_tokens(b)
    if not ta or not tb:
        return 0.0
    years_a = {t for t in ta if len(t) == 4 and t.isdigit() and t.startswith(("19", "20"))}
    years_b = {t for t in tb if len(t) == 4 and t.isdigit() and t.startswith(("19", "20"))}
    if years_a and years_b and not (years_a & years_b):
        return 0.0
    if not _vehicle_bodies_compatible(ta, tb):
        return 0.0
    families = {"tahoe", "silverado", "suburban", "equinox", "traverse", "malibu"}
    fa, fb = ta & families, tb & families
    if fa and fb and not (fa & fb):
        return 0.0
    inter = ta & tb
    if not inter:
        return 0.0
    union = ta | tb
    score = len(inter) / max(len(union), 1)
    if fa and fb and (fa & fb):
        score = max(score, 0.35)
    body = {"k1500", "k2500", "k3500", "1500", "2500", "3500"}
    if (ta & body) and (tb & body) and (ta & body) & (tb & body):
        score = max(score, score + 0.15)
    return min(score, 1.0)


def _vehicle_match_score(portal_row: dict, ghl_vehicle: dict) -> float:
    p_name = str(portal_row.get("name") or "")
    ghl_core = _ghl_vehicle_core_text(ghl_vehicle)
    ghl_desc = _strip_crm_vehicle_noise(_ghl_vehicle_description(ghl_vehicle))
    return max(
        _vehicle_line_similarity(p_name, ghl_core),
        _vehicle_line_similarity(p_name, ghl_desc),
        _vehicle_name_similarity(p_name, ghl_core),
        _vehicle_name_similarity(p_name, ghl_desc),
    )


def _vehicle_strong_line_match(portal_row: dict, ghl_vehicle: dict) -> bool:
    return _vehicle_match_score(portal_row, ghl_vehicle) >= 0.30


def _vin_prefix_usable(vin_prefix: str) -> bool:
    p = _normalize_vin(vin_prefix)
    if len(p) < 8:
        return False
    if p.endswith("000000") or p.endswith("00000"):
        return False
    return True


def _vehicle_name_similarity(a: str, b: str) -> float:
    ta = _vehicle_match_tokens(a)
    tb = _vehicle_match_tokens(b)
    if not ta or not tb:
        return 0.0
    years_a = {t for t in ta if len(t) == 4 and t.isdigit() and t.startswith(("19", "20"))}
    years_b = {t for t in tb if len(t) == 4 and t.isdigit() and t.startswith(("19", "20"))}
    if years_a and years_b and not (years_a & years_b):
        return 0.0
    inter = ta & tb
    if not inter:
        return 0.0
    union = ta | tb
    return len(inter) / max(len(union), 1)


def _normalize_vin(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", (value or "").upper())


def _vin_matches(portal_vin: str, ghl_vin_prefix: str) -> bool:
    pv = _normalize_vin(portal_vin)
    gp = _normalize_vin(ghl_vin_prefix)
    if not pv or not gp or len(gp) < 5:
        return False
    if pv == gp:
        return True
    for n in (17, 11, 8, 5):
        if len(gp) >= n and pv.startswith(gp[:n]):
            return True
        if len(pv) >= n and gp.startswith(pv[:n]):
            return True
    return False


def _clean_portal_vehicle_name(raw: str) -> str:
    name = re.sub(r"\s+", " ", (raw or "")).strip()
    name = re.sub(r"(?i)\badd\s+coverages\b", "", name).strip()
    name = re.sub(r"(?i)\bview\s*/\s*edit\b", "", name).strip()
    name = re.sub(r"(?i)\bdelete\b", "", name).strip()
    name = re.sub(r"^\d+\s+", "", name).strip()
    name = re.sub(r"\s+-\s*1\s*$", "", name).strip()
    name = re.sub(r"\s+-\s*1\b", " ", name).strip()
    return re.sub(r"\s+", " ", name).strip()


def _looks_like_valid_portal_vehicle(row: dict) -> bool:
    vin = _normalize_vin(str(row.get("vin") or ""))
    name = _clean_portal_vehicle_name(str(row.get("name") or ""))
    if len(vin) >= 11 and re.fullmatch(r"[A-HJ-NPR-Z0-9]+", vin):
        return True
    if re.search(r"\b(19|20)\d{2}\b", name) and len(name) >= 8:
        return True
    return False


def _normalize_portal_vehicle_rows(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        idx = int(row.get("idx", len(out)))
        try:
            unit = int(row.get("unit", idx + 1))
        except (TypeError, ValueError):
            unit = idx + 1
        out.append(
            {
                "idx": idx,
                "unit": unit,
                "name": _clean_portal_vehicle_name(str(row.get("name") or "")),
                "vin": _normalize_vin(str(row.get("vin") or "")),
            }
        )
    return out


def _ghl_index_for_portal_vin(portal_row: dict, ghl_vehicles: list[dict]) -> int:
    portal_vin = str(portal_row.get("vin") or "")
    for gi, g in enumerate(ghl_vehicles):
        if not _vehicle_years_compatible(portal_row, g):
            continue
        prefix = str(g.get("vin_prefix") or "")
        if not _vin_prefix_usable(prefix):
            continue
        if _vin_matches(portal_vin, prefix):
            return gi
    return -1


def _portal_row_vin_protected(portal_row: dict, ghl_vehicles: list[dict]) -> bool:
    return _ghl_index_for_portal_vin(portal_row, ghl_vehicles) >= 0


def _finalize_vehicle_match_plan(
    plan: dict, portal_vehicles: list[dict], ghl_vehicles: list[dict]
) -> dict:
    """Merge VIN matches and drop deletes for rows that match CRM."""
    matches: list[dict] = []
    seen_portal: set[int] = set()
    seen_ghl: set[int] = set()
    for m in plan.get("matches", []):
        if not isinstance(m, dict):
            continue
        try:
            p_idx = int(m["portal_idx"])
            g_idx = int(m["ghl_idx"])
        except (TypeError, ValueError, KeyError):
            continue
        if p_idx in seen_portal or g_idx in seen_ghl:
            continue
        if g_idx < 0 or g_idx >= len(ghl_vehicles):
            continue
        portal_row = next((p for p in portal_vehicles if int(p["idx"]) == p_idx), None)
        if portal_row and not _vehicle_years_compatible(portal_row, ghl_vehicles[g_idx]):
            log(
                "WARN",
                f"Rejecting match portal {p_idx} -> ghl {g_idx}: year "
                f"{_portal_vehicle_year(portal_row)} vs {_ghl_vehicle_year(ghl_vehicles[g_idx])}",
            )
            continue
        matches.append({"portal_idx": p_idx, "ghl_idx": g_idx})
        seen_portal.add(p_idx)
        seen_ghl.add(g_idx)

    for p in portal_vehicles:
        p_idx = int(p["idx"])
        if p_idx in seen_portal:
            continue
        for gi, g in enumerate(ghl_vehicles):
            if gi in seen_ghl:
                continue
            if not _vehicle_years_compatible(p, g):
                continue
            prefix = str(g.get("vin_prefix") or "")
            if not _vin_prefix_usable(prefix):
                continue
            if _vin_matches(str(p.get("vin") or ""), prefix):
                matches.append({"portal_idx": p_idx, "ghl_idx": gi})
                seen_portal.add(p_idx)
                seen_ghl.add(gi)
                break

    delete: list[int] = []
    for p in portal_vehicles:
        p_idx = int(p["idx"])
        if p_idx in seen_portal:
            continue
        if _portal_row_vin_protected(p, ghl_vehicles):
            log("WARN", f"Portal row {p_idx} VIN matches CRM; keeping (not deleting)")
            continue
        delete.append(p_idx)
    return {"matches": matches, "delete": delete}


def _fallback_match_vehicles(portal_vehicles: list[dict], ghl_vehicles: list[dict]) -> dict:
    """Local VIN + name matcher when Gemini is unavailable."""
    portal_vehicles = _normalize_portal_vehicle_rows(portal_vehicles)
    if not ghl_vehicles:
        return {
            "matches": [],
            "delete": [int(p["idx"]) for p in portal_vehicles],
        }

    candidates: list[tuple[float, int, int]] = []
    for p in portal_vehicles:
        p_idx = int(p["idx"])
        p_name = str(p.get("name") or "")
        p_vin = str(p.get("vin") or "")
        for gi, g in enumerate(ghl_vehicles):
            if not _vehicle_years_compatible(p, g):
                continue
            prefix = str(g.get("vin_prefix") or "")
            if _vin_prefix_usable(prefix) and _vin_matches(p_vin, prefix):
                score = 1.0
            else:
                score = _vehicle_match_score(p, g)
            if score > 0:
                candidates.append((score, p_idx, gi))

    candidates.sort(key=lambda x: x[0], reverse=True)
    matches: list[dict] = []
    used_portal: set[int] = set()
    used_ghl: set[int] = set()
    min_name_score = 0.12
    for score, p_idx, gi in candidates:
        if p_idx in used_portal or gi in used_ghl:
            continue
        if score < 1.0 and score < min_name_score:
            continue
        matches.append({"portal_idx": p_idx, "ghl_idx": gi})
        used_portal.add(p_idx)
        used_ghl.add(gi)

    delete = [int(p["idx"]) for p in portal_vehicles if int(p["idx"]) not in used_portal]
    return _finalize_vehicle_match_plan({"matches": matches, "delete": delete}, portal_vehicles, ghl_vehicles)


def _parse_gemini_vehicle_json(text: str) -> dict:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty Gemini response")
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw, re.I)
    if fence:
        raw = fence.group(1).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start : end + 1]
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Gemini JSON root must be an object")
    matches = data.get("matches", [])
    delete = data.get("delete", [])
    if not isinstance(matches, list):
        matches = []
    if not isinstance(delete, list):
        delete = []
    norm_matches: list[dict] = []
    for m in matches:
        if not isinstance(m, dict):
            continue
        try:
            norm_matches.append(
                {"portal_idx": int(m["portal_idx"]), "ghl_idx": int(m["ghl_idx"])}
            )
        except (TypeError, ValueError, KeyError):
            continue
    norm_delete: list[int] = []
    for d in delete:
        try:
            norm_delete.append(int(d))
        except (TypeError, ValueError):
            continue
    return {"matches": norm_matches, "delete": norm_delete}


def _gemini_match_vehicles_sync(portal_vehicles: list[dict], ghl_vehicles: list[dict]) -> dict:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        log("WARN", "GEMINI_API_KEY not set; using local vehicle name matcher")
        return _fallback_match_vehicles(portal_vehicles, ghl_vehicles)

    try:
        import google.generativeai as genai
    except ImportError as exc:
        log("WARN", f"google-generativeai not installed ({exc}); using local matcher")
        return _fallback_match_vehicles(portal_vehicles, ghl_vehicles)

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(_GEMINI_VEHICLE_MODEL)

    portal_payload = [
        {
            "idx": int(p["idx"]),
            "year": _portal_vehicle_year(p) or None,
            "model_line": str(p.get("name") or ""),
            "name": str(p.get("name") or ""),
            "vin": str(p.get("vin") or ""),
        }
        for p in portal_vehicles
    ]
    ghl_payload = [
        {
            "idx": gi,
            "year": _ghl_vehicle_year(g) or None,
            "model_line": _ghl_vehicle_core_text(g),
            "description": _ghl_vehicle_description(g),
            "vin_prefix": str(g.get("vin_prefix") or ""),
        }
        for gi, g in enumerate(ghl_vehicles)
    ]

    prompt = (
        "You match insurance quote vehicles on a portal to vehicles from a CRM (GHL).\n"
        "Each GHL vehicle maps to at most one portal row. Unmatched portal rows go in delete.\n\n"
        "CRITICAL — MODEL YEAR MUST MATCH:\n"
        "- Portal year and GHL year must be the same (use year field, name, or VIN).\n"
        "- Different year = different vehicle (do not match).\n\n"
        "TRIM / LABEL EQUIVALENCE (same vehicle):\n"
        "- Ignore CRM noise: 'Distance Driven', 'Owned', 'SILVERADO-PICKUP', etc.\n"
        "- Trim differences on the SAME model line ARE the same vehicle:\n"
        "  e.g. portal 'TAHOE K1500 LT' = GHL 'TAHOE K1500 PREMIER'\n"
        "  e.g. portal 'SILVERADO K3500 HIGH COUNTRY' = GHL 'SILVERADO K3500 HIGH COUNTRY Distance Driven'\n"
        "- Match on year + make + model line (TAHOE K1500, SILVERADO K3500), not exact trim.\n\n"
        f"Portal vehicles: {json.dumps(portal_payload)}\n"
        f"GHL vehicles: {json.dumps(ghl_payload)}\n\n"
        "Return ONLY valid JSON (no markdown prose) with this shape:\n"
        '{"matches":[{"portal_idx":0,"ghl_idx":0}],"delete":[1]}\n'
        "- matches: portal row index -> ghl vehicle index\n"
        "- delete: portal row indices to remove (extras or no CRM equivalent)\n"
    )

    response = model.generate_content(prompt)
    text = getattr(response, "text", None) or ""
    result = _parse_gemini_vehicle_json(text)
    log("INFO", f"Gemini vehicle match: {len(result['matches'])} match(es), {len(result['delete'])} delete(s)")
    return result


async def _gemini_match_vehicles(portal_vehicles: list[dict], ghl_vehicles: list[dict]) -> dict:
    portal_norm = _normalize_portal_vehicle_rows(portal_vehicles)
    plan = await asyncio.to_thread(_gemini_match_vehicles_sync, portal_norm, ghl_vehicles)
    return _finalize_vehicle_match_plan(plan, portal_norm, ghl_vehicles)


async def page_13_vehicles(page: Page, contact: dict) -> None:
    debug_step(13, "UMPD + Gemini match + fill vehicles", page=page)
    log("INFO", "Starting page_13_vehicles")

    vehicles = contact.get("vehicles", [])
    if not isinstance(vehicles, list):
        vehicles = []

    async def _wait_for_vehicle_list_stable() -> None:
        try:
            await page.wait_for_selector(
                "a[id^='MainContent_rpVehicles_btnViewEdit_']",
                state="visible",
                timeout=30000,
            )
        except Exception:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            await page.wait_for_timeout(800)

    async def _scrape_portal_vehicle_rows() -> list[dict]:
        for attempt in range(4):
            try:
                await _wait_for_vehicle_list_stable()
                raw = await page.evaluate(
            """() => {
                const rows = [];
                const links = document.querySelectorAll("a[id^='MainContent_rpVehicles_btnViewEdit_']");
                links.forEach((a) => {
                    const m = (a.id || '').match(/_(\\d+)$/);
                    const idx = m ? parseInt(m[1], 10) : rows.length;
                    let name = '';
                    let vin = '';
                    const tr = a.closest('tr');
                    if (tr) {
                        const vinEl = tr.querySelector("[id^='MainContent_rpVehicles_tdVIN']");
                        if (vinEl) vin = (vinEl.innerText || '').trim();
                        const cells = Array.from(tr.querySelectorAll('td'));
                        const parts = [];
                        for (const td of cells) {
                            const id = (td.id || '').toLowerCase();
                            const txt = (td.innerText || '').replace(/\\s+/g, ' ').trim();
                            if (!txt) continue;
                            if (id.includes('vin')) continue;
                            if (/view\\s*\\/\\s*edit|delete|add coverages/i.test(txt)) continue;
                            if (/^\\d+$/.test(txt)) continue;
                            if (/^-\\s*1$/i.test(txt)) continue;
                            if (/\\b(19|20)\\d{2}\\b/.test(txt) || /[A-Z]{2,}/.test(txt)) {
                                parts.push(txt);
                            }
                        }
                        name = parts.join(' ').trim();
                        if (!name) {
                            name = (tr.innerText || '')
                                .replace(/View\\s*\\/\\s*Edit/gi, '')
                                .replace(/Delete/gi, '')
                                .replace(/Add\\s*Coverages/gi, '')
                                .replace(vin, '')
                                .replace(/\\s+/g, ' ')
                                .trim();
                        }
                    }
                    let unit = idx + 1;
                    if (tr) {
                        const unitTd = tr.querySelector('td');
                        const unitTxt = (unitTd?.innerText || '').trim();
                        const unitNum = parseInt(unitTxt, 10);
                        if (!isNaN(unitNum)) unit = unitNum;
                    }
                    rows.push({ idx, unit, name, vin });
                });
                return rows;
            }"""
                )
                return _normalize_portal_vehicle_rows(raw)
            except Exception as exc:
                msg = str(exc).lower()
                if "execution context was destroyed" in msg or "navigation" in msg:
                    log("WARN", f"Vehicle list scrape retry {attempt + 1}: {exc}")
                    await page.wait_for_timeout(1200)
                    continue
                raise
        return []

    async def _set_umpd_reduced_by() -> None:
        target = _VEHICLE_UMPD_TARGET_LABEL
        umpd_selectors = [
            "#MainContent_ucAutoPolicy_rptCoreCoverages_ddlCovUMUIMPD_7",
            "select[id*='ddlCovUMUIMPD']",
        ]
        for sel in umpd_selectors:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            try:
                await loc.select_option(value="25000/250")
                log("INFO", f"Set UMPD Reduced By -> {target} ({sel})")
                return
            except Exception:
                try:
                    await loc.select_option(label=target)
                    log("INFO", f"Set UMPD Reduced By -> {target} ({sel})")
                    return
                except Exception:
                    pass
        found = await page.evaluate(
            """(targetLabel) => {
                const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const want = norm(targetLabel);
                const labels = Array.from(document.querySelectorAll('label'));
                for (const label of labels) {
                    const t = norm(label.textContent);
                    if (!t.includes('uninsured') || !t.includes('motorist') || !t.includes('property damage')) {
                        continue;
                    }
                    if (!t.includes('reduced')) continue;
                    const forId = label.getAttribute('for');
                    if (forId) {
                        const sel = document.getElementById(forId);
                        if (sel && sel.tagName === 'SELECT') return sel.id;
                    }
                    const sel = label.closest('li, tr, div')?.querySelector('select');
                    if (sel) return sel.id;
                }
                const selects = Array.from(document.querySelectorAll('select'));
                for (const sel of selects) {
                    for (const opt of Array.from(sel.options)) {
                        if (norm(opt.text) === want || norm(opt.label) === want) {
                            return sel.id;
                        }
                    }
                }
                return null;
            }""",
            target,
        )
        if not found:
            log("WARN", "UMPD Reduced By dropdown not found on Vehicle Info list page")
            return
        sel = f"#{found}"
        try:
            await page.select_option(sel, label=target)
        except Exception:
            try:
                await page.select_option(sel, value=target)
            except Exception as exc:
                log("WARN", f"Could not set UMPD Reduced By to '{target}': {exc}")
                return
        log("INFO", f"Set UMPD Reduced By -> {target}")

    async def _delete_vehicle_row(row_index: int) -> bool:
        del_selectors = [
            f"#MainContent_rpVehicles_btnDelete_{row_index}",
            f"tr:has(#MainContent_rpVehicles_tdVIN_{row_index}) a:has-text('Delete')",
        ]
        for sel in del_selectors:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                try:
                    async with page.expect_navigation(timeout=30000, wait_until="networkidle"):
                        await loc.click(timeout=10000)
                    await _wait_for_vehicle_list_stable()
                    return True
                except PlaywrightTimeoutError:
                    try:
                        await loc.click(timeout=10000)
                        await _wait_for_vehicle_list_stable()
                        return True
                    except Exception:
                        pass
                except Exception:
                    pass
        try:
            async with page.expect_navigation(timeout=30000, wait_until="networkidle"):
                await page.evaluate(
                    f"document.getElementById('MainContent_rpVehicles_btnDelete_{row_index}')?.click()"
                )
            await _wait_for_vehicle_list_stable()
            return True
        except Exception:
            return False

    async def _ensure_select(selector: str, value: str, *, label: str | None = None) -> None:
        loc = page.locator(selector).first
        if await loc.count() == 0:
            return
        try:
            if not await loc.is_visible():
                return
        except Exception:
            return
        try:
            current = (await loc.input_value()).strip()
            if current == value:
                return
        except Exception:
            pass
        try:
            await loc.select_option(value=value)
        except Exception:
            if label:
                await loc.select_option(label=label)

    async def _fill_text_if_empty(selector: str, value: str) -> None:
        loc = page.locator(selector).first
        if await loc.count() == 0:
            return
        try:
            if not await loc.is_visible():
                return
        except Exception:
            return
        current = (await loc.input_value()).strip()
        if current:
            return
        await loc.fill(value)

    async def _fill_vehicle_edit_form(ghl_vehicle: dict) -> dict[str, str]:
        """Fill required vehicle fields (force unset/invalid dropdowns)."""
        applied: dict[str, str] = {}

        purchase_raw = (
            ghl_vehicle.get("purchase_date")
            or ghl_vehicle.get("purchaseDate")
            or _VEHICLE_FIELD_BACKUPS["purchase_date"]
        )
        purchase_val = _date(str(purchase_raw)) if purchase_raw else _VEHICLE_FIELD_BACKUPS["purchase_date"]
        await _fill_text_if_empty("#MainContent_ucVehicleInfo_txtPurchaseDate", purchase_val)
        applied["Purchased Date"] = purchase_val

        await _ensure_select(
            "#MainContent_ucVehicleInfo_ddlAutoRentedToOthers",
            str(_VEHICLE_FIELD_BACKUPS["auto_rented_to_others"]),
            label="No",
        )
        applied["Is vehicle ever rented/leased to others for a fee?"] = "No"

        await _ensure_select(
            "#MainContent_ucVehicleInfo_ddlVehicleWeightBetween14kAnd16k",
            str(_VEHICLE_FIELD_BACKUPS["vehicle_weight_14k_16k"]),
            label="No",
        )
        applied["Is the vehicle weight between 14k -16k and used to service a farm/residence premises?"] = "No"

        await _ensure_select(
            "#MainContent_ucVehicleInfo_ddlAgreedValue",
            str(_VEHICLE_FIELD_BACKUPS["agreed_value"]),
            label="No",
        )
        applied["Agreed Value"] = "No"

        await _ensure_select(
            "#MainContent_ucVehicleInfo_ddlCamperIncluded",
            str(_VEHICLE_FIELD_BACKUPS["camper_included"]),
            label="No",
        )
        applied["Is a camper unit included with this vehicle?"] = "No"

        await _ensure_select(
            "#MainContent_ucVehicleInfo_ddlSuspensionIndicator",
            str(_VEHICLE_FIELD_BACKUPS["suspension_indicator"]),
            label="No",
        )
        applied["Suspension Indicator"] = "No"

        base_price = str(_VEHICLE_FIELD_BACKUPS["base_list_price"])
        await _fill_text_if_empty("#MainContent_ucVehicleInfo_txtBasePrice", base_price)
        applied["Base List Price"] = base_price

        own_raw = ghl_vehicle.get("ownership_status")
        if own_raw is None or str(own_raw).strip() in ("", "-1"):
            own_val = str(_VEHICLE_FIELD_BACKUPS["ownership_status"])
        else:
            own_val = str(int(own_raw)) if str(own_raw).isdigit() else str(_VEHICLE_FIELD_BACKUPS["ownership_status"])
        await _ensure_select("#MainContent_ucVehicleInfo_ddlOwnershipStatus", own_val, label="Own")
        applied["Ownership Status"] = "Own" if own_val == "3" else own_val

        mileage_raw = ghl_vehicle.get("annual_mileage") or ghl_vehicle.get("annualMileage")
        mileage_val = str(mileage_raw) if mileage_raw not in (None, "") else str(_VEHICLE_FIELD_BACKUPS["annual_mileage"])
        await _fill_text_if_empty("#MainContent_ucVehicleInfo_txtAnnualMileage", mileage_val)
        applied["Annual Mileage"] = mileage_val

        return applied

    async def _open_vehicle_edit(row_index: int) -> bool:
        edit_btn = f"#MainContent_rpVehicles_btnViewEdit_{row_index}"
        if await page.locator(edit_btn).count() == 0:
            log("WARN", f"View/Edit button missing for row {row_index}")
            return False
        try:
            async with page.expect_navigation(timeout=30000, wait_until="networkidle"):
                await page.click(edit_btn, timeout=10000)
        except PlaywrightTimeoutError:
            await page.click(edit_btn, timeout=10000)
        except Exception:
            return False
        try:
            await page.wait_for_selector(
                "#MainContent_ucVehicleInfo_txtPurchaseDate, #MainContent_ucVehicleInfo_ddlOwnershipStatus",
                state="visible",
                timeout=20000,
            )
            return True
        except Exception:
            return await page.locator("#MainContent_ucVehicleInfo_formContainer").count() > 0

    async def _vehicle_save_errors() -> str:
        if await page.locator("#lstErrors li").count() == 0:
            return ""
        return (await page.locator("#lstErrors").inner_text()).strip()

    _SAVE_VEHICLE_SELECTORS = (
        "input#MainContent_btnSaveVehicle",
        "#MainContent_btnSaveVehicle",
    )
    _SAVE_VEHICLE_COV_SELECTORS = (
        "input#MainContent_btnSaveVehicleCov",
        "#MainContent_btnSaveVehicleCov",
        "#MainContent_divCovButtons input.btnRight",
    )

    async def _selector_visible(selectors: tuple[str, ...]) -> bool:
        for sel in selectors:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            try:
                if await loc.is_visible():
                    return True
            except Exception:
                return True
        return False

    async def _on_vehicle_list_page() -> bool:
        return await page.locator("a[id^='MainContent_rpVehicles_btnViewEdit_']").count() > 0

    async def _on_vehicle_edit_form() -> bool:
        return await _selector_visible(
            (
                "#MainContent_ucVehicleInfo_txtPurchaseDate",
                "input#MainContent_btnSaveVehicle",
                "#MainContent_btnSaveVehicle",
            )
        )

    async def _click_vehicle_submit(selectors: tuple[str, ...], label: str) -> bool:
        loc = None
        used_sel = ""
        for sel in selectors:
            candidate = page.locator(sel).first
            if await candidate.count() > 0:
                loc = candidate
                used_sel = sel
                break
        if loc is None:
            return False
        try:
            await loc.scroll_into_view_if_needed()
        except Exception:
            pass
        log("INFO", f"Clicking {label} ({used_sel})")
        try:
            async with page.expect_navigation(timeout=45000, wait_until="networkidle"):
                await loc.click(timeout=15000)
        except PlaywrightTimeoutError:
            await loc.click(timeout=15000)
        except Exception:
            btn_id = used_sel.split("#", 1)[-1]
            await page.evaluate(f"() => document.getElementById('{btn_id}')?.click()")
        try:
            await page.wait_for_load_state("networkidle", timeout=25000)
        except Exception:
            await page.wait_for_timeout(1200)
        err = await _vehicle_save_errors()
        if err:
            log("WARN", f"{label} validation: {err}")
            return False
        return True

    async def _wait_for_vehicle_cov_or_list(timeout_s: float = 35.0) -> str:
        """Return 'list', 'cov', or '' after polling."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if await _on_vehicle_list_page():
                return "list"
            if await _selector_visible(_SAVE_VEHICLE_COV_SELECTORS):
                return "cov"
            await page.wait_for_timeout(1000)
        return ""

    async def _save_vehicle_and_return_to_list() -> bool:
        """Save Vehicle -> Save Vehicle Coverages -> vehicle list (retry until success)."""
        for attempt in range(1, 7):
            log("INFO", f"Vehicle save flow attempt {attempt}/6")

            if await _on_vehicle_list_page():
                log("INFO", "On vehicle list — save flow complete")
                return True

            if await _selector_visible(_SAVE_VEHICLE_COV_SELECTORS):
                if not await _click_vehicle_submit(
                    _SAVE_VEHICLE_COV_SELECTORS, "Save Vehicle Coverages"
                ):
                    log("WARN", f"Save Vehicle Coverages click failed (attempt {attempt})")
                    await page.wait_for_timeout(1000)
                    continue
                state = await _wait_for_vehicle_cov_or_list(timeout_s=40.0)
                if state == "list" or await _on_vehicle_list_page():
                    await _wait_for_vehicle_list_stable()
                    log("INFO", "Returned to vehicle list after Save Vehicle Coverages")
                    return True
                continue

            if await _on_vehicle_edit_form() or await _selector_visible(_SAVE_VEHICLE_SELECTORS):
                if not await _click_vehicle_submit(_SAVE_VEHICLE_SELECTORS, "Save Vehicle"):
                    log("WARN", f"Save Vehicle button not found (attempt {attempt})")
                    await page.wait_for_timeout(1000)
                    continue

                state = await _wait_for_vehicle_cov_or_list(timeout_s=35.0)
                if state == "list" or await _on_vehicle_list_page():
                    await _wait_for_vehicle_list_stable()
                    log("INFO", "Returned to vehicle list after Save Vehicle")
                    return True
                if state == "cov":
                    continue
                log(
                    "WARN",
                    f"Still on vehicle edit form after Save Vehicle (attempt {attempt}); retrying",
                )
                continue

            await page.wait_for_timeout(1000)

        await _wait_for_vehicle_list_stable()
        if await _on_vehicle_list_page():
            return True
        log("WARN", "Not on vehicle list after save flow (all attempts exhausted)")
        return False

    def _ghl_index_for_portal_row(row: dict) -> int:
        gi = _ghl_index_for_portal_vin(row, vehicles)
        if gi >= 0:
            return gi
        plan = _fallback_match_vehicles([row], vehicles)
        matches = plan.get("matches", [])
        if matches:
            return int(matches[0]["ghl_idx"])
        return -1

    def _portal_row_for_vehicle_number(portal_rows: list[dict], vehicle_num: int) -> dict | None:
        for row in portal_rows:
            try:
                if int(row.get("unit", 0)) == vehicle_num:
                    return row
            except (TypeError, ValueError):
                pass
        if 1 <= vehicle_num <= len(portal_rows):
            return portal_rows[vehicle_num - 1]
        for row in portal_rows:
            if int(row.get("idx", -1)) == vehicle_num - 1:
                return row
        return None

    async def _fill_matched_vehicle(portal_idx: int, ghl_idx: int) -> bool:
        if ghl_idx < 0 or ghl_idx >= len(vehicles):
            return False
        ghl_v = vehicles[ghl_idx]
        log(
            "INFO",
            f"Editing portal row {portal_idx} with GHL vehicle {ghl_idx}: {_ghl_vehicle_description(ghl_v)}",
        )
        if not await _open_vehicle_edit(portal_idx):
            log("WARN", f"Could not open View/Edit for vehicle row {portal_idx}")
            return False
        field_values = await _fill_vehicle_edit_form(ghl_v)
        log("INFO", f"Vehicle row {portal_idx} field values: {json.dumps(field_values)}")
        if not await _save_vehicle_and_return_to_list():
            raise RuntimeError(
                f"Vehicle row {portal_idx} did not return to list after Save Vehicle / Save Vehicle Coverages"
            )
        return True

    async def _fill_all_list_vehicles() -> None:
        """Open every vehicle row on the list and fill using VIN-first CRM match."""
        used_ghl: set[int] = set()
        for _ in range(3):
            portal_rows = await _scrape_portal_vehicle_rows()
            if not portal_rows:
                break
            progressed = False
            for row in portal_rows:
                portal_idx = int(row["idx"])
                ghl_idx = _ghl_index_for_portal_row(row)
                if ghl_idx < 0:
                    log("WARN", f"No CRM match for portal row {portal_idx} ({row.get('name')}); skipping fill")
                    continue
                if ghl_idx in used_ghl:
                    log("WARN", f"CRM vehicle {ghl_idx} already used; row {portal_idx} uses same match")
                if await _fill_matched_vehicle(portal_idx, ghl_idx):
                    used_ghl.add(ghl_idx)
                    progressed = True
            if not progressed:
                break

    async def _retry_fill_from_validation_error(err_text: str) -> None:
        portal_rows = await _scrape_portal_vehicle_rows()
        vehicle_nums = sorted({int(n) for n in re.findall(r"vehicle\s+(\d+)", err_text, re.I)})
        for vehicle_num in vehicle_nums:
            row = _portal_row_for_vehicle_number(portal_rows, vehicle_num)
            if not row:
                log("WARN", f"Could not find portal row for Vehicle {vehicle_num}")
                continue
            portal_idx = int(row["idx"])
            ghl_idx = _ghl_index_for_portal_row(row)
            if ghl_idx < 0:
                log("WARN", f"No CRM data for Vehicle {vehicle_num} (row {portal_idx})")
                continue
            log("INFO", f"Retrying fill for Vehicle {vehicle_num} (portal row {portal_idx})")
            await _fill_matched_vehicle(portal_idx, ghl_idx)

    await _set_umpd_reduced_by()
    await _wait_for_vehicle_list_stable()

    match_plan: dict[str, Any] = {"matches": [], "delete": []}
    portal_rows = await _scrape_portal_vehicle_rows()
    log("INFO", f"Portal vehicle rows: {portal_rows}")
    log("INFO", f"GHL vehicles ({len(vehicles)}): {[ _ghl_vehicle_description(v) for v in vehicles ]}")

    for _ in range(3):
        portal_rows = await _scrape_portal_vehicle_rows()
        if not portal_rows:
            break
        match_plan = await _gemini_match_vehicles(portal_rows, vehicles)
        delete_indices = sorted({int(i) for i in match_plan.get("delete", [])}, reverse=True)
        if not delete_indices:
            break
        for idx in delete_indices:
            log("INFO", f"Deleting portal vehicle row {idx} (not in CRM match plan)")
            if not await _delete_vehicle_row(idx):
                log("WARN", f"Could not delete vehicle row {idx}")

    log("INFO", f"Vehicle match plan after cleanup: {json.dumps(match_plan, default=str)}")

    await _fill_all_list_vehicles()

    # Inline click next logic with fallbacks
    navigated = False
    for attempt in range(5):
        log("INFO", f"Continue click attempt {attempt + 1}")
        click_success = False
        for sel in ["#MainContent_btnContinue", "input[name$='btnContinue']", "button:has-text('Continue')", "button:has-text('Next')"]:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    log("INFO", f"Clicking {sel} using playwright")
                    await btn.click(timeout=10000)
                    click_success = True
                    break
            except Exception:
                pass
                
        if not click_success:
            try:
                await page.evaluate("document.getElementById('MainContent_btnContinue')?.click()")
                click_success = True
            except Exception:
                pass
                
        if click_success:
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            if "VehicleInfo" not in page.url:
                log("INFO", "Successfully navigated away from VehicleInfo page")
                navigated = True
                break
            else:
                log("WARN", "Still on VehicleInfo page after clicking. Checking for validation errors...")
                err_text = ""
                if await page.locator("#lstErrors li").count() > 0:
                    err_text = (await page.locator("#lstErrors").inner_text()).strip()
                    if err_text:
                        log("WARN", f"VehicleInfo validation errors found: {err_text}")
                if err_text:
                    await _retry_fill_from_validation_error(err_text)

        await page.wait_for_timeout(1000)
        
    if not navigated:
        if "VehicleInfo" in page.url and await page.locator("#lstErrors li").count() > 0:
            err = (await page.locator("#lstErrors").inner_text()).strip()
            raise RuntimeError(f"Vehicle validation blocked continue: {err}")
        else:
            raise RuntimeError("Could not click Continue on Vehicles")

    log("INFO", "Completed page_13_vehicles")

# - PAGE 14: AUTO UNDERWRITING --------------------

_AUTO_UNDERWRITING_QUESTIONS: tuple[tuple[str, str, str | None], ...] = (
    ("#MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_0", "Individual", "Individual"),
    ("#MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_1", "True", "Yes"),
    ("#MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_2", "False", "No"),
    ("#MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_3", "False", "No"),
)


async def page_14_auto_underwriting(page: Page) -> None:
    debug_step(14, "fill auto underwriting questions and continue", page=page)
    log("INFO", "Starting page_14_auto_underwriting")

    async def _fill_uw_dropdown(selector: str, value: str, label: str | None = None) -> None:
        loc = page.locator(selector).first
        if await loc.count() == 0:
            log("WARN", f"Auto underwriting dropdown not found: {selector}")
            return
        try:
            if not await loc.is_visible():
                return
        except Exception:
            pass
        try:
            current = (await loc.input_value()).strip()
            if current == value:
                return
        except Exception:
            pass
        try:
            await loc.select_option(value=value)
            log("INFO", f"Set {selector} -> {value}")
            return
        except Exception:
            pass
        if label:
            try:
                await loc.select_option(label=label)
                log("INFO", f"Set {selector} -> {label}")
            except Exception as exc:
                log("WARN", f"Could not set {selector} to {value!r}: {exc}")

    try:
        await page.wait_for_selector(
            "#MainContent_ucAutoQuestions_pnlQuestionGroup, #MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_0",
            state="visible",
            timeout=30000,
        )
    except Exception:
        pass

    for selector, value, label in _AUTO_UNDERWRITING_QUESTIONS:
        await _fill_uw_dropdown(selector, value, label)
        await page.wait_for_timeout(400)

    if await page.locator("#lstErrors li").count() > 0:
        err = (await page.locator("#lstErrors").inner_text()).strip()
        if err:
            raise RuntimeError(f"Auto underwriting validation: {err}")

    navigated = False
    for attempt in range(5):
        log("INFO", f"Auto underwriting Continue attempt {attempt + 1}")
        click_success = False
        for sel in [
            "#MainContent_btnContinue",
            "input#MainContent_btnContinue",
            "input[name$='btnContinue']",
            "button:has-text('Next')",
            "button:has-text('Continue')",
        ]:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    try:
                        async with page.expect_navigation(timeout=45000, wait_until="networkidle"):
                            await btn.click(timeout=15000)
                    except PlaywrightTimeoutError:
                        await btn.click(timeout=15000)
                    click_success = True
                    break
            except Exception:
                pass
        if not click_success:
            try:
                await page.evaluate("document.getElementById('MainContent_btnContinue')?.click()")
                click_success = True
            except Exception:
                pass

        if click_success:
            try:
                await page.wait_for_load_state("networkidle", timeout=25000)
            except Exception:
                await page.wait_for_timeout(1200)

        if "AutoUnderwriting" not in page.url:
            log("INFO", "Navigated away from Auto Underwriting page")
            navigated = True
            break

        if await page.locator("#lstErrors li").count() > 0:
            err = (await page.locator("#lstErrors").inner_text()).strip()
            if err and "auto information question" in err.lower():
                log("WARN", f"Auto underwriting errors after Continue: {err}")
                for selector, value, label in _AUTO_UNDERWRITING_QUESTIONS:
                    await _fill_uw_dropdown(selector, value, label)
                await page.wait_for_timeout(800)
                continue

        await page.wait_for_timeout(1000)

    if not navigated:
        if await page.locator("#lstErrors li").count() > 0:
            err = (await page.locator("#lstErrors").inner_text()).strip()
            raise RuntimeError(f"Auto underwriting blocked continue: {err}")
        raise RuntimeError("Could not leave Auto Underwriting page (Next/Continue)")

    log("INFO", "Completed page_14_auto_underwriting")

# - PAGE 15: PREMIUM SUMMARY --------------------

_PREMIUM_SUMMARY_TERM = "12"
_PREMIUM_SUMMARY_PAY_METHOD = "AS"
_PREMIUM_SUMMARY_PAY_PLAN = "Installments"
_PREMIUM_SUMMARY_DRAFT_DAY = "15"
_PREMIUM_SUMMARY_TERM_SEL = "#MainContent_ucPaymentMethod_ddlTerm"
_PREMIUM_SUMMARY_PAY_METHOD_SEL = "#MainContent_ucPaymentMethod_ddlPayMethod"
_PREMIUM_SUMMARY_PAY_PLAN_SEL = "#MainContent_ucPaymentMethod_ddlPayPlan"
_PREMIUM_SUMMARY_DRAFT_DAY_SEL = "#MainContent_ucPaymentMethod_ddlDraftDay"
_PREMIUM_SUMMARY_RERATE_BTN = "#MainContent_ucRater_btnRate"
_PREMIUM_SUMMARY_RATES_TABLE = "#MainContent_ucRater_gvRates"
_PREMIUM_SUMMARY_QUOTE_PROPOSAL_BTN = "#MainContent_btnQuoteProposal"
_QUOTE_PROPOSAL_POPUP_TIMEOUT_MS = 180000  # 3 minutes for dynamic DisplayPDF popup


def _format_premium_amount(raw: str) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("$"):
        return text
    return f"${text}"


def _parse_premium_summary_table_rows(rows: list[tuple[str, str]]) -> dict[str, str]:
    """Map (label, amount) rows from gvRates to premium fields."""
    out: dict[str, str] = {}
    for label, amount in rows:
        lab = (label or "").strip().casefold()
        formatted = _format_premium_amount(amount)
        if not formatted:
            continue
        if lab == "auto premium":
            out["auto_premium"] = formatted
        elif lab == "home premium":
            out["home_premium"] = formatted
        elif lab == "total":
            out["premium"] = formatted
    return out


async def _premium_summary_wait_processing(page: Page, timeout_ms: int = 60000) -> None:
    mask = page.locator("#ProcessingMask, #OverlayBlock").first
    try:
        if await mask.count() > 0:
            await page.wait_for_function(
                """() => {
                    const m = document.querySelector('#ProcessingMask');
                    const o = document.querySelector('#OverlayBlock');
                    const hidden = (el) => !el || el.style.display === 'none' || getComputedStyle(el).display === 'none';
                    return hidden(m) && hidden(o);
                }""",
                timeout=timeout_ms,
            )
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 30000))
    except Exception:
        await page.wait_for_timeout(800)


async def _premium_summary_select_if_needed(page: Page, selector: str, value: str, *, retries: int = 3) -> None:
    loc = page.locator(selector).first
    if await loc.count() == 0:
        log("WARN", f"Premium summary dropdown not found: {selector}")
        return
    try:
        current = (await loc.input_value()).strip()
        if current == value:
            return
    except Exception:
        pass
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            await loc.wait_for(state="visible", timeout=10000)
            await loc.select_option(value=value, timeout=15000)
            actual = (await loc.input_value()).strip()
            if actual == value:
                log("INFO", f"Set {selector} -> {value!r}" + (f" (attempt {attempt})" if attempt > 1 else ""))
                await _premium_summary_wait_processing(page)
                return
            log("WARN", f"{selector}: selected but value is {actual!r} not {value!r} — retrying ({attempt}/{retries})")
        except Exception as exc:
            last_exc = exc
            log("WARN", f"{selector}: attempt {attempt}/{retries} failed: {exc}")
        if attempt < retries:
            await page.wait_for_timeout(1500)
    log("WARN", f"Could not set {selector} to {value!r} after {retries} attempts: {last_exc}")
    await _premium_summary_wait_processing(page)


async def _premium_summary_wait_ready(page: Page) -> None:
    await page.wait_for_selector(
        f"{_PREMIUM_SUMMARY_RERATE_BTN}, {_PREMIUM_SUMMARY_PAY_METHOD_SEL}",
        state="attached",
        timeout=30000,
    )
    await _premium_summary_wait_processing(page)


async def _premium_summary_wait_payment_ready(page: Page) -> None:
    """Wait until core payment dropdowns exist; term/draft day may load shortly after."""
    await page.wait_for_selector(
        f"{_PREMIUM_SUMMARY_PAY_METHOD_SEL}, {_PREMIUM_SUMMARY_PAY_PLAN_SEL}",
        state="attached",
        timeout=30000,
    )
    for sel in (_PREMIUM_SUMMARY_TERM_SEL, _PREMIUM_SUMMARY_DRAFT_DAY_SEL):
        try:
            await page.wait_for_selector(sel, state="attached", timeout=15000)
        except Exception:
            pass
    await _premium_summary_wait_processing(page)


async def _premium_summary_select_term_if_needed(page: Page) -> None:
    """Policy term — required before Re-Rate on some Premium Summary layouts."""
    for sel in (_PREMIUM_SUMMARY_TERM_SEL, "#MainContent_ddlTerm"):
        loc = page.locator(sel).first
        if await loc.count() == 0:
            continue
        try:
            current = (await loc.input_value()).strip()
            if current and current not in ("-1", ""):
                return
        except Exception:
            pass
        for value, label in (
            (_PREMIUM_SUMMARY_TERM, None),
            ("Annual", "Annual"),
            ("12", "12 Month"),
            ("12", "12 Months"),
        ):
            try:
                if label:
                    await loc.select_option(label=label, timeout=10000)
                else:
                    await loc.select_option(value=value, timeout=10000)
                log("INFO", f"Set {sel} -> {value!r}")
                await _premium_summary_wait_processing(page)
                return
            except Exception:
                continue
        log("WARN", f"Could not set policy term on {sel}")
        return


async def _premium_summary_set_payment_options(page: Page) -> None:
    """Term, pay method, pay plan (installments), withdrawal day — before Re-Rate."""
    log("INFO", "Setting Premium Summary payment options (term, pay method, plan, withdrawal day)")
    await _premium_summary_select_term_if_needed(page)
    await _premium_summary_select_if_needed(page, _PREMIUM_SUMMARY_PAY_METHOD_SEL, _PREMIUM_SUMMARY_PAY_METHOD)
    await _premium_summary_select_if_needed(page, _PREMIUM_SUMMARY_PAY_PLAN_SEL, _PREMIUM_SUMMARY_PAY_PLAN)
    await _premium_summary_select_if_needed(page, _PREMIUM_SUMMARY_DRAFT_DAY_SEL, _PREMIUM_SUMMARY_DRAFT_DAY)
    await _premium_summary_wait_processing(page)


async def _premium_summary_wait_rated(page: Page, timeout_ms: int = 90000) -> None:
    """Wait until Re-Rate has populated the Your Rate table (leaves 'Unrated' state)."""
    await page.wait_for_function(
        """() => {
            const msg = document.querySelector('#MainContent_ucRater_lblRateMessage');
            const msgText = ((msg && msg.innerText) || '').trim().toLowerCase();
            if (msgText && msgText !== 'unrated') {
                const t = document.querySelector('#MainContent_ucRater_gvRates');
                if (t) {
                    for (const r of t.querySelectorAll('tr')) {
                        const cells = r.querySelectorAll('td');
                        if (cells.length >= 2) {
                            const label = (cells[0].innerText || '').trim().toLowerCase();
                            if (label === 'total') return true;
                        }
                    }
                }
            }
            const t = document.querySelector('#MainContent_ucRater_gvRates');
            if (!t) return false;
            for (const r of t.querySelectorAll('tr')) {
                const cells = r.querySelectorAll('td');
                if (cells.length >= 2) {
                    const label = (cells[0].innerText || '').trim().toLowerCase();
                    if (label === 'total') return true;
                }
            }
            return false;
        }""",
        timeout=timeout_ms,
    )


async def _premium_summary_click_rerate(page: Page) -> bool:
    """Click Re-Rate even when off-screen (exists in DOM but not Playwright-visible)."""
    rerate = page.locator(_PREMIUM_SUMMARY_RERATE_BTN).first
    if await rerate.count() == 0:
        log("WARN", "Re-Rate button not found in DOM")
        return False

    for panel_sel in (
        "#MainContent_ucRater_pnlRate",
        _PREMIUM_SUMMARY_RATES_TABLE,
        _PREMIUM_SUMMARY_RERATE_BTN,
    ):
        panel = page.locator(panel_sel).first
        if await panel.count() > 0:
            try:
                await panel.scroll_into_view_if_needed(timeout=8000)
            except Exception:
                pass

    log("INFO", "Clicking Re-Rate on Premium Summary")
    clicked = False
    for attempt, action in enumerate(
        (
            ("click", lambda: rerate.click(timeout=15000, no_wait_after=True)),
            ("force click", lambda: rerate.click(timeout=15000, force=True, no_wait_after=True)),
            (
                "js click",
                lambda: page.evaluate(
                    "document.getElementById('MainContent_ucRater_btnRate')?.click()"
                ),
            ),
        ),
        start=1,
    ):
        label, fn = action
        try:
            await fn()
            clicked = True
            log("INFO", f"Re-Rate clicked ({label}, attempt {attempt})")
            break
        except Exception as exc:
            log("WARN", f"Re-Rate {label} failed (attempt {attempt}): {exc}")

    if not clicked:
        log("WARN", "Could not click Re-Rate after all strategies")
        return False

    await _premium_summary_wait_processing(page, timeout_ms=90000)
    try:
        await _premium_summary_wait_rated(page, timeout_ms=90000)
        log("INFO", "Premium Summary rated — rate table ready")
    except Exception:
        log("WARN", "Timed out waiting for rated Total row after Re-Rate")
    return True


async def _extract_premium_summary_rates(page: Page) -> dict[str, str]:
    rates: dict[str, str] = {}
    table = page.locator(_PREMIUM_SUMMARY_RATES_TABLE)
    if await table.count() > 0:
        rows = await table.locator("tr").evaluate_all(
            """rows => rows.map(r => {
                const cells = Array.from(r.querySelectorAll('td')).map(c => (c.innerText || '').trim());
                if (cells.length >= 2) return [cells[0], cells[1]];
                return null;
            }).filter(Boolean)"""
        )
        rates = _parse_premium_summary_table_rows([(r[0], r[1]) for r in rows if r])

    if not rates.get("auto_premium"):
        loc = page.locator("#MainContent_ucPPASummary_lblDisplayVehiclePrem").first
        if await loc.count() > 0:
            text = _format_premium_amount(await loc.inner_text())
            if text:
                rates["auto_premium"] = text
    if not rates.get("home_premium"):
        loc = page.locator("#MainContent_ucHODFSummary_lblDisplayTotalLocationPremium").first
        if await loc.count() > 0:
            text = _format_premium_amount(await loc.inner_text())
            if text:
                rates["home_premium"] = text

    if rates.get("auto_premium") or rates.get("home_premium"):
        log(
            "INFO",
            f"Premium summary rates: auto={rates.get('auto_premium')!r} "
            f"home={rates.get('home_premium')!r} total={rates.get('premium')!r}",
        )
    return rates


def _is_pdf_bytes(body: bytes | None) -> bool:
    return bool(body) and len(body) >= 4 and body[:4] == b"%PDF"


def _is_pdf_candidate_response(response) -> bool:
    try:
        ct = ((response.headers or {}).get("content-type") or "").lower()
        url = (response.url or "").lower()
        if "application/pdf" in ct:
            return True
        if url.endswith(".pdf"):
            return True
        if "displaypdf.aspx" in url:
            return True
        if "iid=" in url and ("natgenagency.com" in url or "natgen" in url):
            return True
        return False
    except Exception:
        return False


def _quote_proposal_contact_name_slug(contact: dict | None) -> str:
    first = _contact_first(contact or {}, "firstName", "first_name") or ""
    last = _contact_first(contact or {}, "lastName", "last_name") or ""
    combined = f"{first} {last}".strip()
    if not combined:
        return "unknown"
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", combined).strip("_")
    return slug[:120] if slug else "unknown"


def _quote_proposal_pdf_filename(
    name_slug: str, quote_num: str, ts: str, suffix: str = ""
) -> str:
    parts = [name_slug, "quote_proposal"]
    if quote_num and quote_num != "unknown":
        parts.append(quote_num)
    parts.append(ts)
    if suffix:
        parts.append(suffix)
    return "_".join(parts) + ".pdf"


async def _save_quote_proposal_bytes(
    pdf_dir: Path,
    name_slug: str,
    quote_num: str,
    ts: str,
    body: bytes,
    suffix: str = "",
) -> str:
    path = pdf_dir / _quote_proposal_pdf_filename(name_slug, quote_num, ts, suffix)
    await asyncio.to_thread(path.write_bytes, body)
    log("OK", f"Saved Quote Proposal PDF -> {path}")
    return str(path)


async def _fetch_pdf_from_url(page: Page, url: str) -> bytes | None:
    url = (url or "").strip()
    if not url or url == "about:blank" or url.lower().startswith(("chrome://", "devtools://")):
        return None
    try:
        resp = await page.context.request.get(url, timeout=90000)
        if resp.ok:
            body = await resp.body()
            if _is_pdf_bytes(body):
                return body
    except Exception as exc:
        log("DEBUG", f"PDF URL fetch failed ({url[:100]}): {exc}")
    return None


async def _fetch_pdf_from_popup(popup: Page) -> bytes | None:
    try:
        data = await popup.evaluate(
            """async () => {
                const href = window.location.href || '';
                if (!href.startsWith('blob:') && !href.startsWith('http')) return null;
                try {
                    const r = await fetch(href);
                    const buf = await r.arrayBuffer();
                    const bytes = new Uint8Array(buf);
                    if (bytes.length < 4) return null;
                    if (bytes[0] !== 0x25 || bytes[1] !== 0x50 || bytes[2] !== 0x44 || bytes[3] !== 0x46) {
                        return null;
                    }
                    return Array.from(bytes);
                } catch (e) {
                    return null;
                }
            }"""
        )
        if data:
            body = bytes(data)
            if _is_pdf_bytes(body):
                return body
    except Exception as exc:
        log("DEBUG", f"Popup in-page PDF fetch failed: {exc}")
    return None


async def _body_from_pdf_response(response) -> bytes | None:
    if not _is_pdf_candidate_response(response):
        return None
    try:
        body = await response.body()
        return body if _is_pdf_bytes(body) else None
    except Exception:
        return None


async def _extract_embed_pdf_url(popup: Page) -> str | None:
    try:
        href = await popup.evaluate(
            """() => {
                const pick = (el) => (el && (el.src || el.data || el.getAttribute('data'))) || '';
                const embed = document.querySelector('embed[type="application/pdf"], embed');
                if (embed) {
                    const u = pick(embed);
                    if (u) return u;
                }
                const obj = document.querySelector('object[type="application/pdf"], object');
                if (obj) {
                    const u = pick(obj);
                    if (u) return u;
                }
                const iframe = document.querySelector('iframe');
                if (iframe && iframe.src) return iframe.src;
                return '';
            }"""
        )
        href = (href or "").strip()
        if not href:
            return None
        if href.startswith("//"):
            return f"https:{href}"
        if href.startswith("/"):
            base = (popup.url or "").strip()
            if base.startswith("http"):
                from urllib.parse import urljoin
                return urljoin(base, href)
        return href if href.startswith(("http://", "https://", "blob:")) else None
    except Exception as exc:
        log("DEBUG", f"Embed PDF URL extraction failed: {exc}")
        return None


def _is_quote_proposal_popup(candidate: Page, main_page: Page) -> bool:
    if candidate == main_page:
        return False
    url = (candidate.url or "").strip().lower()
    if not url or url == "about:blank":
        return False
    if "displaypdf.aspx" in url:
        return True
    return "iid=" in url and "natgenagency.com" in url


async def _wait_for_quote_proposal_popup(
    context: BrowserContext, main_page: Page, timeout_ms: int = _QUOTE_PROPOSAL_POPUP_TIMEOUT_MS
) -> Page | None:
    """Wait up to 3 minutes for the dynamic DisplayPDF popup after Quote Proposal click."""
    log("INFO", f"Waiting for Quote Proposal popup (max {timeout_ms // 1000}s)...")
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    last_log = 0.0

    while time.monotonic() < deadline:
        for candidate in context.pages:
            if _is_quote_proposal_popup(candidate, main_page):
                try:
                    await candidate.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                log("INFO", f"Quote Proposal popup detected: {(candidate.url or '')[:120]}")
                return candidate

        remaining_ms = int(max(500, (deadline - time.monotonic()) * 1000))
        try:
            new_page = await context.wait_for_event("page", timeout=min(3000, remaining_ms))
            if _is_quote_proposal_popup(new_page, main_page):
                try:
                    await new_page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                log("INFO", f"Quote Proposal popup opened: {(new_page.url or '')[:120]}")
                return new_page
        except PlaywrightTimeoutError:
            pass
        except Exception:
            pass

        now = time.monotonic()
        if now - last_log >= 15.0:
            last_log = now
            log("INFO", "Still waiting for Quote Proposal popup (DisplayPDF)...")

        await asyncio.sleep(0.5)

    log("WARN", f"Quote Proposal popup did not appear within {timeout_ms // 1000}s")
    return None


async def _wait_for_popup_pdf_ready(
    popup: Page, pdf_responses: list, timeout_ms: int = 60000
) -> bool:
    """Wait until DisplayPDF popup has loaded PDF content (URL or captured response)."""
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        for resp in pdf_responses:
            if _is_pdf_candidate_response(resp):
                return True
        url = (popup.url or "").strip().lower()
        if "displaypdf.aspx" in url and "iid=" in url:
            return True
        try:
            ready = await popup.evaluate(
                """() => {
                    const href = (window.location.href || '').toLowerCase();
                    if (href.includes('displaypdf.aspx') && href.includes('iid=')) return true;
                    return !!document.querySelector(
                        'embed[type="application/pdf"], object[type="application/pdf"], iframe'
                    );
                }"""
            )
            if ready:
                return True
        except Exception:
            pass
        await popup.wait_for_timeout(1500)
    return False


async def _download_display_pdf_from_popup(
    page: Page, popup: Page, pdf_responses: list
) -> tuple[bytes | None, str]:
    """Download PDF bytes from DisplayPDF.aspx popup (not a screenshot)."""
    for resp in list(pdf_responses):
        body = await _body_from_pdf_response(resp)
        if body:
            log("INFO", f"PDF from captured network response: {(resp.url or '')[:120]}")
            return body, "popup_response"

    popup_url = (popup.url or "").strip()
    if popup_url:
        log("INFO", f"Quote Proposal popup URL: {popup_url[:120]}")

    if popup_url.startswith(("http://", "https://")):
        body = await _fetch_pdf_from_url(page, popup_url)
        if body:
            return body, "displaypdf_get"

    embed_url = await _extract_embed_pdf_url(popup)
    if embed_url:
        log("INFO", f"Trying embed PDF URL: {embed_url[:120]}")
        body = await _fetch_pdf_from_url(page, embed_url)
        if body:
            return body, "embed_src"

    body = await _fetch_pdf_from_popup(popup)
    if body:
        return body, "popup_fetch"

    return None, ""


async def _wait_and_save_popup_pdf(
    page: Page,
    popup: Page,
    pdf_dir: Path,
    name_slug: str,
    quote_num: str,
    ts: str,
    pdf_responses: list,
    on_response,
) -> str | None:
    """Wait for PDF in popup, download automatically, checkpoint only if save fails."""
    popup.on("response", on_response)

    try:
        try:
            await popup.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception:
            pass

        log("INFO", "Waiting for PDF to load in Quote Proposal popup...")
        pdf_ready = await _wait_for_popup_pdf_ready(popup, pdf_responses, timeout_ms=90000)
        if not pdf_ready:
            log("WARN", "PDF viewer not confirmed ready; attempting download anyway")

        log("INFO", "Starting Quote Proposal PDF download")
        for attempt in range(1, 11):
            body, strategy = await _download_display_pdf_from_popup(page, popup, pdf_responses)
            if body:
                suffix = strategy or "popup"
                log("INFO", f"Quote Proposal PDF download strategy: {suffix}")
                return await _save_quote_proposal_bytes(
                    pdf_dir, name_slug, quote_num, ts, body, suffix
                )
            if attempt < 10:
                log("INFO", f"PDF not saved yet, retrying ({attempt}/10)...")
                await popup.wait_for_timeout(2000)

        log("WARN", "Could not save PDF automatically — pausing for manual review")
        await _checkpoint_wait_for_enter(
            "Quote Proposal PDF could not be saved automatically. Review the PDF in the browser, "
            "then press Enter to retry save or continue. The browser will stay open."
        )
        for attempt in range(1, 6):
            body, strategy = await _download_display_pdf_from_popup(page, popup, pdf_responses)
            if body:
                suffix = strategy or "popup"
                return await _save_quote_proposal_bytes(
                    pdf_dir, name_slug, quote_num, ts, body, suffix
                )
            await popup.wait_for_timeout(2000)

        log("WARN", "Could not save PDF after checkpoint — popup left open for manual save")
        return None
    finally:
        try:
            popup.remove_listener("response", on_response)
        except Exception:
            pass


async def _click_quote_proposal_btn(page: Page, btn) -> None:
    """Click Quote Proposal without waiting for main-page navigation (opens DisplayPDF popup)."""
    await _premium_summary_wait_processing(page)
    try:
        await btn.scroll_into_view_if_needed(timeout=8000)
    except Exception:
        pass
    log("INFO", "Clicking Quote Proposal")
    try:
        await btn.click(timeout=15000, no_wait_after=True)
        log("INFO", "Quote Proposal click sent (no_wait_after)")
        return
    except Exception as exc:
        log("WARN", f"Quote Proposal locator click failed: {exc} — checking for popup anyway")
    try:
        await page.evaluate(
            "document.getElementById('MainContent_btnQuoteProposal')?.click()"
        )
        log("INFO", "Quote Proposal click sent (JS fallback)")
    except Exception as exc:
        log("WARN", f"Quote Proposal JS click failed: {exc}")


async def _download_quote_proposal_pdf(page: Page, contact: dict | None = None) -> str | None:
    btn = page.locator(_PREMIUM_SUMMARY_QUOTE_PROPOSAL_BTN).first
    if await btn.count() == 0:
        log("WARN", "Quote Proposal button not found on Premium Summary")
        return None

    pdf_dir = ARTIFACTS_DIR / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    name_slug = _quote_proposal_contact_name_slug(contact)
    quote_num = "unknown"
    try:
        quote_info = (await page.locator("#lblContainerHighLevelInfo").first.inner_text()).strip()
        m = re.search(r"(\d{6,})", quote_info)
        if m:
            quote_num = m.group(1)
    except Exception:
        pass
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    pdf_responses: list = []

    def _on_response(response) -> None:
        try:
            if _is_pdf_candidate_response(response):
                pdf_responses.append(response)
        except Exception:
            pass

    page.context.on("response", _on_response)

    popup = None
    try:
        await _click_quote_proposal_btn(page, btn)
        popup = await _wait_for_quote_proposal_popup(
            page.context, page, timeout_ms=_QUOTE_PROPOSAL_POPUP_TIMEOUT_MS
        )

        if popup is not None:
            path = await _wait_and_save_popup_pdf(
                page,
                popup,
                pdf_dir,
                name_slug,
                quote_num,
                ts,
                pdf_responses,
                _on_response,
            )
            if path:
                return path
        else:
            log("WARN", "No Quote Proposal popup within 3 minutes")
    finally:
        try:
            page.context.remove_listener("response", _on_response)
        except Exception:
            pass

    log("INFO", "Retrying Quote Proposal via direct download")
    try:
        async with page.expect_download(timeout=90000) as dl_info:
            await _click_quote_proposal_btn(page, btn)
        download = await dl_info.value
        suggested = (download.suggested_filename or "quote_proposal.pdf").strip()
        if not suggested.lower().endswith(".pdf"):
            suggested = f"{suggested}.pdf"
        path = pdf_dir / _quote_proposal_pdf_filename(name_slug, quote_num, ts, suggested)
        await download.save_as(str(path))
        log("OK", f"Downloaded Quote Proposal PDF -> {path}")
        return str(path)
    except Exception as exc:
        log("WARN", f"Quote Proposal direct download failed: {exc}")

    return None


async def page_15_premium_summary(page: Page, contact: dict | None = None) -> dict:
    debug_step(15, "refresh, payment options, rerate, extract, quote proposal", page=page)
    log("INFO", "Starting page_15_premium_summary")

    await _premium_summary_wait_ready(page)

    log("INFO", "Refreshing Premium Summary")
    await page.reload(wait_until="networkidle")
    await _premium_summary_wait_ready(page)
    await _premium_summary_wait_payment_ready(page)
    await _premium_summary_set_payment_options(page)

    if not await _premium_summary_click_rerate(page):
        raise RuntimeError("Re-Rate failed on Premium Summary")

    rates = await _extract_premium_summary_rates(page)
    premium = rates.get("premium")
    auto_premium = rates.get("auto_premium")
    home_premium = rates.get("home_premium")
    if not premium and auto_premium and home_premium:
        try:
            auto_val = float(re.sub(r"[^\d.]", "", auto_premium))
            home_val = float(re.sub(r"[^\d.]", "", home_premium))
            premium = _format_premium_amount(f"{auto_val + home_val:,.2f}")
            log("INFO", f"Derived total premium from auto+home: {premium}")
        except (TypeError, ValueError):
            pass
    if not premium:
        raise RuntimeError("Could not extract total premium from Premium Summary rate table")

    pdf_path = await _download_quote_proposal_pdf(page, contact)

    result = {
        "premium": premium,
        "auto_premium": auto_premium,
        "home_premium": home_premium,
        "pay_plan": _PREMIUM_SUMMARY_PAY_PLAN,
        "pdf_path": pdf_path,
    }
    log("INFO", f"Completed page_15_premium_summary: {result}")
    return result

# - RATE QUOTE & DOWNLOAD (legacy) -----------

async def extract_premium(page: Page) -> str | None:
    for attempt in range(5):
        if "RateQuote" not in page.url:
            log("INFO", f"Not on RateQuote page yet: {page.url}, wait {attempt}")
            await page.wait_for_timeout(3000)
            continue
        try:
            for sel in ("#lblPackageTotalPremiumAmount", "#MainContent_lblTotalPremium"):
                if await page.locator(sel).count() > 0:
                    text = (await page.locator(sel).inner_text()).strip()
                    if text:
                        log("OK", f"Extracted premium: {text}")
                        return text
        except Exception as e:
            log("WARN", f"Error extracting premium: {e}")
            await page.wait_for_timeout(2000)
    return None

async def download_proposal_pdf(page: Page) -> str | None:
    if await page.locator("#MainContent_btnPrintProposal").count() == 0:
        return None
    try:
        async with page.expect_download(timeout=30000) as dl_info:
            await page.click("#MainContent_btnPrintProposal")
        download = await dl_info.value
        path = ARTIFACTS_DIR / download.suggested_filename
        await download.save_as(str(path))
        log("OK", f"Downloaded PDF -> {path}")
        return str(path)
    except Exception as e:
        log("WARN", f"Failed to download PDF: {e}")
        return None

# - ORCHESTRATOR ---------------------------

def normalize_run_bot_result(raw: dict) -> dict:
    """Map raw run_bot output to the worker/GHL contract."""
    premium = raw.get("premium")
    pdf_path = raw.get("pdf_path")
    base = {
        "premium": premium,
        "pdf_path": pdf_path,
        "total_premium": premium or "$0.00",
        "home_premium": raw.get("home_premium") or "$0.00",
        "auto_premium": raw.get("auto_premium") or "$0.00",
        "pay_plan": raw.get("pay_plan") or "",
    }
    if raw.get("error"):
        return {"success": False, "error": raw["error"], **base}
    if premium and pdf_path:
        return {"success": True, "error": None, **base}
    return {"success": False, "error": "Missing premium or PDF", **base}


async def run_bot(contact: dict) -> dict:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    contact = _normalize_contact_payload(contact)
    enrich_contact_from_custom_fields(contact)

    username = os.environ.get("NATGEN_USERNAME", "")
    password = os.environ.get("NATGEN_PASSWORD", "")
    agent_id = os.environ.get("NATGEN_AGENT_ID", "")
    otp_sender = os.environ.get("NATGEN_2FA_SENDER", "43015")
    otp_timeout = int(os.environ.get("NATGEN_2FA_TIMEOUT", "90"))
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
            log("DEBUG", "========== BOT RUN START (pages 01–15 + post-flow) ==========")
            await run_bot_page(
                1, page, page_1_login,
                page, username, password, otp_sender, otp_timeout, otp_initial_wait, context,
            )

            await run_bot_page(2, page, page_2_select_state_product, page)
            await run_bot_page(3, page, page_3_search_add_customer, page, contact)
            await run_bot_page(4, page, page_4_client_info_p1, page, contact)
            await run_bot_page(5, page, page_5_client_info_p2, page, agent_id)
            await run_bot_page(6, page, page_6_prefill, page)
            await run_bot_page(7, page, page_7_property, page, contact)
            await run_bot_page(8, page, page_8_underwriting, page, contact)

            loss_pass = 0
            while True:
                loss_pass += 1
                await run_bot_page(
                    9, page, page_9_loss_history, page,
                    note=f"pass {loss_pass}",
                )
                if "LossHistory" not in page.url:
                    if loss_pass > 1:
                        debug_step(9, f"left Loss History after {loss_pass} passes", page=page)
                    break

            await run_bot_page(10, page, page_10_coverage, page, contact)

            if "DriverInfo" in page.url:
                await run_bot_page(11, page, page_11_driver, page, contact)
            else:
                debug_page_skip(11, "URL does not contain DriverInfo", page=page)

            if "DriverViolations" in page.url:
                await run_bot_page(12, page, page_12_driver_violations, page)
            else:
                debug_page_skip(12, "URL does not contain DriverViolations", page=page)

            if "VehicleInfo" in page.url:
                await run_bot_page(13, page, page_13_vehicles, page, contact)
            else:
                debug_page_skip(13, "URL does not contain VehicleInfo", page=page)

            if "AutoUnderwriting" in page.url:
                await run_bot_page(14, page, page_14_auto_underwriting, page)
            else:
                debug_page_skip(14, "URL does not contain AutoUnderwriting", page=page)

            if "PremiumSummary" in page.url:
                summary = await run_bot_page(15, page, page_15_premium_summary, page, contact)
                if isinstance(summary, dict):
                    results.update(summary)
            elif "RateQuote" in page.url:
                debug_post_flow("Rate Quote → extract premium", page=page)
                results["premium"] = await extract_premium(page)
                debug_post_flow("Rate Quote → download proposal PDF", page=page)
                results["pdf_path"] = await download_proposal_pdf(page)
                debug_post_flow(
                    "Rate Quote complete",
                    page=page,
                    detail=f"premium={results['premium']!r} pdf={results['pdf_path']!r}",
                )
            else:
                debug_post_flow("expected Premium Summary or Rate Quote but landed elsewhere", page=page)
                log("WARN", f"Expected PremiumSummary/RateQuote, but ended on {page.url}")

            log("DEBUG", "========== BOT RUN FINISHED ==========")

        except Exception as e:
            log("FAIL", f"Bot execution failed: {e}")
            results["error"] = str(e)
            await _save_html(page, "error")
            await _save_screenshot(page, "error")
            log("INFO", "Waiting 5s before closing browser so you can inspect the failure...")
            await asyncio.sleep(5)

        finally:
            await browser.close()

        return normalize_run_bot_result(results)

# - TEST SMOKE ----------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    test_contact = {
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
    
    res = asyncio.run(run_bot(test_contact))
    print("Test Output:", res)
