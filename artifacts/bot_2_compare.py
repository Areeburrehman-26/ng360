import asyncio
import json
import logging
import os
import random
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

def _contact_first(contact: dict, *keys: str) -> str | None:
    for k in keys:
        v = contact.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None

def _date(raw: str) -> str:
    mmddyyyy = format_date_to_mmddyyyy(raw)
    return f"{mmddyyyy[0:2]}/{mmddyyyy[2:4]}/{mmddyyyy[4:8]}"


# --- Vehicle matching helpers (ported from bridge_bot.py) ---

_GEMINI_VEHICLE_MODEL = "gemini-2.5-flash"

_VEHICLE_TRIM_TOKENS: frozenset[str] = frozenset({
    "lt", "ls", "ltz", "premier", "platinum", "custom", "sport", "utility",
    "vehicle", "pickup", "owned", "distance", "driven", "chev", "chevy", "chevrolet",
})

_VEHICLE_TOKEN_ALIASES: dict[str, str] = {
    "chev": "chevrolet",
    "chevy": "chevrolet",
}

_VIN_MODEL_YEAR_CODES: dict[str, int] = {
    **{str(d): 2000 + d for d in range(1, 10)},
    **{c: 2010 + i for i, c in enumerate("ABCDEFGHJKLMNPRSTVWXY")},
}


def _normalize_vin(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", (value or "").upper())


def _vin_model_year(vin: str) -> str:
    v = _normalize_vin(vin)
    if len(v) < 10:
        return ""
    code = v[9].upper()
    year = _VIN_MODEL_YEAR_CODES.get(code)
    return str(year) if year else ""


def _extract_year_from_text(text: str) -> str:
    m = re.search(r"\b(19|20)\d{2}\b", text or "")
    return m.group(0) if m else ""


def _strip_crm_vehicle_noise(text: str) -> str:
    s = re.sub(r"\s+", " ", (text or "")).strip()
    s = re.sub(r"(?i)\bdistance\s+driven\b", "", s).strip()
    s = re.sub(r"(?i)\bowned\b", "", s).strip()
    s = re.sub(r"(?i)\b[A-Z]+-(?:PICKUP|SPORT\s+UTILITY\s+VEHICLE)\b", "", s).strip()
    return re.sub(r"\s+", " ", s).strip()


def _ghl_vehicle_description(v: dict) -> str:
    parts = [
        str(v.get("year") or "").strip(),
        str(v.get("make") or "").strip(),
        str(v.get("model") or "").strip(),
        str(v.get("submodel") or "").strip(),
    ]
    return " ".join(p for p in parts if p)


def _ghl_vehicle_core_text(v: dict) -> str:
    year = str(v.get("year") or "").strip()
    make = _strip_crm_vehicle_noise(str(v.get("make") or ""))
    model = _strip_crm_vehicle_noise(str(v.get("model") or ""))
    model = re.split(r"\s{2,}", model)[0].strip()
    return " ".join(p for p in (year, make, model) if p)


def _ghl_vehicle_year(v: dict) -> str:
    raw = str(v.get("year") or "").strip()
    if re.fullmatch(r"\d{4}", raw):
        return raw
    return _extract_year_from_text(_ghl_vehicle_description(v))


def _portal_vehicle_year(row: dict) -> str:
    name = str(row.get("name") or "")
    y = _extract_year_from_text(name)
    if y:
        return y
    return _vin_model_year(str(row.get("vin") or ""))


def _clean_portal_vehicle_name(raw: str) -> str:
    name = re.sub(r"\s+", " ", (raw or "")).strip()
    name = re.sub(r"(?i)\badd\s+coverages\b", "", name).strip()
    name = re.sub(r"(?i)\bview\s*/\s*edit\b", "", name).strip()
    name = re.sub(r"(?i)\bdelete\b", "", name).strip()
    name = re.sub(r"^\d+\s+", "", name).strip()
    name = re.sub(r"\s+-\s*1\s*$", "", name).strip()
    name = re.sub(r"\s+-\s*1\b", " ", name).strip()
    return re.sub(r"\s+", " ", name).strip()


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
        out.append({
            "idx": idx,
            "unit": unit,
            "name": _clean_portal_vehicle_name(str(row.get("name") or "")),
            "vin": _normalize_vin(str(row.get("vin") or "")),
        })
    return out


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
    return len(inter) / max(len(ta | tb), 1)


def _vehicle_line_similarity(a: str, b: str) -> float:
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
    score = len(inter) / max(len(ta | tb), 1)
    if fa and fb and (fa & fb):
        score = max(score, 0.35)
    body = {"k1500", "k2500", "k3500", "1500", "2500", "3500"}
    if (ta & body) and (tb & body) and (ta & body) & (tb & body):
        score = min(score + 0.15, 1.0)
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


def _vin_prefix_usable(vin_prefix: str) -> bool:
    p = _normalize_vin(vin_prefix)
    if len(p) < 8:
        return False
    if p.endswith("000000") or p.endswith("00000"):
        return False
    return True


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
        log("WARN", f"CRM year {ghl_year} != portal year {portal_year} but same model line")
        return True
    return False


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


def _finalize_vehicle_match_plan(plan: dict, portal_vehicles: list[dict], ghl_vehicles: list[dict]) -> dict:
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
            log("WARN", f"Rejecting match portal {p_idx} -> ghl {g_idx}: year mismatch")
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
    portal_vehicles = _normalize_portal_vehicle_rows(portal_vehicles)
    if not ghl_vehicles:
        return {"matches": [], "delete": [int(p["idx"]) for p in portal_vehicles]}

    candidates: list[tuple[float, int, int]] = []
    for p in portal_vehicles:
        p_idx = int(p["idx"])
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
    for score, p_idx, gi in candidates:
        if p_idx in used_portal or gi in used_ghl:
            continue
        if score < 1.0 and score < 0.12:
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
        raw = raw[start: end + 1]
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
            norm_matches.append({"portal_idx": int(m["portal_idx"]), "ghl_idx": int(m["ghl_idx"])})
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
        "- Trim differences on the SAME model line ARE the same vehicle.\n"
        "- Match on year + make + model line, not exact trim.\n\n"
        f"Portal vehicles: {json.dumps(portal_payload)}\n"
        f"GHL vehicles: {json.dumps(ghl_payload)}\n\n"
        "Return ONLY valid JSON (no markdown prose) with this shape:\n"
        '{"matches":[{"portal_idx":0,"ghl_idx":0}],"delete":[1]}\n'
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


# --- AI-driven underwriting question answering ---
# The Auto Underwriting page's question set/order shifts by state and quote
# variant (confirmed across SC/GA runs), so hardcoded field-index logic keeps
# breaking. Instead: scrape whatever questions are actually on the page by
# label text, ask Gemini to answer them against our standing business rules
# (never touching coverage/limit/deductible fields), and fall back to the
# same rules applied locally by keyword match if Gemini is unavailable.

_GEMINI_UW_MODEL = "gemini-2.5-flash"

_UW_SYSTEM_PROMPT = """You are filling out an auto insurance underwriting questionnaire on behalf
of an insurance agency (Trustwell Insurance). You will be given CONTACT_DATA
and a JSON list of questions currently visible on the page, each with:
  - field_id      : the HTML element id to answer
  - field_type    : "select" (dropdown) or "text" (free text input)
  - question_num  : the question's displayed number (e.g. "5")
  - label         : the exact question text
  - options       : for "select" fields, the list of {value, label} choices
                     available (only pick from these — never invent a value)

Your job: return ONLY a JSON object mapping field_id -> the value to set,
for every question you can confidently answer using the RULES below.

STRICT REQUIREMENTS:
  - For "select" fields, the value you return MUST exactly match one of the
    option "value" strings given for that field. Never guess a value that
    isn't in the options list.
  - For "text" fields, return a plain string.
  - If a question is NOT covered by the RULES below and you are not
    confident of the correct answer, OMIT it from the JSON entirely
    (do not guess). Omitted fields are left untouched and flagged for
    human review.
  - Never answer a question that changes coverage amounts, premium,
    deductibles, or policy limits — those are handled by separate,
    carrier-approved logic elsewhere in the bot. Only answer
    underwriting/eligibility yes-no-type questions.
  - Return raw JSON only. No markdown fences, no commentary.

RULES (our standing answers — apply whenever a question matches):
  1. "Named Insured Type" -> "Individual"
  2. "Does Applicant/Co-Applicant/Spouse own a residential property..." -> Yes (true)
  3. "Do any operators have a Company car?" -> No (false)
  4. "Affinity Group" (free text) -> omit, not required
  5. "Have you had any losses in the previous 5 years?" -> No (false)
  6. "How many years has the insured had uninterrupted/continuous
     insurance?" (free text) -> use CONTACT_DATA.years_continuous_ins if
     provided, otherwise "4". (Same value already used on the Home
     Underwriting page for continuous property insurance — keep answers
     consistent between Home and Auto.)
  7. "Years with Prior Auto Carrier" (free text, only if NOT disabled) -> "4"
  8. Any question asking about site/road access (e.g. "flat area, easy
     access roads") -> select the "flat area / easy access" option
  9. Any question about paperless/electronic delivery/documents -> Yes (true)
 10. Any other yes/no underwriting question not covered above whose label
     clearly matches a standard eligibility check (prior claims, violations,
     business use, etc.) -> default to No (false) UNLESS rule 2, 5, 8, or 9
     above says otherwise for that specific question.
 11. Never touch coverage/limit/deductible dropdowns (Coverage A/B/C/D,
     liability limits, medical payments, deductible percentages) even if
     they appear on this page — those are out of scope for this prompt.

NOTE ON FIELD ORDER: question index/position shifts by state and quote
variant. Always match rules by LABEL TEXT, never by field_id index/position.

If a question doesn't clearly map to any rule above, omit it — do not guess."""


def _uw_label_match(label: str, *needles: str) -> bool:
    lab = (label or "").strip().casefold()
    return any(n in lab for n in needles)


def _uw_fallback_answer(question: dict, contact: dict) -> str | None:
    """Local rule-matching fallback, used when Gemini is unavailable or
    returns something unusable. Mirrors _UW_SYSTEM_PROMPT's RULES, keyed off
    label text (never field_id index, since that shifts by state)."""
    label = question.get("label") or ""
    field_type = question.get("field_type")
    options = {opt["value"] for opt in (question.get("options") or [])}

    def _pick(value: str) -> str | None:
        return value if not options or value in options else None

    if _uw_label_match(label, "named insured type"):
        return _pick("Individual")
    if _uw_label_match(label, "own a residential property"):
        return _pick("True")
    if _uw_label_match(label, "company car"):
        return _pick("False")
    if _uw_label_match(label, "affinity group"):
        return None
    if _uw_label_match(label, "losses in the previous 5 years", "losses in the last 5 years"):
        return _pick("False")
    if _uw_label_match(label, "continuous insurance", "uninterrupted"):
        years = _contact_first(contact, "years_continuous_ins", "yearsContinuousPropertyInsurance", "continuous_insurance_years")
        return years or "4"
    if _uw_label_match(label, "prior auto carrier"):
        return "4"
    if _uw_label_match(label, "site access", "easy access", "access roads"):
        for opt in (question.get("options") or []):
            if "flat" in (opt.get("label") or "").casefold() or "easy" in (opt.get("value") or "").casefold():
                return opt["value"]
        return None
    if _uw_label_match(label, "paperless", "electronic"):
        return _pick("True")
    if field_type == "select":
        return _pick("False")
    return None


_UW_BLANK_SENTINELS = {"", "-1", "-- select --"}


def _uw_already_answered(question: dict) -> bool:
    current = (question.get("current_value") or "").strip()
    return current.casefold() not in _UW_BLANK_SENTINELS


async def _scrape_uw_questions(page: Page, id_prefix: str) -> list[dict]:
    """Scrape every underwriting question whose field id starts with
    id_prefix: field id, type (select/text), displayed question number,
    label text, current value, and — for selects — the available options.

    Queries the whole document by id-prefix rather than scoping to a
    container element, since the ASP.NET Repeater these questions live in
    (rpParentQuestions) doesn't render its own wrapper element in the DOM."""
    return await page.evaluate(
        """(idPrefix) => {
            const out = [];
            document.querySelectorAll(
                `select[id^="${idPrefix}ddlAnswer_"], input[type="text"][id^="${idPrefix}txtAnswer_"]`
            ).forEach((el) => {
                const label = document.querySelector(`label[for="${el.id}"]`);
                const labelText = label ? label.textContent.replace(/\\s+/g, ' ').trim() : '';
                const numMatch = labelText.match(/^(\\d+):/);
                const entry = {
                    field_id: el.id,
                    field_type: el.tagName.toLowerCase() === 'select' ? 'select' : 'text',
                    question_num: numMatch ? numMatch[1] : '',
                    label: labelText.replace(/^\\d+:\\s*/, ''),
                    disabled: !!el.disabled,
                    current_value: (el.value || '').trim(),
                    options: null,
                };
                if (entry.field_type === 'select') {
                    entry.options = Array.from(el.options).map(o => ({value: o.value, label: o.text.trim()}));
                }
                out.push(entry);
            });
            return out;
        }""",
        id_prefix,
    )


def _uw_answer_locally(questions: list[dict], contact: dict) -> dict:
    answers: dict[str, str] = {}
    for q in questions:
        if q.get("disabled") or _uw_already_answered(q):
            continue
        val = _uw_fallback_answer(q, contact)
        if val is not None:
            answers[q["field_id"]] = val
    return answers


def _gemini_answer_uw_sync(questions: list[dict], contact: dict) -> dict:
    # Only ask about questions that are actually blank — never re-decide
    # something the portal (or a prior run) already answered.
    pending = [q for q in questions if not q.get("disabled") and not _uw_already_answered(q)]
    if not pending:
        return {}

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        log("WARN", "GEMINI_API_KEY not set; using local underwriting question matcher")
        return _uw_answer_locally(pending, contact)

    try:
        import google.generativeai as genai
    except ImportError as exc:
        log("WARN", f"google-generativeai not installed ({exc}); using local matcher")
        return _uw_answer_locally(pending, contact)

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(_GEMINI_UW_MODEL)

    contact_data = {
        "years_continuous_ins": _contact_first(
            contact, "years_continuous_ins", "yearsContinuousPropertyInsurance", "continuous_insurance_years"
        ),
    }
    prompt = (
        f"{_UW_SYSTEM_PROMPT}\n\n"
        f"CONTACT_DATA: {json.dumps(contact_data)}\n\n"
        f"Questions on this page: "
        f"{json.dumps([{k: v for k, v in q.items() if k not in ('disabled', 'current_value')} for q in pending])}\n"
    )

    try:
        response = model.generate_content(prompt)
        text = getattr(response, "text", None) or ""
        raw = text.strip()
        fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw, re.I)
        if fence:
            raw = fence.group(1).strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start:end + 1]
        answers = json.loads(raw)
        if not isinstance(answers, dict):
            raise ValueError("Gemini JSON root must be an object")
        answers = {k: str(v) for k, v in answers.items()}
        log("INFO", f"Gemini answered {len(answers)}/{len(pending)} pending underwriting question(s)")
    except Exception as exc:
        log("WARN", f"Gemini underwriting answer failed ({exc}); using local matcher")
        return _uw_answer_locally(pending, contact)

    valid_ids = {q["field_id"] for q in pending}
    return {fid: val for fid, val in answers.items() if fid in valid_ids}


