"""Regulatory approval-date lookup (Gemini, two-pass verification), for the jurisdictions the drug is marketed in."""

import logging

import pandas as pd

from .. import config
from ..gemini_utils import gemini_json_call
from ..utils import norm_phase
from .constants import JURISDICTION_FULL
from .lookups import run_lookups

logger = logging.getLogger(__name__)

NOT_FOUND = "Not found"

FIND_PROMPT = """You are a pharmaceutical regulatory expert.
Drug: {drug}
Jurisdiction: {jurisdiction}

Task: List EVERY regulatory marketing-authorisation approval date for this drug in this
jurisdiction (all indications, formulations, brand names). Use only factual, publicly verifiable data.

Rules:
- Respond ONLY with a JSON object.
- Format: {{"dates": ["YYYY-MM-DD", ...], "source": "<brief source>"}}
- If you are NOT confident or the drug is not approved there, return: {{"dates": [], "source": "unknown"}}
"""

VERIFY_PROMPT = """You are a pharmaceutical regulatory expert.
Drug: {drug}
Jurisdiction: {jurisdiction}

A previous search returned the earliest approval date as {earliest}.
Is this date CORRECT for the first marketing-authorisation approval of this drug in this jurisdiction?

Rules:
- Respond ONLY with a JSON object.
- Format: {{"confirmed": true/false, "corrected_date": "YYYY-MM-DD or null", "reason": "brief"}}
- If you are not sure, set confirmed to false and corrected_date to null.
"""


def marketed_jurisdictions(df: pd.DataFrame) -> set:
    """The jurisdictions in which the drug is Marketed: those with a glp_master_loe row whose Phase is
    Marketed. An approval date exists only there, so only there is it looked up."""
    if "Phase" not in df.columns:
        logger.warning("[APPROVAL] 'Phase' not found in the master rows — no jurisdiction counts as marketed")
        return set()
    marketed = df["Phase"].map(norm_phase) == "approved/marketed"
    return set(df.loc[marketed, "Jurisdiction"].dropna().str.strip())


def _ask(prompt: str) -> dict | None:
    """One Gemini JSON call; None if it fails or does not return valid JSON."""
    try:
        return gemini_json_call(prompt, config.GEMINI_TEXT_MODEL)
    except Exception as exc:
        logger.warning("[APPROVAL] Gemini lookup failed: %s", exc)
        return None


def lookup_earliest_approval_date(drug_name: str, jurisdiction: str) -> str:
    """The earliest approval date of the drug in the jurisdiction ('YYYY-MM-DD'), or 'Not found'.
    Blocking — run it in a thread."""
    jurisdiction_name = JURISDICTION_FULL.get(jurisdiction, jurisdiction)

    found = _ask(FIND_PROMPT.format(drug=drug_name, jurisdiction=jurisdiction_name))
    dates = [pd.to_datetime(d, errors="coerce") for d in (found or {}).get("dates", [])]
    dates = [d for d in dates if pd.notna(d)]
    if not dates:
        return NOT_FOUND
    earliest = min(dates).strftime("%Y-%m-%d")

    check = _ask(VERIFY_PROMPT.format(drug=drug_name, jurisdiction=jurisdiction_name, earliest=earliest)) or {}
    if check.get("confirmed"):
        return earliest
    corrected = pd.to_datetime(check.get("corrected_date"), errors="coerce")
    return corrected.strftime("%Y-%m-%d") if pd.notna(corrected) else earliest


async def fill_approval_dates(shortlisted: pd.DataFrame, drug: str, cached: dict, marketed: set) -> None:
    """Fill the missing Approval Dates in place: from the cache, else from Gemini (per jurisdiction).
    Only jurisdictions in which the drug is marketed are looked up; the others keep no approval date."""
    approvals = dict(cached)
    missing = {
        row["Jurisdiction"] for _, row in shortlisted.iterrows()
        if row["Jurisdiction"] in marketed and pd.isna(pd.to_datetime(row.get("Approval Date"), errors="coerce"))
    }
    skipped = sorted(set(shortlisted["Jurisdiction"]) - marketed)
    if skipped:
        logger.info("[LAUNCH] Drug not marketed in %s — approval dates not looked up", skipped)
    to_query = {(drug, jurisdiction) for jurisdiction in missing if jurisdiction not in approvals}
    logger.info("[LAUNCH] Approval-date lookups needed: %d (reused %d from the previous run)",
                len(to_query), len(missing) - len(to_query))

    for (_, jurisdiction), date_found in (await run_lookups(to_query, lookup_earliest_approval_date)).items():
        approvals[jurisdiction] = date_found
        logger.info("[LAUNCH] %s / %s approval -> %s", drug, jurisdiction, date_found)

    for idx, row in shortlisted.iterrows():
        if row["Jurisdiction"] in approvals and row["Jurisdiction"] in missing:
            shortlisted.at[idx, "Approval Date"] = approvals[row["Jurisdiction"]]
