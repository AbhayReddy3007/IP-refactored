"""
phase_utils.py
──────────────
Phase / date normalisation and the approval-date fetch.
"""

import re
from typing import Optional
import pandas as pd
from ...score_calculation import approval_date_fetcher as _fetcher_mod


# ─────────────────────────────────────────────
# Phase normalisation
# ─────────────────────────────────────────────

def _normalise_phase(raw) -> Optional[str]:
    """Normalises a phase string: 3a→Phase 3, III→Phase 3, Marketed, etc."""
    s = str(raw).strip()
    if not s or s.lower() in ("nan", "none", ""):
        return None

    # Replace pipe characters used as roman numerals
    s = re.sub(r'\|\|\|', 'III', s)
    s = re.sub(r'\|\|',   'II',  s)
    s = re.sub(r'\|',     'I',   s)

    low = s.lower()

    if low in ("marketed", "launched", "approved", "registered"):
        return "Marketed"
    if low in ("pre-registration", "preregistration", "pre registration", "nda/bla", "nda", "bla"):
        return "Pre-registration"
    if low in ("preclinical", "pre-clinical", "discovery"):
        return "Preclinical"

    cleaned = re.sub(r"^phase[\s\-]*", "", low).strip()
    _roman = {"i": 1, "ii": 2, "iii": 3, "iv": 4}
    roman_match = re.match(r"^(i{1,3}v?)\s*[a-z]?\s*$", cleaned, re.IGNORECASE)
    if roman_match:
        num = _roman.get(roman_match.group(1).lower())
        if num:
            return f"Phase {num}"

    num_match = re.match(r"^(\d)", cleaned)
    if num_match:
        return f"Phase {num_match.group(1)}"

    return None


def _parse_date_or_year(val):
    """Parses a date or year-only value (2008 → Jan 1 2008)."""
    if pd.isna(val):
        return pd.NaT
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none", ""):
        return pd.NaT
    try:
        year = int(float(s))
        if 1900 <= year <= 2100:
            return pd.Timestamp(year=year, month=1, day=1)
    except (ValueError, TypeError):
        pass
    return pd.to_datetime(s, errors="coerce", dayfirst=True)


# ─────────────────────────────────────────────
# Approval date fetcher
# ─────────────────────────────────────────────

_raw_fetch = _fetcher_mod.fetch_approval_dates


async def _fetch_approval_for_molecules(molecules: list) -> dict:
    results = {}
    for mol in molecules:
        print(f"  [APPROVAL] Fetching for '{mol}'...")
        try:
            approval = await _raw_fetch(drug_name=mol, bq_companies=[], bq_brands=[], fetch_us=True, fetch_eu=True)
            results[mol] = {"US": approval.get("US", {}).get("date"), "EU": approval.get("EU", {}).get("date")}
        except Exception as e:
            print(f"  [APPROVAL] Failed for '{mol}': {e}")
            results[mol] = {"US": None, "EU": None}
    return results