async def _ai_answer_underwriting_questions(questions: list[dict], contact: dict) -> dict:
    if not questions:
        return {}
    return await asyncio.to_thread(_gemini_answer_uw_sync, questions, contact)


async def _apply_uw_answers(page: Page, questions: list[dict], answers: dict) -> None:
    by_id = {q["field_id"]: q for q in questions}

    for q in questions:
        if _uw_already_answered(q):
            log("INFO", f"UW already answered — skipping [{q.get('question_num')}] {q.get('label')!r} "
                        f"(current: {q.get('current_value')!r})")

    for field_id, value in answers.items():
        question = by_id.get(field_id)
        if not question:
            continue
        selector = f"#{field_id}"
        try:
            if question["field_type"] == "select":
                await page.select_option(selector, value, timeout=5000)
            else:
                await page.fill(selector, value)
            log("INFO", f"UW answered [{question.get('question_num')}] {question.get('label')!r} -> {value!r}")
        except Exception as exc:
            log("WARN", f"UW answer failed for {field_id} ({question.get('label')!r}): {exc}")
        await asyncio.sleep(random.uniform(6, 10))

    unanswered = [
        q for q in questions
        if not q.get("disabled") and not _uw_already_answered(q) and q["field_id"] not in answers
    ]
    for q in unanswered:
        log("WARN", f"UW question not answered — needs review: [{q.get('question_num')}] {q.get('label')!r}")


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


# ── PAGE 15: PREMIUM SUMMARY HELPERS ──────────────────────────────────────────

_P15_TERM = "12"
_P15_PAY_METHOD = "AS"
_P15_PAY_PLAN = "Installments"
_P15_DRAFT_DAY = "15"
_P15_TERM_SEL = "#MainContent_ucPaymentMethod_ddlTerm"
_P15_PAY_METHOD_SEL = "#MainContent_ucPaymentMethod_ddlPayMethod"
_P15_PAY_PLAN_SEL = "#MainContent_ucPaymentMethod_ddlPayPlan"
_P15_DRAFT_DAY_SEL = "#MainContent_ucPaymentMethod_ddlDraftDay"
_P15_RERATE_BTN = "#MainContent_ucRater_btnRate"
_P15_RATES_TABLE = "#MainContent_ucRater_gvRates"
_P15_QUOTE_PROPOSAL_BTN = "#MainContent_btnQuoteProposal"
_P15_POPUP_TIMEOUT_MS = 180000


def _p15_fmt(raw: str) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    return text if text.startswith("$") else f"${text}"


def _p15_parse_rates(rows: list[tuple[str, str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for label, amount in rows:
        lab = (label or "").strip().casefold()
        formatted = _p15_fmt(amount)
        if not formatted:
            continue
        if lab == "auto premium":
            out["auto_premium"] = formatted
        elif lab == "home premium":
            out["home_premium"] = formatted
        elif lab == "total":
            out["premium"] = formatted
    return out


async def _p15_wait_processing(page: Page, timeout_ms: int = 60000) -> None:
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


async def _p15_select_if_needed(page: Page, selector: str, value: str, *, retries: int = 3) -> None:
    loc = page.locator(selector).first
    if await loc.count() == 0:
        log("WARN", f"P15 dropdown not found: {selector}")
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
                await _p15_wait_processing(page)
                return
            log("WARN", f"{selector}: selected but value is {actual!r} not {value!r} — retrying ({attempt}/{retries})")
        except Exception as exc:
            last_exc = exc
            log("WARN", f"{selector}: attempt {attempt}/{retries} failed: {exc}")
        if attempt < retries:
            await page.wait_for_timeout(1500)
    log("WARN", f"Could not set {selector} to {value!r} after {retries} attempts: {last_exc}")
    await _p15_wait_processing(page)


async def _p15_wait_ready(page: Page) -> None:
    await page.wait_for_selector(
        f"{_P15_RERATE_BTN}, {_P15_PAY_METHOD_SEL}",
        state="attached",
        timeout=30000,
    )
    await _p15_wait_processing(page)


async def _p15_wait_payment_ready(page: Page) -> None:
    await page.wait_for_selector(
        f"{_P15_PAY_METHOD_SEL}, {_P15_PAY_PLAN_SEL}",
        state="attached",
        timeout=30000,
    )
    for sel in (_P15_TERM_SEL, _P15_DRAFT_DAY_SEL):
        try:
            await page.wait_for_selector(sel, state="attached", timeout=15000)
        except Exception:
            pass
    await _p15_wait_processing(page)


async def _p15_select_term_if_needed(page: Page) -> None:
    for sel in (_P15_TERM_SEL, "#MainContent_ddlTerm"):
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
            (_P15_TERM, None),
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
                await _p15_wait_processing(page)
                return
            except Exception:
                continue
        log("WARN", f"Could not set policy term on {sel}")
        return


async def _p15_set_payment_options(page: Page) -> None:
    log("INFO", "Setting P15 payment options (term, pay method, plan, withdrawal day)")
    await _p15_select_term_if_needed(page)
    await _p15_select_if_needed(page, _P15_PAY_METHOD_SEL, _P15_PAY_METHOD)
    await _p15_select_if_needed(page, _P15_PAY_PLAN_SEL, _P15_PAY_PLAN)
    await _p15_select_if_needed(page, _P15_DRAFT_DAY_SEL, _P15_DRAFT_DAY)
    await _p15_wait_processing(page)


async def _p15_wait_rated(page: Page, timeout_ms: int = 90000) -> None:
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


async def _p15_click_rerate(page: Page) -> bool:
    rerate = page.locator(_P15_RERATE_BTN).first
    if await rerate.count() == 0:
        log("WARN", "Re-Rate button not found in DOM")
        return False

    for panel_sel in ("#MainContent_ucRater_pnlRate", _P15_RATES_TABLE, _P15_RERATE_BTN):
        panel = page.locator(panel_sel).first
        if await panel.count() > 0:
            try:
                await panel.scroll_into_view_if_needed(timeout=8000)
            except Exception:
                pass

    log("INFO", "Clicking Re-Rate on Premium Summary")
    clicked = False
    for attempt, (label, fn) in enumerate(
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

    await _p15_wait_processing(page, timeout_ms=90000)
    try:
        await _p15_wait_rated(page, timeout_ms=90000)
        log("INFO", "Premium Summary rated — rate table ready")
    except Exception:
        log("WARN", "Timed out waiting for rated Total row after Re-Rate")
    return True


async def _p15_extract_rates(page: Page) -> dict[str, str]:
    rates: dict[str, str] = {}
    table = page.locator(_P15_RATES_TABLE)
    if await table.count() > 0:
        rows = await table.locator("tr").evaluate_all(
            """rows => rows.map(r => {
                const cells = Array.from(r.querySelectorAll('td')).map(c => (c.innerText || '').trim());
                if (cells.length >= 2) return [cells[0], cells[1]];
                return null;
            }).filter(Boolean)"""
        )
        rates = _p15_parse_rates([(r[0], r[1]) for r in rows if r])

    if not rates.get("auto_premium"):
        loc = page.locator("#MainContent_ucPPASummary_lblDisplayVehiclePrem").first
        if await loc.count() > 0:
            text = _p15_fmt(await loc.inner_text())
            if text:
                rates["auto_premium"] = text
    if not rates.get("home_premium"):
        loc = page.locator("#MainContent_ucHODFSummary_lblDisplayTotalLocationPremium").first
        if await loc.count() > 0:
            text = _p15_fmt(await loc.inner_text())
            if text:
                rates["home_premium"] = text

    if rates.get("auto_premium") or rates.get("home_premium"):
        log(
            "INFO",
            f"P15 rates: auto={rates.get('auto_premium')!r} "
            f"home={rates.get('home_premium')!r} total={rates.get('premium')!r}",
        )
    return rates


def _p15_is_pdf_bytes(body: bytes | None) -> bool:
    return bool(body) and len(body) >= 4 and body[:4] == b"%PDF"


def _p15_is_pdf_candidate_response(response) -> bool:
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


def _p15_contact_name_slug(contact: dict | None) -> str:
    first = _contact_first(contact or {}, "firstName", "first_name") or ""
    last = _contact_first(contact or {}, "lastName", "last_name") or ""
    combined = f"{first} {last}".strip()
    if not combined:
        return "unknown"
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", combined).strip("_")
    return slug[:120] if slug else "unknown"


def _p15_pdf_filename(name_slug: str, quote_num: str, ts: str, suffix: str = "") -> str:
    parts = [name_slug, "quote_proposal"]
    if quote_num and quote_num != "unknown":
        parts.append(quote_num)
    parts.append(ts)
    if suffix:
        parts.append(suffix)
    return "_".join(parts) + ".pdf"


async def _p15_save_pdf_bytes(
    pdf_dir: Path, name_slug: str, quote_num: str, ts: str, body: bytes, suffix: str = ""
) -> str:
    path = pdf_dir / _p15_pdf_filename(name_slug, quote_num, ts, suffix)
    await asyncio.to_thread(path.write_bytes, body)
    log("OK", f"Saved Quote Proposal PDF -> {path}")
    return str(path)


async def _p15_fetch_pdf_from_url(page: Page, url: str) -> bytes | None:
    url = (url or "").strip()
    if not url or url == "about:blank" or url.lower().startswith(("chrome://", "devtools://")):
        return None
    try:
        resp = await page.context.request.get(url, timeout=90000)
        if resp.ok:
            body = await resp.body()
            if _p15_is_pdf_bytes(body):
                return body
    except Exception as exc:
        log("DEBUG", f"PDF URL fetch failed ({url[:100]}): {exc}")
    return None


async def _p15_fetch_pdf_from_popup(popup: Page) -> bytes | None:
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
                    if (bytes[0] !== 0x25 || bytes[1] !== 0x50 || bytes[2] !== 0x44 || bytes[3] !== 0x46) return null;
                    return Array.from(bytes);
                } catch (e) { return null; }
            }"""
        )
        if data:
            body = bytes(data)
            if _p15_is_pdf_bytes(body):
                return body
    except Exception as exc:
        log("DEBUG", f"Popup in-page PDF fetch failed: {exc}")
    return None


async def _p15_body_from_pdf_response(response) -> bytes | None:
    if not _p15_is_pdf_candidate_response(response):
        return None
    try:
        body = await response.body()
        return body if _p15_is_pdf_bytes(body) else None
    except Exception:
        return None


async def _p15_extract_embed_pdf_url(popup: Page) -> str | None:
    try:
        href = await popup.evaluate(
            """() => {
                const pick = (el) => (el && (el.src || el.data || el.getAttribute('data'))) || '';
                const embed = document.querySelector('embed[type="application/pdf"], embed');
                if (embed) { const u = pick(embed); if (u) return u; }
                const obj = document.querySelector('object[type="application/pdf"], object');
                if (obj) { const u = pick(obj); if (u) return u; }
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


def _p15_is_quote_proposal_popup(candidate: Page, main_page: Page) -> bool:
    if candidate == main_page:
        return False
    url = (candidate.url or "").strip().lower()
    if not url or url == "about:blank":
        return False
    if "displaypdf.aspx" in url:
        return True
    return "iid=" in url and "natgenagency.com" in url


async def _p15_wait_for_popup(
    context: BrowserContext, main_page: Page, timeout_ms: int = _P15_POPUP_TIMEOUT_MS
) -> Page | None:
    log("INFO", f"Waiting for Quote Proposal popup (max {timeout_ms // 1000}s)...")
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    last_log = 0.0

    while time.monotonic() < deadline:
        for candidate in context.pages:
            if _p15_is_quote_proposal_popup(candidate, main_page):
                try:
                    await candidate.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                log("INFO", f"Quote Proposal popup detected: {(candidate.url or '')[:120]}")
                return candidate

        remaining_ms = int(max(500, (deadline - time.monotonic()) * 1000))
        try:
            new_page = await context.wait_for_event("page", timeout=min(3000, remaining_ms))
            if _p15_is_quote_proposal_popup(new_page, main_page):
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

        await asyncio.sleep(random.uniform(6, 10))

    log("WARN", f"Quote Proposal popup did not appear within {timeout_ms // 1000}s")
    return None


async def _p15_wait_popup_pdf_ready(popup: Page, pdf_responses: list, timeout_ms: int = 60000) -> bool:
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        for resp in pdf_responses:
            if _p15_is_pdf_candidate_response(resp):
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


async def _p15_download_from_popup(
    page: Page, popup: Page, pdf_responses: list
) -> tuple[bytes | None, str]:
    for resp in list(pdf_responses):
        body = await _p15_body_from_pdf_response(resp)
        if body:
            log("INFO", f"PDF from captured network response: {(resp.url or '')[:120]}")
            return body, "popup_response"

    popup_url = (popup.url or "").strip()
    if popup_url:
        log("INFO", f"Quote Proposal popup URL: {popup_url[:120]}")

    if popup_url.startswith(("http://", "https://")):
        body = await _p15_fetch_pdf_from_url(page, popup_url)
        if body:
            return body, "displaypdf_get"

    embed_url = await _p15_extract_embed_pdf_url(popup)
    if embed_url:
        log("INFO", f"Trying embed PDF URL: {embed_url[:120]}")
        body = await _p15_fetch_pdf_from_url(page, embed_url)
        if body:
            return body, "embed_src"

    body = await _p15_fetch_pdf_from_popup(popup)
    if body:
        return body, "popup_fetch"

    return None, ""


async def _p15_wait_and_save_popup_pdf(
    page: Page, popup: Page, pdf_dir: Path, name_slug: str, quote_num: str, ts: str,
    pdf_responses: list, on_response,
) -> str | None:
    popup.on("response", on_response)
    try:
        try:
            await popup.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception:
            pass

        log("INFO", "Waiting for PDF to load in Quote Proposal popup...")
        pdf_ready = await _p15_wait_popup_pdf_ready(popup, pdf_responses, timeout_ms=90000)
        if not pdf_ready:
            log("WARN", "PDF viewer not confirmed ready; attempting download anyway")

        log("INFO", "Starting Quote Proposal PDF download")
        for attempt in range(1, 11):
            body, strategy = await _p15_download_from_popup(page, popup, pdf_responses)
            if body:
                suffix = strategy or "popup"
                log("INFO", f"Quote Proposal PDF download strategy: {suffix}")
                return await _p15_save_pdf_bytes(pdf_dir, name_slug, quote_num, ts, body, suffix)
            if attempt < 10:
                log("INFO", f"PDF not saved yet, retrying ({attempt}/10)...")
                await popup.wait_for_timeout(2000)

        log("WARN", "Could not save PDF automatically after 10 attempts")
        return None
    finally:
        try:
            popup.remove_listener("response", on_response)
        except Exception:
            pass


async def _p15_click_quote_proposal(page: Page, btn) -> None:
    await _p15_wait_processing(page)
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
        await page.evaluate("document.getElementById('MainContent_btnQuoteProposal')?.click()")
        log("INFO", "Quote Proposal click sent (JS fallback)")
    except Exception as exc:
        log("WARN", f"Quote Proposal JS click failed: {exc}")


async def _p15_download_quote_proposal(page: Page, contact: dict | None = None) -> str | None:
    btn = page.locator(_P15_QUOTE_PROPOSAL_BTN).first
    if await btn.count() == 0:
        log("WARN", "Quote Proposal button not found on Premium Summary")
        return None

    pdf_dir = ARTIFACTS_DIR / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    name_slug = _p15_contact_name_slug(contact)
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
            if _p15_is_pdf_candidate_response(response):
                pdf_responses.append(response)
        except Exception:
            pass

    page.context.on("response", _on_response)

    popup = None
    try:
        await _p15_click_quote_proposal(page, btn)
        popup = await _p15_wait_for_popup(page.context, page, timeout_ms=_P15_POPUP_TIMEOUT_MS)

        if popup is not None:
            path = await _p15_wait_and_save_popup_pdf(
                page, popup, pdf_dir, name_slug, quote_num, ts, pdf_responses, _on_response,
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
            await _p15_click_quote_proposal(page, btn)
        download = await dl_info.value
        suggested = (download.suggested_filename or "quote_proposal.pdf").strip()
        if not suggested.lower().endswith(".pdf"):
            suggested = f"{suggested}.pdf"
        path = pdf_dir / _p15_pdf_filename(name_slug, quote_num, ts, suggested)
        await download.save_as(str(path))
        log("OK", f"Downloaded Quote Proposal PDF -> {path}")
        return str(path)
    except Exception as exc:
        log("WARN", f"Quote Proposal direct download failed: {exc}")

    return None


# ── END PAGE 15 HELPERS ────────────────────────────────────────────────────────


async def full_flow(page: Page, context: BrowserContext, username: str, password: str, otp_sender: str, contact: dict, agent_id: str) -> dict:

    # iMessage DB check
    db_path = Path.home() / "Library" / "Messages" / "chat.db"
    if not db_path.exists():
        log("WARN", f"Messages DB not found: {db_path}")

    # --- LOGIN ---
    await page.goto("https://natgenagency.com/Login.aspx")
    await page.wait_for_load_state("domcontentloaded")

    enable_login_btn = page.locator("#btnEnableLogin")
    if await enable_login_btn.count() > 0:
        log("INFO", "Enable Login button found — clicking it")
        await enable_login_btn.click()
        await page.wait_for_load_state("domcontentloaded")

    if "MainMenu.aspx" not in page.url:
        await page.wait_for_selector("#txtUserID", state="visible", timeout=30000)
        await page.fill("#txtUserID", username)
        await page.click("#btnLogin")

        if "MainMenu.aspx" not in page.url:
            await asyncio.sleep(5)
            await page.wait_for_selector("#Password", state="visible")
            await page.fill("#Password", password)
            await page.click("button[type='submit']")

            if "MainMenu.aspx" not in page.url:
                await page.click("#loginWith2faSms")
                await asyncio.sleep(30)
                code = _retrieve_2fa_code_from_imessage(otp_sender=otp_sender)
                await page.fill("#TwoFactorCode", code)
                await page.click("#verifyButton")
                await page.wait_for_url("**/MainMenu.aspx")

    await context.storage_state(path=str(STORAGE_STATE_PATH))
    log("INFO", "Login complete — on Main Menu")
    await asyncio.sleep(random.uniform(12, 22))

    # --- PAGE 2: STATE + PRODUCT SELECT (MainMenu.aspx) ---
    _quote_state = (contact.get("state") or "GA").strip().upper()
    await page.locator("xpath=/html/body/div[1]/div/form/div[3]/table[2]/tbody/tr/td/div/div[1]/div[3]/div[2]/select[1]").select_option(_quote_state)
    await asyncio.sleep(random.uniform(6, 10))
    if _quote_state == "GA":
        await page.locator("xpath=/html/body/div[1]/div/form/div[3]/table[2]/tbody/tr/td/div/div[1]/div[3]/div[2]/select[2]").select_option("Custom360 Package 2.0")
    if _quote_state == "SC" or _quote_state == "TN":
        await page.locator("xpath=/html/body/div[1]/div/form/div[3]/table[2]/tbody/tr/td/div/div[1]/div[3]/div[2]/select[2]").select_option("Custom360 Package")
    await asyncio.sleep(random.uniform(6, 10))
    await page.locator("xpath=/html/body/div[1]/div/form/div[3]/table[2]/tbody/tr/td/div/div[1]/div[3]/div[2]/span").click()
    await page.wait_for_url("**/ClientSearch*", timeout=30000)
    log("INFO", "On Client Search page")
    await asyncio.sleep(random.uniform(12, 22))

    # --- PAGE 3: CLIENT SEARCH + ADD NEW ---
    await page.fill("#MainContent_txtFirstName", _val(contact, "firstName", "first_name"))
    await asyncio.sleep(random.uniform(6, 10))
    await page.fill("#MainContent_txtLastName", _val(contact, "lastName", "last_name"))
    await asyncio.sleep(random.uniform(6, 10))
    await page.fill("#MainContent_txtZipCode", _val(contact, "postalCode", "zip"))
    await asyncio.sleep(random.uniform(6, 10))
    await page.click("#MainContent_btnSearch")
    await page.wait_for_load_state("networkidle")
    await page.wait_for_selector("#MainContent_btnAddNewClient", state="visible")
    await page.click("#MainContent_btnAddNewClient")
    await page.wait_for_url("**/ClientInfo*", timeout=30000)
    log("INFO", "On Client Info P1 page")
    await asyncio.sleep(random.uniform(12, 22))

    # --- PAGE 4: CLIENT INFO P1 ---
    dob = _date(_val(contact, "dateOfBirth", "date_of_birth"))
    area, prefix, line = split_phone_number(_val(contact, "phone"))
    email = _val(contact, "email")

    await page.fill("#MainContent_ucNamedInsured_txtDateOfBirth", dob)
    await asyncio.sleep(random.uniform(6, 10))
    await page.select_option("#MainContent_ucNamedInsured_ddlGender", _val(contact, "gender"))
    await asyncio.sleep(random.uniform(6, 10))
    await page.select_option("#MainContent_ucNamedInsured_ddlMaritalStatus", _val(contact, "maritalStatus", "marital_status"))
    await asyncio.sleep(random.uniform(6, 10))
    await page.select_option("#MainContent_ucNamedInsured_ddlOccupation", "Other")
    await asyncio.sleep(random.uniform(6, 10))
    await page.select_option("#MainContent_ucContactInfo_ucPhoneNumber_ddlPhoneType", "Cell")
    await asyncio.sleep(random.uniform(6, 10))
    await page.fill("#MainContent_ucContactInfo_ucPhoneNumber_txtAreaCode", area)
    await asyncio.sleep(random.uniform(6, 10))
    await page.fill("#MainContent_ucContactInfo_ucPhoneNumber_txtPrefix", prefix)
    await asyncio.sleep(random.uniform(6, 10))
    await page.fill("#MainContent_ucContactInfo_ucPhoneNumber_txtLineNumber", line)
    await asyncio.sleep(random.uniform(6, 10))
    await page.fill("#MainContent_ucResidentialAddress_txtAddress", _val(contact, "address1", "address"))
    await asyncio.sleep(random.uniform(6, 10))
    await page.fill("#MainContent_ucResidentialAddress_txtCity", _val(contact, "city"))
    await asyncio.sleep(random.uniform(6, 10))
    await page.select_option("#MainContent_ddlYearsAtAddress", "3")
    await asyncio.sleep(random.uniform(6, 10))
    await page.select_option("#MainContent_ucContactInfo_ucEmailAddress_ddlEmailOption", "Provided")
    await asyncio.sleep(random.uniform(6, 10))
    await page.fill("#MainContent_ucContactInfo_ucEmailAddress_txtEmailAddress", email)
    await asyncio.sleep(random.uniform(6, 10))
    await page.fill("#MainContent_ucContactInfo_ucEmailAddress_txtEmailAddressConfirmation", email)
    await asyncio.sleep(10)
    if _quote_state == "SC" or  _quote_state == "TN":
        ddl_automated = page.locator(
            "#MainContent_ucContactInfo_ddlAutomatedContact, "
            "[name='ctl00$MainContent$ucContactInfo$ddlAutomatedContact']"
        ).first
        if await ddl_automated.count() > 0:
            try:
                await ddl_automated.click()
                await ddl_automated.select_option(value="True")
            except Exception:
                pass
            await page.evaluate(
                """() => {
                    const el = document.getElementById('MainContent_ucContactInfo_ddlAutomatedContact')
                        || document.querySelector("[name='ctl00$MainContent$ucContactInfo$ddlAutomatedContact']");
                    if (!el) return;
                    el.value = 'True';
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }"""
            )
            await asyncio.sleep(random.uniform(6, 10))
    await page.click("#MainContent_btnContinue")
    await page.wait_for_load_state("networkidle")
    log("INFO", "On Client Info P2 page")
    await asyncio.sleep(random.uniform(12, 22))

    # --- PAGE 5: CLIENT INFO P2 ---
    await page.select_option("#MainContent_ucGeneralInformation_ddlInputBy", agent_id)
    await asyncio.sleep(random.uniform(6, 10))
    await page.click("#MainContent_btnContinue")
    await page.wait_for_url("**/Prefill*", timeout=30000)
    log("INFO", "On Prefill page")
    await asyncio.sleep(random.uniform(12, 22))

    # --- PAGE 6: PREFILL ---
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

        license_cbs = page.locator("#gvPrefillDriver input[type='checkbox'][id*='License' i], input[type='checkbox'][id*='chkLicense' i]")
        for i in range(await license_cbs.count()):
            cb = license_cbs.nth(i)
            try:
                if not await cb.is_checked():
                    await cb.check(timeout=5000)
            except Exception:
                pass

        if await page.locator("#btnAcceptAllAutos").count() > 0:
            await page.click("#btnAcceptAllAutos")
            await page.wait_for_load_state("networkidle")

        auto_accept = page.locator("#gvPrefillAuto input[type='radio'][name*='rbAccept']")
        for i in range(await auto_accept.count()):
            if not await auto_accept.nth(i).is_checked():
                await auto_accept.nth(i).check()

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

        await asyncio.sleep(2)
    except PlaywrightTimeoutError:
        log("WARN", "No prefill table found — skipping")

    await page.click("#MainContent_btnContinue")
    await page.wait_for_load_state("networkidle")
    log("INFO", "On Property Info page")
    await asyncio.sleep(random.uniform(12, 22))

    # --- PAGE 7: PROPERTY INFO ---
    yb_raw = _contact_first(contact, "year_built", "yearBuilt")
    yb_val = str(yb_raw).strip() if yb_raw else ""
    if yb_val:
        await page.fill("#MainContent_txtYearBuilt", yb_val)
    await asyncio.sleep(random.uniform(6, 10))

    # Roof renovation year — must not be older than year built, else portal gives S1130 warning
    roof_raw = _contact_first(contact, "year_roof_renovation", "roofRenovationYear", "roof_year", "roofYear")
    if roof_raw:
        roof_val = str(roof_raw).strip()
    else:
        roof_val = str(datetime.now().year - 5)
    yb_int = int(yb_val) if str(yb_val).isdigit() else datetime.now().year
    if not str(roof_val).isdigit() or int(roof_val) < yb_int:
        roof_val = yb_val
    if await page.locator("#MainContent_txtYearRoofRenovation").count() > 0:
        cur = (await page.locator("#MainContent_txtYearRoofRenovation").first.input_value()).strip()
        if not cur:
            await page.fill("#MainContent_txtYearRoofRenovation", roof_val)
            await asyncio.sleep(random.uniform(6, 10))

    await page.select_option("#MainContent_ddlForm", "HO3")
    await asyncio.sleep(random.uniform(6, 10))
    await page.select_option("#MainContent_ddlResidenceClass", "Primary")
    await asyncio.sleep(random.uniform(6, 10))
    await page.select_option("#MainContent_ddlOccupancy2", "OwnerOccupied")
    await asyncio.sleep(random.uniform(6, 10))
    await page.select_option("#MainContent_ddlType", "Dwelling")
    await asyncio.sleep(random.uniform(6, 10))
    await page.select_option("#MainContent_ddlNumberOfFamilies", "1")
    await asyncio.sleep(random.uniform(6, 10))
    await page.select_option("#MainContent_ddlConstruction", "Frame")
    await asyncio.sleep(random.uniform(6, 10))
    await page.select_option("#MainContent_ddlNamedInsuredType", "Owner")
    await asyncio.sleep(random.uniform(6, 10))

    dp_raw = _contact_first(contact, "datePurchased", "date_purchased")
    dp_val = _date(str(dp_raw)) if dp_raw else _date("8/1/2023")
    await page.fill("#MainContent_txtDatePurchased", dp_val)
    await asyncio.sleep(random.uniform(6, 10))

    sq_raw = _contact_first(contact, "square_footage", "squareFootage", "sqft")
    sq_val = str(sq_raw).strip() if sq_raw else ""
    if sq_val:
        await page.fill("#MainContent_txtSquareFootage", sq_val)
    await asyncio.sleep(random.uniform(6, 10))

    await page.select_option("#MainContent_ddlNumberOfStories", "2")
    await asyncio.sleep(random.uniform(6, 10))
    await page.select_option("#MainContent_ddlRoofType", "AS")
    await asyncio.sleep(random.uniform(6, 10))

    roof_raw = _contact_first(contact, "year_roof_renovation", "roofRenovationYear", "yearRoofRenovation")
    roof_val = roof_raw if roof_raw else str(datetime.now().year - 5)
    await page.fill("#MainContent_txtYearRoofRenovation", roof_val)
    await asyncio.sleep(random.uniform(6, 10))

    await page.select_option("#MainContent_ddlPrimaryHeat", "Electric")
    await asyncio.sleep(random.uniform(6, 10))
    await page.select_option("#MainContent_ddlSecondaryHeat", "None")
    await asyncio.sleep(random.uniform(6, 10))
    await page.select_option("#MainContent_ddlStoveOnPremise", "False")
    await asyncio.sleep(random.uniform(6, 10))
    await page.select_option("#MainContent_ddlRoofShape", "Gable, Slight Pitch")
    await asyncio.sleep(random.uniform(6, 10))
    await page.select_option("#MainContent_ddlRoofHail", "False")
    await asyncio.sleep(random.uniform(6, 10))
    await page.select_option("#MainContent_ddlPropertyUnderConstruction", "False")
    await asyncio.sleep(random.uniform(6, 10))
    await page.select_option("#MainContent_ddlSmokeDetectorsFireExtinguishers", "B")
    await asyncio.sleep(random.uniform(6, 10))
    await page.select_option("#MainContent_ddlDeadBoltLocks", "True")
    await asyncio.sleep(random.uniform(6, 10))
    await page.select_option("#MainContent_ddlBurglarAlarm", "L")
    await asyncio.sleep(random.uniform(6, 10))
    await page.select_option("#MainContent_ddlFireAlarmSystem", "N")
    await asyncio.sleep(random.uniform(6, 10))
    await page.select_option("#MainContent_ddlSprinklerSystem", "N")
    await asyncio.sleep(random.uniform(6, 10))
    await page.select_option("#MainContent_ddlWaterShutoffSystem", "False")
    await asyncio.sleep(random.uniform(6, 10))
    await page.select_option("#MainContent_ddlOilTank", "None")
    await asyncio.sleep(random.uniform(6, 10))

    eff_raw = _contact_first(contact, "effective_date", "effectiveDate")
    eff_val = _date(str(eff_raw)) if eff_raw else (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%m/%d/%Y")
    if await page.locator("#MainContent_txtEffectiveDate").count() > 0:
        await page.fill("#MainContent_txtEffectiveDate", eff_val)
        await asyncio.sleep(random.uniform(6, 10))

    # Mortgagee-Bill additional interest
    already_saved = False
    for sel in (
        "#MainContent_uc1PropertyAI_gvInterests tbody tr",
        "#MainContent_gvAdditionalInterests tbody tr",
        "#MainContent_uc1PropertyAI_gvSavedInterests tbody tr",
    ):
        if await page.locator(sel).count() > 0:
            already_saved = True
            break

    if not already_saved and await page.locator("#MainContent_uc1PropertyAI_btnAddInterest").count() > 0:
        await page.click("#MainContent_uc1PropertyAI_btnAddInterest")
        await page.wait_for_load_state("networkidle")

        # Select interest type (Mortgagee-Bill) — triggers postback to show search panel
        ddl_type = page.locator("#MainContent_uc1PropertyAI_ddlInterestType").first
        if await ddl_type.count() > 0:
            for val in ("Mortgagee-Bill", "MortgageeBill", "Mortgagee"):
                try:
                    await ddl_type.select_option(value=val, timeout=5000)
                    name_attr = await ddl_type.get_attribute("name")
                    if name_attr:
                        await page.evaluate("n => { if (typeof __doPostBack === 'function') __doPostBack(n, ''); }", name_attr)
                    await page.wait_for_load_state("networkidle")
                    break
                except Exception:
                    continue
            await asyncio.sleep(random.uniform(6, 10))

        # Check if search panel appeared
        search_name = page.locator("#MainContent_uc1PropertyAI_txtSearchName").first
        interest_name = page.locator("#MainContent_uc1PropertyAI_txtInterestName").first

        if await search_name.count() > 0 and await search_name.is_visible():
            # Search flow
            await search_name.fill("test")
            await asyncio.sleep(random.uniform(6, 10))
            await page.click("#MainContent_uc1PropertyAI_btnSearch")
            await page.wait_for_load_state("networkidle")

            grid = page.locator("#MainContent_uc1PropertyAI_gvMortgageCompany")
            if await grid.count() > 0:
                await grid.wait_for(state="visible", timeout=15000)
                await grid.locator("tbody a").filter(has_text="Select").first.click()
                await page.wait_for_load_state("networkidle")
            else:
                # No results — fall through to manual form
                pass

        # If we're on the manual View/Edit form, fill required fields
        manual_name = page.locator("#MainContent_uc1PropertyAI_txtInterestName").first
        if await manual_name.count() > 0 and await manual_name.is_visible():
            await manual_name.fill("Unknown Mortgagee")
            await asyncio.sleep(random.uniform(6, 10))
            addr_field = page.locator("#MainContent_uc1PropertyAI_txtStreetAddress1").first
            if await addr_field.count() > 0:
                await addr_field.fill("123 Main St")
                await asyncio.sleep(random.uniform(6, 10))
            city_field = page.locator("#MainContent_uc1PropertyAI_txtCity").first
            if await city_field.count() > 0:
                await city_field.fill("Atlanta")
                await asyncio.sleep(random.uniform(6, 10))
            state_field = page.locator("#MainContent_uc1PropertyAI_ddlState").first
            if await state_field.count() > 0:
                await state_field.select_option("GA")
                await asyncio.sleep(random.uniform(6, 10))
            zip_field = page.locator("#MainContent_uc1PropertyAI_txtZip").first
            if await zip_field.count() > 0:
                zip_val = _contact_first(contact, "postalCode", "zip") or "30301"
                await zip_field.fill(zip_val[:5])
                await asyncio.sleep(random.uniform(6, 10))

        save_btn = page.locator("#MainContent_uc1PropertyAI_btnSaveInterest").first
        if await save_btn.count() > 0:
            await save_btn.wait_for(state="visible", timeout=15000)
            await save_btn.click()
            await page.wait_for_load_state("networkidle")

    await page.click("#MainContent_btnContinue")
    await page.wait_for_load_state("networkidle")
    log("INFO", "On Underwriting page")
    await asyncio.sleep(random.uniform(12, 22))

    # --- PAGE 8: UNDERWRITING ---
    # Prior policy information
    await page.wait_for_selector("#MainContent_ucPriorPolicyInformation_ddlPriorInsuranceCoverage", state="visible", timeout=15000)
    await page.select_option("#MainContent_ucPriorPolicyInformation_ddlPriorInsuranceCoverage", "Prior standard insurance")
    await asyncio.sleep(random.uniform(6, 10))

    prior_carrier_raw = (_contact_first(contact, "prior_carrier_home", "prior_carrier", "current_insurer", "current_home_carrier") or "").strip()
    carrier_ddl = page.locator("#MainContent_ucPriorPolicyInformation_ddlPriorInsuranceCompany").first
    if await carrier_ddl.count() > 0:
        selected = False
        if prior_carrier_raw:
            # Try exact value match first, then partial text match via JS
            try:
                await carrier_ddl.select_option(value=prior_carrier_raw, timeout=3000)
                selected = True
            except Exception:
                pass
            if not selected:
                selected = await carrier_ddl.evaluate(
                    """(el, val) => {
                        const v = val.toLowerCase();
                        for (const o of el.options) {
                            if (o.value.toLowerCase().includes(v) || (o.textContent||'').toLowerCase().includes(v)) {
                                el.value = o.value;
                                el.dispatchEvent(new Event('change', {bubbles:true}));
                                return true;
                            }
                        }
                        return false;
                    }""",
                    prior_carrier_raw,
                )
        if not selected:
            # Pick first non-empty option
            await carrier_ddl.evaluate(
                """el => {
                    for (const o of el.options) {
                        if (o.value && o.value !== '-1' && o.value !== '') {
                            el.value = o.value;
                            el.dispatchEvent(new Event('change', {bubbles:true}));
                            break;
                        }
                    }
                }"""
            )
        await asyncio.sleep(random.uniform(6, 10))

    prior_exp_val = (datetime.now(timezone.utc) + timedelta(days=14)).strftime("%m/%d/%Y")
    await page.fill("#MainContent_ucPriorPolicyInformation_txtExpirationDate", prior_exp_val)
    await asyncio.sleep(random.uniform(6, 10))

    continuous_raw = _contact_first(contact, "years_continuous_ins", "yearsContinuousPropertyInsurance", "continuous_insurance_years")
    continuous_val = str(continuous_raw).strip() if continuous_raw else "4"
    await page.fill("#MainContent_ucPriorPolicyInformation_txtContinuousInsurance", continuous_val)
    await asyncio.sleep(random.uniform(6, 10))

    # Additional underwriting questions — answer all visible selects
    # Index 5 (question #6a) must always be forced to False/No regardless of current value
    FORCE_FALSE_INDEXES = {5}

    all_selects = page.locator("select[id^='MainContent_ucUnderwritingQuestions_rpParentQuestions_ddlAnswer_']")
    for i in range(await all_selects.count()):
        loc = all_selects.nth(i)
        if not await loc.is_visible():
            continue
        cur = (await loc.input_value()).strip()

        # Force specific indexes to False regardless of current value
        if i in FORCE_FALSE_INDEXES:
            for safe_val in ("False", "No", "N"):
                try:
                    await loc.select_option(value=safe_val)
                    await asyncio.sleep(random.uniform(6, 10))
                    await page.wait_for_load_state("networkidle", timeout=8000)
                    break
                except Exception:
                    continue
            continue

        # Skip already-answered questions
        if cur not in ("", "-1"):
            continue

        # Get question text to pick the right answer
        q_text = ""
        try:
            q_text = await loc.evaluate("el => { let tr = el.closest('tr, li'); return tr ? tr.innerText : el.parentElement.innerText; }")
            q_text = q_text.lower()
        except Exception:
            pass

        if "site access" in q_text or "underwriting site" in q_text:
            try:
                await loc.select_option(value="FlatAreaEasyAccessRoads")
                await asyncio.sleep(random.uniform(6, 10))
                await page.wait_for_load_state("networkidle", timeout=8000)
                continue
            except Exception:
                pass
        if "paperless" in q_text or "electronic" in q_text:
            try:
                await loc.select_option(value="True")
                await asyncio.sleep(random.uniform(6, 10))
                await page.wait_for_load_state("networkidle", timeout=8000)
                continue
            except Exception:
                pass
        # Everything else: False/No
        for safe_val in ("False", "No", "N"):
            try:
                await loc.select_option(value=safe_val)
                await asyncio.sleep(random.uniform(6, 10))
                await page.wait_for_load_state("networkidle", timeout=8000)
                break
            except Exception:
                continue

    await page.click("#MainContent_btnContinue")
    await page.wait_for_load_state("networkidle")
    log("INFO", "On Loss History page")
    await asyncio.sleep(random.uniform(12, 22))

    # --- PAGE 9: LOSS HISTORY ---
    await page.click("#MainContent_btnContinue")
    await page.wait_for_load_state("networkidle")
    log("INFO", "On Coverage page")
    await asyncio.sleep(random.uniform(12, 22))

    # --- PAGE 10: COVERAGE ---
    # Answer road accessibility question if present (always Yes)
    cov_labels = page.locator("label")
    road_pattern = re.compile(r"(accessible|accesible).{0,80}(year|round|road)|(paved|plowed|maintained.{0,40}road)", re.I | re.S)
    for i in range(min(await cov_labels.count(), 600)):
        lb = cov_labels.nth(i)
        try:
            txt = (await lb.inner_text()).strip()
        except Exception:
            continue
        if not txt or not road_pattern.search(re.sub(r"\s+", " ", txt)):
            continue
        fid = await lb.get_attribute("for")
        if fid:
            tg = page.locator(f"#{fid}")
            if await tg.count() > 0:
                tag = await tg.evaluate("el => el.tagName.toLowerCase()")
                if tag == "select":
                    cur = (await tg.input_value()).strip()
                    if cur in ("", "-1"):
                        for yes_val in ("True", "Yes", "Y", "1"):
                            try:
                                await tg.select_option(value=yes_val)
                                await page.wait_for_load_state("networkidle")
                                break
                            except Exception:
                                continue

    await page.wait_for_selector("#MainContent_ucCoreCoverages_rptCoreCoverages_txtCovA_0", state="visible", timeout=30000)
    await page.wait_for_selector("#MainContent_ddlPerils", state="visible", timeout=30000)

    # Coverage A
    cov_a_raw = _contact_first(contact, "coverage_a", "coverageA", "dwelling_limit", "dwellingLimit")
    cov_a_val = re.sub(r"\D", "", str(cov_a_raw)) if cov_a_raw else "452937"
    if not cov_a_val or int(cov_a_val) <= 0:
        cov_a_val = "452937"

    portal_cov_a = (await page.locator("#MainContent_ucCoreCoverages_rptCoreCoverages_txtCovA_0").input_value()).strip()
    portal_digits = re.sub(r"\D", "", portal_cov_a)
    if not portal_digits or int(portal_digits) <= 0:
        await page.fill("#MainContent_ucCoreCoverages_rptCoreCoverages_txtCovA_0", cov_a_val)
        await asyncio.sleep(random.uniform(6, 10))
        await page.locator("#MainContent_ucCoreCoverages_rptCoreCoverages_txtCovA_0").evaluate(
            "el => { el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); el.blur(); }"
        )
        await page.wait_for_load_state("networkidle", timeout=30000)

    # Coverage B/C/D and Fair Rental
    await page.select_option("#MainContent_ucCoreCoverages_rptCoreCoverages_ddlCovB_1", "0.0500")
    await asyncio.sleep(random.uniform(6, 10))
    await page.wait_for_load_state("networkidle", timeout=30000)
    await page.select_option("#MainContent_ucCoreCoverages_rptCoreCoverages_ddlCovC_2", "0.2500")
    await asyncio.sleep(random.uniform(6, 10))
    await page.wait_for_load_state("networkidle", timeout=30000)
    await page.select_option("#MainContent_ucCoreCoverages_rptCoreCoverages_ddlCovD_3", "0.1500")
    await asyncio.sleep(random.uniform(6, 10))
    await page.wait_for_load_state("networkidle", timeout=30000)
    await page.select_option("#MainContent_ucCoreCoverages_rptCoreCoverages_ddlFairRentalValue_4", "0.0000")
    await asyncio.sleep(random.uniform(6, 10))
    await page.wait_for_load_state("networkidle", timeout=30000)

    # All Perils Deductible
    await page.select_option("#MainContent_ddlPerils", "0.010")
    await asyncio.sleep(random.uniform(6, 10))
    await page.wait_for_load_state("networkidle", timeout=30000)

    await page.click("#MainContent_btnContinue")
    await page.wait_for_load_state("networkidle")
    log("INFO", "On Driver Info page")
    await asyncio.sleep(random.uniform(12, 22))

    # --- PAGE 11: DRIVER INFO ---
    # Helpers ---------------------------------------------------------------
    def _ddl_is_blank(val: str) -> bool:
        return val.strip() in ("", "-1", "0")

    async def _ddl_val(sel: str) -> str:
        loc = page.locator(sel)
        if await loc.count() > 0:
            return (await loc.first.input_value()).strip()
        return ""

    async def _txt_val(sel: str) -> str:
        loc = page.locator(sel)
        if await loc.count() > 0:
            return (await loc.first.input_value()).strip()
        return ""

    # Fill only if the dropdown is currently blank/unselected
    async def _fill_ddl_if_blank(sel: str, value: str, *, wait_network: bool = False) -> None:
        cur = await _ddl_val(sel)
        if not _ddl_is_blank(cur):
            return
        try:
            await page.select_option(sel, value)
            await asyncio.sleep(random.uniform(6, 10))
            if wait_network:
                await page.wait_for_load_state("networkidle")
        except Exception:
            pass

    # Fill only if the text field is currently blank/invalid
    async def _fill_txt_if_blank(sel: str, value: str, invalid: tuple = ()) -> None:
        cur = await _txt_val(sel)
        if cur and cur not in invalid:
            return
        if await page.locator(sel).count() > 0:
            await page.fill(sel, value)
            await asyncio.sleep(random.uniform(6, 10))

    # Helper: fill the driver form that is currently open on screen
    async def _fill_driver_form(is_primary: bool = False, driver_index: int = 0) -> None:
        await page.wait_for_selector("#MainContent_txtFirstName", state="visible", timeout=20000)

        # Name — only fill if blank (portal may have prefilled from prefill page)
        cur_fn = await _txt_val("#MainContent_txtFirstName")
        if not cur_fn and is_primary:
            fn = _contact_first(contact, "firstName", "first_name")
            if fn:
                await page.fill("#MainContent_txtFirstName", fn)
                await asyncio.sleep(random.uniform(6, 10))

        cur_ln = await _txt_val("#MainContent_txtLastName")
        if not cur_ln and is_primary:
            ln = _contact_first(contact, "lastName", "last_name")
            if ln:
                await page.fill("#MainContent_txtLastName", ln)
                await asyncio.sleep(random.uniform(6, 10))

        # DOB — only fill if blank or invalid (1/1/0001)
        dob_field = page.locator("#MainContent_txtDateOfBirth")
        if await dob_field.count() > 0:
            cur_dob = (await dob_field.first.input_value()).strip()
            if not cur_dob or cur_dob in ("1/1/0001", "01/01/0001"):
                dob_raw = _contact_first(contact, "dateOfBirth", "date_of_birth") if is_primary else ""
                driver_dob = _date(dob_raw) if dob_raw else "01/01/1990"
                await page.fill("#MainContent_txtDateOfBirth", driver_dob)
                await asyncio.sleep(random.uniform(6, 10))
                await page.evaluate("() => { const el = document.getElementById('MainContent_txtDateOfBirth'); if (el) el.dispatchEvent(new Event('focusout', {bubbles:true})); }")
                await page.wait_for_timeout(300)

        # Gender — only if blank
        if _ddl_is_blank(await _ddl_val("#MainContent_ddlGender")):
            gender_raw = _contact_first(contact, "gender") or "" if is_primary else ""
            gender_val = "F" if gender_raw.strip().casefold() in ("f", "female") else "M"
            await _fill_ddl_if_blank("#MainContent_ddlGender", gender_val, wait_network=True)

        # Marital status — only if blank
        if _ddl_is_blank(await _ddl_val("#MainContent_ddlMaritalStatus")):
            marital_raw = _contact_first(contact, "maritalStatus", "marital_status") or "" if is_primary else ""
            marital_allowed = ("Divorced", "Married", "Single", "Separated", "Unknown", "Widowed")
            marital_val = next((a for a in marital_allowed if a.casefold() == marital_raw.strip().casefold()), "Single")
            await _fill_ddl_if_blank("#MainContent_ddlMaritalStatus", marital_val, wait_network=True)

        # Occupation — only if blank
        await _fill_ddl_if_blank("#MainContent_ddlOccupation", "Other", wait_network=True)

        # Education level — only if blank
        await _fill_ddl_if_blank("#MainContent_ddlEducationLevel", "4")

        # Household member — only if blank
        await _fill_ddl_if_blank("#MainContent_ddlDriverHouseholdMember", "True")

        # Dynamic Drive
        opt_in_loc = page.locator("#MainContent_ddlConnectedDriverOptIn")
        if await opt_in_loc.count() > 0 and await opt_in_loc.first.is_visible():
            cur_opt = (await opt_in_loc.first.input_value()).strip()
            if _ddl_is_blank(cur_opt):
                if is_primary:
                    await page.select_option("#MainContent_ddlConnectedDriverOptIn", "True")
                    await asyncio.sleep(random.uniform(6, 10))
                    await page.wait_for_load_state("networkidle")
                    phone = _contact_first(contact, "phone", "phoneNumber", "mobile")
                    if phone:
                        try:
                            ph_area, ph_prefix, ph_line = split_phone_number(phone)
                            await _fill_txt_if_blank("#MainContent_txtAreaCode", ph_area)
                            await _fill_txt_if_blank("#MainContent_txtPrefix", ph_prefix)
                            await _fill_txt_if_blank("#MainContent_txtLineNumber", ph_line)
                        except Exception:
                            pass
                    em = _contact_first(contact, "email")
                    if em:
                        await _fill_txt_if_blank("#MainContent_txtEmail", em)
                else:
                    try:
                        await page.select_option("#MainContent_ddlConnectedDriverOptIn", "False")
                    except Exception:
                        pass
                    await asyncio.sleep(random.uniform(6, 10))
                    await page.wait_for_load_state("networkidle")

        # Operator type — only if blank
        await _fill_ddl_if_blank("#MainContent_ddlOperatorType", "Operator", wait_network=True)

        # Losses in 5 years — only if blank
        await _fill_ddl_if_blank("#MainContent_ddlLossesIn5Years", "False")

        # Defensive driver — only if blank, force No via JS
        def_loc = page.locator("#MainContent_ddlDefensiveDriverCourse")
        if await def_loc.count() > 0:
            cur_def = (await def_loc.first.input_value()).strip()
            if _ddl_is_blank(cur_def):
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
                    "MainContent_ddlDefensiveDriverCourse",
                )
                await page.wait_for_timeout(500)

        # Relationship — only if blank; max 2 drivers can be Insured (G2005)
        rel_loc = page.locator("xpath=/html/body/form/div[3]/div[2]/div[8]/div[4]/fieldset/ul[1]/li[7]/select")
        if await rel_loc.count() > 0:
            cur_rel = (await rel_loc.first.input_value()).strip()
            if _ddl_is_blank(cur_rel):
                rel_val = "Insured" if driver_index < 2 else "Relative"
                await rel_loc.select_option(rel_val)
                await asyncio.sleep(random.uniform(6, 10))
                await page.wait_for_load_state("networkidle")

        # License status — always set Active (this is the whole point of this loop)
        lic_status_loc = page.locator("xpath=/html/body/form/div[3]/div[2]/div[8]/div[4]/fieldset/ul[2]/li[4]/select")
        if await lic_status_loc.count() > 0:
            await lic_status_loc.select_option("Active")
            await asyncio.sleep(random.uniform(6, 10))
            await page.wait_for_load_state("networkidle")

        # License state — only if blank
        await _fill_ddl_if_blank("#MainContent_ddlLicenseState", "GA")

        # Years experience — only if blank
        if await page.locator("#MainContent_txtYearsExperience").count() > 0:
            cur_exp = (await page.locator("#MainContent_txtYearsExperience").first.input_value()).strip()
            if not cur_exp:
                await page.fill("#MainContent_txtYearsExperience", "4")
                await asyncio.sleep(random.uniform(6, 10))
                await page.locator("#MainContent_txtYearsExperience").evaluate(
                    "el => { el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); el.blur(); }"
                )
                await page.wait_for_load_state("networkidle", timeout=15000)

        # License number — only if blank
        if await page.locator("#MainContent_txtDriversLicenseNumber").count() > 0:
            cur_lic = (await page.locator("#MainContent_txtDriversLicenseNumber").first.input_value()).strip()
            if not cur_lic:
                if is_primary:
                    lic_raw = _contact_first(contact, "driverLicenseNumber", "driversLicenseNumber", "licenseNumber", "driver_license_number") or ""
                    lic_digits = re.sub(r"\D", "", str(lic_raw))
                    lic_val = lic_digits[:9].zfill(9) if lic_digits else f"12345678{driver_index}"
                else:
                    lic_val = f"12345678{driver_index}"
                await page.fill("#MainContent_txtDriversLicenseNumber", lic_val)
                await asyncio.sleep(random.uniform(6, 10))

        # Save the driver form — retry up to 5 times if grid doesn't reappear
        for _save_attempt in range(5):
            save_btn = page.locator("#MainContent_btnSaveDriver")
            if await save_btn.count() == 0:
                break
            await save_btn.click()
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(random.uniform(6, 10))

            # Success = driver grid is back on screen
            if await page.locator("#MainContent_gvDrivers").count() > 0:
                break

            # Still on the form — validation error or page didn't navigate
            # Re-fill required fields and try again
            log("INFO", f"Driver save attempt {_save_attempt + 1}: grid not visible, retrying fill")
            await asyncio.sleep(random.uniform(6, 10))

            # Re-fill only the fields most likely to cause validation failure
            rel_retry = page.locator("xpath=/html/body/form/div[3]/div[2]/div[8]/div[4]/fieldset/ul[1]/li[7]/select")
            if await rel_retry.count() > 0:
                cur_rel_r = (await rel_retry.first.input_value()).strip()
                if _ddl_is_blank(cur_rel_r):
                    _rel_map = ["Insured", "Insured", "Spouse", "Spouse", "Child", "Child", "Sibling", "Sibling", "Parent", "Parent"]
                    await rel_retry.select_option(_rel_map[driver_index] if driver_index < len(_rel_map) else "Child")
                    await asyncio.sleep(random.uniform(6, 10))
                    await page.wait_for_load_state("networkidle")

            lic_status_retry = page.locator("xpath=/html/body/form/div[3]/div[2]/div[8]/div[4]/fieldset/ul[2]/li[4]/select")
            if await lic_status_retry.count() > 0:
                await lic_status_retry.select_option("Active")
                await asyncio.sleep(random.uniform(6, 10))
                await page.wait_for_load_state("networkidle")

            dob_field = page.locator("#MainContent_txtDateOfBirth")
            if await dob_field.count() > 0:
                cur_dob = (await dob_field.first.input_value()).strip()
                if not cur_dob or cur_dob in ("1/1/0001", "01/01/0001"):
                    await page.fill("#MainContent_txtDateOfBirth", "01/01/1990")
                    await asyncio.sleep(random.uniform(6, 10))
                    await page.evaluate("() => { const el = document.getElementById('MainContent_txtDateOfBirth'); if (el) el.dispatchEvent(new Event('focusout', {bubbles:true})); }")
                    await page.wait_for_timeout(300)

            if await page.locator("#MainContent_ddlGender").count() > 0:
                cur_g = (await page.locator("#MainContent_ddlGender").first.input_value()).strip()
                if _ddl_is_blank(cur_g):
                    await page.select_option("#MainContent_ddlGender", "M")
                    await asyncio.sleep(random.uniform(6, 10))
                    await page.wait_for_load_state("networkidle")

            if await page.locator("#MainContent_ddlMaritalStatus").count() > 0:
                cur_m = (await page.locator("#MainContent_ddlMaritalStatus").first.input_value()).strip()
                if _ddl_is_blank(cur_m):
                    await page.select_option("#MainContent_ddlMaritalStatus", "Single")
                    await asyncio.sleep(random.uniform(6, 10))
                    await page.wait_for_load_state("networkidle")

            if await page.locator("#MainContent_ddlOperatorType").count() > 0:
                cur_op = (await page.locator("#MainContent_ddlOperatorType").first.input_value()).strip()
                if _ddl_is_blank(cur_op):
                    await page.select_option("#MainContent_ddlOperatorType", "Operator")
                    await asyncio.sleep(random.uniform(6, 10))
                    await page.wait_for_load_state("networkidle")

            if await page.locator("#MainContent_txtDriversLicenseNumber").count() > 0:
                cur_lic = (await page.locator("#MainContent_txtDriversLicenseNumber").first.input_value()).strip()
                if not cur_lic:
                    await page.fill("#MainContent_txtDriversLicenseNumber", f"12345678{driver_index}")
                    await asyncio.sleep(random.uniform(6, 10))

            if await page.locator("#MainContent_txtYearsExperience").count() > 0:
                cur_exp = (await page.locator("#MainContent_txtYearsExperience").first.input_value()).strip()
                if not cur_exp:
                    await page.fill("#MainContent_txtYearsExperience", "4")
                    await asyncio.sleep(random.uniform(6, 10))
                    await page.locator("#MainContent_txtYearsExperience").evaluate(
                        "el => { el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); el.blur(); }"
                    )
                    await page.wait_for_load_state("networkidle", timeout=15000)

    # Read driver grid and process all rows
    if await page.locator("#MainContent_gvDrivers").count() > 0:
        # Fresh scan every iteration — grid reloads after each Save
        driver_index = 0
        while True:
            rows = page.locator("#MainContent_gvDrivers tbody tr")
            total = await rows.count()
            log("INFO", f"Driver grid: {total} row(s), checking index {driver_index}")

            if driver_index >= total:
                break

            row = rows.nth(driver_index)

            # License status is in the 5th <td> (index 4, zero-based)
            cells = row.locator("td")
            cell_count = await cells.count()
            license_status = ""
            if cell_count >= 5:
                license_status = (await cells.nth(4).inner_text()).strip()

            if license_status.lower() == "active":
                log("INFO", f"Driver {driver_index}: already Active — skipping")
                driver_index += 1
                continue

            # Click View/Edit for this driver
            view_edit_link = row.locator("a:has-text('View/Edit')")
            if await view_edit_link.count() == 0:
                log("INFO", f"Driver {driver_index}: no View/Edit link — skipping")
                driver_index += 1
                continue

            log("INFO", f"Driver {driver_index}: status='{license_status}' — opening View/Edit")
            await view_edit_link.click()
            await page.wait_for_load_state("networkidle")

            is_primary = driver_index == 0
            await _fill_driver_form(is_primary=is_primary, driver_index=driver_index)

            log("INFO", f"Driver {driver_index}: saved — moving to next")
            driver_index += 1

        log("INFO", "All drivers processed")
    else:
        # No grid visible — direct form (only one driver, fill as primary)
        await _fill_driver_form(is_primary=True, driver_index=0)
        log("INFO", "Primary driver saved")

    # Connected Driver Agreement checkbox if present (shown after all drivers done)
    if await page.locator("#MainContent_chkConnectedDriverAgreement").count() > 0:
        if not await page.locator("#MainContent_chkConnectedDriverAgreement").is_checked():
            try:
                await page.locator("#MainContent_chkConnectedDriverAgreement").check(force=True)
                await page.wait_for_load_state("networkidle")
            except Exception:
                await page.evaluate("document.getElementById('MainContent_chkConnectedDriverAgreement').click()")
                await page.wait_for_load_state("networkidle")

    await page.click("#MainContent_btnContinue")
    await page.wait_for_load_state("networkidle")
    log("INFO", "On Driver Violations page")
    await asyncio.sleep(random.uniform(12, 22))

    # --- PAGE 12: DRIVER VIOLATIONS ---
    await page.click("#MainContent_btnContinue")
    await page.wait_for_load_state("networkidle")
    log("INFO", "On Vehicles page")
    await asyncio.sleep(random.uniform(12, 22))

    # --- PAGE 13: VEHICLES ---
    log("INFO", "Starting PAGE 13: Vehicles")

    vehicles = contact.get("vehicles", [])
    if not isinstance(vehicles, list):
        vehicles = []

    _VEHICLE_FIELD_BACKUPS_13: dict[str, Any] = {
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
        target = "Included - 25,000 (250 Ded)"
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
                    if (!t.includes('uninsured') || !t.includes('motorist') || !t.includes('property damage')) continue;
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
                        if (norm(opt.text) === want || norm(opt.label) === want) return sel.id;
                    }
                }
                return null;
            }""",
            target,
        )
        if not found:
            log("WARN", "UMPD Reduced By dropdown not found on Vehicle Info list page")
            return
        try:
            await page.select_option(f"#{found}", label=target)
        except Exception:
            try:
                await page.select_option(f"#{found}", value=target)
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

    async def _v13_ensure_select(selector: str, value: str, *, label: str | None = None) -> None:
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

    async def _v13_fill_text_if_empty(selector: str, value: str) -> None:
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
        applied: dict[str, str] = {}

        purchase_raw = (
            ghl_vehicle.get("purchase_date")
            or ghl_vehicle.get("purchaseDate")
            or _VEHICLE_FIELD_BACKUPS_13["purchase_date"]
        )
        purchase_val = _date(str(purchase_raw)) if purchase_raw else _VEHICLE_FIELD_BACKUPS_13["purchase_date"]
        await _v13_fill_text_if_empty("#MainContent_ucVehicleInfo_txtPurchaseDate", purchase_val)
        applied["Purchased Date"] = purchase_val

        await _v13_ensure_select("#MainContent_ucVehicleInfo_ddlAutoRentedToOthers", str(_VEHICLE_FIELD_BACKUPS_13["auto_rented_to_others"]), label="No")
        applied["Auto Rented To Others"] = "No"

        await _v13_ensure_select("#MainContent_ucVehicleInfo_ddlVehicleWeightBetween14kAnd16k", str(_VEHICLE_FIELD_BACKUPS_13["vehicle_weight_14k_16k"]), label="No")
        applied["Vehicle Weight 14k-16k"] = "No"

        await _v13_ensure_select("#MainContent_ucVehicleInfo_ddlAgreedValue", str(_VEHICLE_FIELD_BACKUPS_13["agreed_value"]), label="No")
        applied["Agreed Value"] = "No"

        await _v13_ensure_select("#MainContent_ucVehicleInfo_ddlCamperIncluded", str(_VEHICLE_FIELD_BACKUPS_13["camper_included"]), label="No")
        applied["Camper Included"] = "No"

        await _v13_ensure_select("#MainContent_ucVehicleInfo_ddlSuspensionIndicator", str(_VEHICLE_FIELD_BACKUPS_13["suspension_indicator"]), label="No")
        applied["Suspension Indicator"] = "No"

        base_price = str(_VEHICLE_FIELD_BACKUPS_13["base_list_price"])
        await _v13_fill_text_if_empty("#MainContent_ucVehicleInfo_txtBasePrice", base_price)
        applied["Base List Price"] = base_price

        own_raw = ghl_vehicle.get("ownership_status")
        if own_raw is None or str(own_raw).strip() in ("", "-1"):
            own_val = str(_VEHICLE_FIELD_BACKUPS_13["ownership_status"])
        else:
            own_val = str(int(own_raw)) if str(own_raw).isdigit() else str(_VEHICLE_FIELD_BACKUPS_13["ownership_status"])
        await _v13_ensure_select("#MainContent_ucVehicleInfo_ddlOwnershipStatus", own_val, label="Own")
        applied["Ownership Status"] = "Own" if own_val == "3" else own_val

        mileage_raw = ghl_vehicle.get("annual_mileage") or ghl_vehicle.get("annualMileage")
        mileage_val = str(mileage_raw) if mileage_raw not in (None, "") else str(_VEHICLE_FIELD_BACKUPS_13["annual_mileage"])
        await _v13_fill_text_if_empty("#MainContent_ucVehicleInfo_txtAnnualMileage", mileage_val)
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

    _SAVE_VEHICLE_SELECTORS_13 = (
        "input#MainContent_btnSaveVehicle",
        "#MainContent_btnSaveVehicle",
    )
    _SAVE_VEHICLE_COV_SELECTORS_13 = (
        "input#MainContent_btnSaveVehicleCov",
        "#MainContent_btnSaveVehicleCov",
        "#MainContent_divCovButtons input.btnRight",
    )

    async def _v13_selector_visible(selectors: tuple[str, ...]) -> bool:
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
        return await _v13_selector_visible(
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
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if await _on_vehicle_list_page():
                return "list"
            if await _v13_selector_visible(_SAVE_VEHICLE_COV_SELECTORS_13):
                return "cov"
            await page.wait_for_timeout(1000)
        return ""

    async def _save_vehicle_and_return_to_list() -> bool:
        for attempt in range(1, 7):
            log("INFO", f"Vehicle save flow attempt {attempt}/6")

            if await _on_vehicle_list_page():
                log("INFO", "On vehicle list — save flow complete")
                return True

            if await _v13_selector_visible(_SAVE_VEHICLE_COV_SELECTORS_13):
                if not await _click_vehicle_submit(_SAVE_VEHICLE_COV_SELECTORS_13, "Save Vehicle Coverages"):
                    log("WARN", f"Save Vehicle Coverages click failed (attempt {attempt})")
                    await page.wait_for_timeout(1000)
                    continue
                state = await _wait_for_vehicle_cov_or_list(timeout_s=40.0)
                if state == "list" or await _on_vehicle_list_page():
                    await _wait_for_vehicle_list_stable()
                    log("INFO", "Returned to vehicle list after Save Vehicle Coverages")
                    return True
                continue

            if await _on_vehicle_edit_form() or await _v13_selector_visible(_SAVE_VEHICLE_SELECTORS_13):
                if not await _click_vehicle_submit(_SAVE_VEHICLE_SELECTORS_13, "Save Vehicle"):
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
                log("WARN", f"Still on vehicle edit form after Save Vehicle (attempt {attempt}); retrying")
                continue

            await page.wait_for_timeout(1000)

        await _wait_for_vehicle_list_stable()
        if await _on_vehicle_list_page():
            return True
        log("WARN", "Not on vehicle list after save flow (all attempts exhausted)")
        return False

    def _ghl_index_for_portal_row_13(row: dict) -> int:
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
        log("INFO", f"Editing portal row {portal_idx} with GHL vehicle {ghl_idx}: {_ghl_vehicle_description(ghl_v)}")
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
        used_ghl: set[int] = set()
        for _ in range(3):
            portal_rows = await _scrape_portal_vehicle_rows()
            if not portal_rows:
                break
            progressed = False
            for row in portal_rows:
                portal_idx = int(row["idx"])
                ghl_idx = _ghl_index_for_portal_row_13(row)
                if ghl_idx < 0:
                    log("WARN", f"No CRM match for portal row {portal_idx} ({row.get('name')}); skipping fill")
                    continue
                if ghl_idx in used_ghl:
                    log("WARN", f"CRM vehicle {ghl_idx} already used; row {portal_idx} skipping")
                    continue
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
            ghl_idx = _ghl_index_for_portal_row_13(row)
            if ghl_idx < 0:
                log("WARN", f"No CRM data for Vehicle {vehicle_num} (row {portal_idx})")
                continue
            log("INFO", f"Retrying fill for Vehicle {vehicle_num} (portal row {portal_idx})")
            await _fill_matched_vehicle(portal_idx, ghl_idx)

    await _set_umpd_reduced_by()
    await _wait_for_vehicle_list_stable()

    match_plan_13: dict[str, Any] = {"matches": [], "delete": []}
    portal_rows_13 = await _scrape_portal_vehicle_rows()
    log("INFO", f"Portal vehicle rows: {portal_rows_13}")
    log("INFO", f"GHL vehicles ({len(vehicles)}): {[_ghl_vehicle_description(v) for v in vehicles]}")

    for _ in range(3):
        portal_rows_13 = await _scrape_portal_vehicle_rows()
        if not portal_rows_13:
            break
        match_plan_13 = await _gemini_match_vehicles(portal_rows_13, vehicles)
        delete_indices = sorted({int(i) for i in match_plan_13.get("delete", [])}, reverse=True)
        if not delete_indices:
            break
        for idx in delete_indices:
            log("INFO", f"Deleting portal vehicle row {idx} (not in CRM match plan)")
            if not await _delete_vehicle_row(idx):
                log("WARN", f"Could not delete vehicle row {idx}")

    log("INFO", f"Vehicle match plan after cleanup: {json.dumps(match_plan_13, default=str)}")

    await _fill_all_list_vehicles()

    # Continue to next page (retry up to 5x, handle validation errors)
    navigated = False
    for attempt in range(5):
        log("INFO", f"Vehicles Continue click attempt {attempt + 1}")
        click_success = False
        for sel in ["#MainContent_btnContinue", "input[name$='btnContinue']", "button:has-text('Continue')", "button:has-text('Next')"]:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
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
                err_text_v = ""
                if await page.locator("#lstErrors li").count() > 0:
                    err_text_v = (await page.locator("#lstErrors").inner_text()).strip()
                    if err_text_v:
                        log("WARN", f"VehicleInfo validation errors: {err_text_v}")
                if err_text_v:
                    await _retry_fill_from_validation_error(err_text_v)
        await page.wait_for_timeout(1000)

    if not navigated:
        if "VehicleInfo" in page.url and await page.locator("#lstErrors li").count() > 0:
            err = (await page.locator("#lstErrors").inner_text()).strip()
            raise RuntimeError(f"Vehicle validation blocked continue: {err}")
        else:
            raise RuntimeError("Could not click Continue on Vehicles")

    log("INFO", "Completed PAGE 13: Vehicles")
    log("INFO", "On Auto Underwriting page")
    await asyncio.sleep(random.uniform(12, 22))

    # --- PAGE 14: AUTO UNDERWRITING ---
    await page.wait_for_load_state("domcontentloaded")
    await asyncio.sleep(random.uniform(6, 10))

    # ── Auto Information Questions (AI-answered — question set/order shifts
    # by state, so we scrape whatever's actually on the page by label text
    # and answer against our standing rules instead of hardcoded indices) ──
    await page.wait_for_selector(
        "#MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_0",
        state="visible",
        timeout=30000,
    )

    _uw_questions = await _scrape_uw_questions(page, "MainContent_ucAutoQuestions_rpParentQuestions_")
    log("INFO", f"Scraped {len(_uw_questions)} Auto Information question(s)")
    _uw_answers = await _ai_answer_underwriting_questions(_uw_questions, contact)
    await _apply_uw_answers(page, _uw_questions, _uw_answers)

    # Click Next — portal may return P2241 error if carrier lookup fails
    await page.wait_for_selector("#MainContent_btnContinue", state="visible", timeout=15000)
    await page.evaluate("document.getElementById('MainContent_btnContinue').click()")
    await page.wait_for_load_state("networkidle")
    await asyncio.sleep(random.uniform(6, 10))

    # ── P2241 check — only visible AFTER clicking Continue ───────────────────
    _p2241_visible = (
        await page.locator("text=P2241").count() > 0
        or await page.locator("li:has-text('P2241'), span:has-text('P2241')").count() > 0
    )

    if _p2241_visible:
        log("INFO", "P2241 detected — filling Prior Policy Information manually")
        _auto_prior_exp = (datetime.now(timezone.utc) + timedelta(days=14)).strftime("%m/%d/%Y")

        # Step 1: Click "Insured Provided Data" radio → triggers ASP.NET postback
        try:
            await page.click("#MainContent_ucPolicyCarrierInfo_rbInsuredProvidedData")
            # Use domcontentloaded — networkidle times out on ASP.NET postbacks
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(3)
        except Exception as _e:
            log("WARNING", f"P2241: radio click failed — {_e}")

        # Step 2: Prior Insurance Co → "State Farm Group"
        # NOTE: this dropdown has onchange=__doPostBack — select_option fires it automatically.
        # Do NOT manually dispatch change again (double postback breaks the page).
        # Wait for domcontentloaded after selection to let the postback complete.
        try:
            await page.wait_for_selector(
                "#MainContent_ucPolicyCarrierInfo_ddlPriorInsCo",
                state="visible", timeout=10000,
            )
            await page.select_option("#MainContent_ucPolicyCarrierInfo_ddlPriorInsCo", "State Farm Group")
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(3)
        except Exception as _e:
            log("WARNING", f"P2241: Prior Insurance Co failed — {_e}")

        # Step 3: Prior BI Coverage → "0" = "State Minimum" (25/50 for GA)
        try:
            await page.select_option("#MainContent_ucPolicyCarrierInfo_ddlPriorBICoverage", "0")
            await asyncio.sleep(random.uniform(6, 10))
        except Exception as _e:
            log("WARNING", f"P2241: Prior BI Coverage failed — {_e}")

        # Step 4: Prior Expiration Date → today + 14 days
        try:
            await page.fill("#MainContent_ucPolicyCarrierInfo_txtPriorExpDate", _auto_prior_exp)
            await page.evaluate("""
                const el = document.getElementById('MainContent_ucPolicyCarrierInfo_txtPriorExpDate');
                if (el) {
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    el.dispatchEvent(new Event('blur'));
                }
            """)
            await asyncio.sleep(random.uniform(6, 10))
        except Exception as _e:
            log("WARNING", f"P2241: Prior Expiration Date failed — {_e}")

        # Step 5: Months with Most Recent Insurance Co → "35" = "12 to 35 months"
        try:
            await page.wait_for_selector(
                "#MainContent_ucPolicyCarrierInfo_ddlPriorMonthsWMostRecentIns:not([disabled])",
                timeout=12000,
            )
            await page.select_option("#MainContent_ucPolicyCarrierInfo_ddlPriorMonthsWMostRecentIns", "35")
            await asyncio.sleep(random.uniform(6, 10))
        except Exception as _e:
            log("WARNING", f"P2241: Months dropdown failed — {_e}")

        log("INFO", f"P2241 fields filled: State Farm Group | State Minimum | exp={_auto_prior_exp} | 12-35 months")

        # Click Next again after filling prior policy info
        try:
            await page.wait_for_selector("#MainContent_btnContinue", state="visible", timeout=15000)
            try:
                await page.click("#MainContent_btnContinue", timeout=10000)
            except Exception:
                try:
                    await page.locator("#MainContent_btnContinue").first.click(force=True, timeout=10000)
                except Exception:
                    await page.evaluate("document.getElementById('MainContent_btnContinue').click()")
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(2)
        except Exception as _e:
            log("ERROR", f"P2241: Next button click failed — {_e}")

    log("INFO", "On Premium Summary page")
    await asyncio.sleep(random.uniform(12, 22))

    # --- PAGE 15: PREMIUM SUMMARY ---
    # Safety check — if still on Auto Underwriting, try clicking Next one more time
    if "AutoUnderwriting" in page.url or "autounderwriting" in page.url.lower():
        log("WARNING", "Still on Auto Underwriting page — attempting one more Next click")
        try:
            await page.click("#MainContent_btnContinue", timeout=10000)
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(2)
        except Exception as _e:
            log("ERROR", f"Final Next click failed: {_e}")

    await _p15_wait_ready(page)

    log("INFO", "Refreshing Premium Summary")
    await page.reload(wait_until="networkidle")
    await _p15_wait_ready(page)
    await _p15_wait_payment_ready(page)
    await _p15_set_payment_options(page)

    if not await _p15_click_rerate(page):
        raise RuntimeError("Re-Rate failed on Premium Summary")

    rates = await _p15_extract_rates(page)
    premium = rates.get("premium")
    auto_premium = rates.get("auto_premium")
    home_premium = rates.get("home_premium")
    if not premium and auto_premium and home_premium:
        try:
            auto_val = float(re.sub(r"[^\d.]", "", auto_premium))
            home_val = float(re.sub(r"[^\d.]", "", home_premium))
            premium = _p15_fmt(f"{auto_val + home_val:,.2f}")
            log("INFO", f"Derived total premium from auto+home: {premium}")
        except (TypeError, ValueError):
            pass
    if not premium:
        raise RuntimeError("Could not extract total premium from Premium Summary rate table")

    pdf_path = await _p15_download_quote_proposal(page, contact)

    log("INFO", "Flow complete")

    return {
        "premium": premium,
        "auto_premium": auto_premium,
        "home_premium": home_premium,
        "pdf_path": pdf_path,
        "pay_plan": "Installments",
        "error": None,
    }


async def run_bot(contact: dict) -> dict:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    contact = _normalize_contact_payload(contact)
    enrich_contact_from_custom_fields(contact)

    username = os.environ.get("NATGEN_USERNAME", "")
    password = os.environ.get("NATGEN_PASSWORD", "")
    agent_id = os.environ.get("NATGEN_AGENT_ID", "")
    otp_sender = os.environ.get("NATGEN_2FA_SENDER", "43015")

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
            flow_result = await full_flow(page, context, username, password, otp_sender, contact, agent_id)
            results.update(flow_result)
            if not results.get("error"):
                results["success"] = True
        except Exception as exc:
            log("ERROR", f"Bot failed: {exc}")
            results["error"] = str(exc)

        return results
