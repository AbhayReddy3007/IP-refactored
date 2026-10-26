"""
utils.py
────────
Pure helper functions shared across the whole IP_refactored package:
name/text normalisation, DataFrame column lookup, jurisdiction tokens,
date parsing, the score bucketing, BigQuery-safe naming, and logging setup.

Nothing here talks to Google Cloud — GCP I/O belongs in gcp_utils.py.
"""

import logging
import re
import sys
from datetime import datetime
from typing import Iterable, Optional, Set

import pandas as pd

from . import config


# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────

def setup_logging(level: int = logging.INFO) -> None:
    """Send log records to stdout so they interleave with print() output
    (Cloud Run and a VM terminal both capture stdout)."""
    logging.basicConfig(
        level=level,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        force=True,
    )


# ─────────────────────────────────────────────
# Name / text normalisation
# ─────────────────────────────────────────────

def normalize(name: str) -> str:
    """Lowercase, strip, collapse spaces/hyphens/underscores for fuzzy matching."""
    return re.sub(r"[\s\-_]+", "", str(name or "").lower().strip())


def normalize_patent_number(value: str) -> str:
    """Strip everything but letters/digits and uppercase, so a BigQuery
    patent_number matches a GCS filename stem regardless of punctuation
    (e.g. 'US-1,234,567-B2' == 'US1234567B2.pdf')."""
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def normalize_drug_str(s) -> str:
    """Lowercase, strip, and collapse whitespace around + / & so combination
    drug names normalise consistently regardless of spacing."""
    if is_blank(s):
        return ""
    s = str(s).strip().lower()
    s = re.sub(r"\s*([+/&])\s*", r"\1", s)
    return re.sub(r"\s+", " ", s)


def safe_name(drug_name: str, lowercase: bool = False) -> str:
    """GCS / filesystem-safe drug name. Keeps '+' (combination drugs) and
    replaces only characters that are unsafe in paths: / \\ < > : " | ? *"""
    s = str(drug_name or "").strip()
    s = re.sub(r'[/\\<>:"|?*\x00]', "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s.lower() if lowercase else s


def norm_col(col) -> str:
    """Collapse a column name to just lowercase letters/digits."""
    return re.sub(r"[^a-z0-9]", "", str(col).lower())


def norm_ws(s) -> str:
    """Lowercase, strip, and collapse whitespace for loose text matching."""
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return ""
    return re.sub(r"\s+", " ", str(s).strip().lower())


def is_blank(val) -> bool:
    if val is None or val is pd.NA or (isinstance(val, float) and pd.isna(val)):
        return True
    return str(val).strip().upper() in ("", "N/A", "NULL", "NONE", "NAN", "NAT", "<NA>")


# ─────────────────────────────────────────────
# Clinical phase normalisation
# ─────────────────────────────────────────────

_ROMAN_PHASE_NUMERALS = {"i": 1, "ii": 2, "iii": 3, "iv": 4}


def norm_phase(s) -> str:
    """Normalise any phase spelling to a canonical lowercase form:
      'Phase 3', 'P3', '3b', '2/3', 'Phase III', 3, 3.0 -> 'phase 3'
      'Approved', 'Marketed'                            -> 'approved/marketed'
      'Pre-Registration', 'preregistration'             -> 'pre-registration'
      'Preclinical', 'Discovery'                        -> 'preclinical'
    Returns '' for blank input; unrecognised text is returned normalised."""
    if isinstance(s, float):
        if pd.isna(s):
            return ""
        s = str(int(s)) if s == int(s) else str(s)
    elif isinstance(s, int):
        s = str(s)

    s = norm_ws(s)
    if not s:
        return ""
    if "approv" in s or "market" in s:
        return "approved/marketed"
    if "pre" in s and "regist" in s:
        return "pre-registration"
    if "preclin" in s or "discovery" in s:
        return "preclinical"

    digit_parts = re.findall(r"\d+", s)
    if digit_parts:
        return f"phase {max(int(p) for p in digit_parts)}"

    for token in re.split(r"[^a-z0-9]+", s):
        if token in _ROMAN_PHASE_NUMERALS:
            return f"phase {_ROMAN_PHASE_NUMERALS[token]}"
    stripped = re.sub(r"^(phase|ph|p)", "", re.sub(r"[^a-z0-9]", "", s))
    if stripped in _ROMAN_PHASE_NUMERALS:
        return f"phase {_ROMAN_PHASE_NUMERALS[stripped]}"
    return s


# ─────────────────────────────────────────────
# DataFrame column lookup
# ─────────────────────────────────────────────

def find_col(df: pd.DataFrame, required_tokens: Iterable[str],
             prefer_tokens: Optional[Iterable[str]] = None) -> Optional[str]:
    """First column whose normalised name contains all required_tokens,
    preferring one that also contains all prefer_tokens."""
    candidates = [c for c in df.columns if all(t in norm_col(c) for t in required_tokens)]
    if not candidates:
        return None
    if prefer_tokens:
        preferred = [c for c in candidates if all(t in norm_col(c) for t in prefer_tokens)]
        if preferred:
            return preferred[0]
    return candidates[0]


# ─────────────────────────────────────────────
# Jurisdiction tokens
# ─────────────────────────────────────────────
# Canonical tokens: "US", "EP" (Europe), or a two-letter country code.

COUNTRY_CODE_ALIASES = {
    "CN": "CN", "CHINA": "CN",
    "AU": "AU", "AUSTRALIA": "AU",
    "BR": "BR", "BRAZIL": "BR",
    "CA": "CA", "CANADA": "CA",
    "ES": "ES", "SPAIN": "ES",
    "IN": "IN", "INDIA": "IN",
    "JP": "JP", "JAPAN": "JP",
    "KR": "KR", "SOUTH KOREA": "KR", "KOREA": "KR",
    "MX": "MX", "MEXICO": "MX",
    "PL": "PL", "POLAND": "PL",
    "RU": "RU", "RUSSIA": "RU",
    "TW": "TW", "TAIWAN": "TW",
    "NL": "NL", "NETHERLANDS": "NL",
}
US_NAMES = {"US", "USA", "U.S.", "U.S.A.", "UNITED STATES", "UNITED STATES OF AMERICA", "UNITES STATES"}
EU_NAMES = {"EU", "EP", "EUROPE", "EUROPEAN UNION"}
ALL_JURISDICTION_TOKENS = {"US", "EP"} | set(COUNTRY_CODE_ALIASES.values())
_GLOBAL_NAMES = {"GLOBAL", "WORLDWIDE"}


def jurisdiction_code(value) -> Optional[str]:
    """Map a jurisdiction/country string to 'US', 'EP', a country code, or None."""
    j = re.sub(r"\s+", " ", str(value or "").strip()).upper()
    if not j:
        return None
    if j in US_NAMES:
        return "US"
    if j in EU_NAMES:
        return "EP"
    return COUNTRY_CODE_ALIASES.get(j)


def split_jurisdiction_tokens(value, separators: str = r"[,;/]+") -> Set[str]:
    """Split a geography/location string ('US, Japan', 'US/EU', 'Global')
    into canonical jurisdiction tokens. 'Global'/'Worldwide' expands to all."""
    if is_blank(value):
        return set()
    raw = str(value).strip().strip("'\"").strip()
    if raw.upper() in _GLOBAL_NAMES:
        return set(ALL_JURISDICTION_TOKENS)
    tokens: Set[str] = set()
    for part in re.split(separators, raw):
        p = re.sub(r"\s+", " ", part.strip().strip("'\"").strip()).upper()
        if p in _GLOBAL_NAMES:
            tokens |= ALL_JURISDICTION_TOKENS
        elif p:
            code = jurisdiction_code(p)
            if code:
                tokens.add(code)
    return tokens


def extract_jurisdiction(patent_number_hint: str) -> str:
    """Best-effort 2-letter jurisdiction code from a patent number / filename stem."""
    match = re.search(r"\b([A-Z]{2})\d", patent_number_hint.upper())
    if match:
        return match.group(1)
    fallback = re.match(r"^([A-Z]{2})", patent_number_hint.upper())
    return fallback.group(1) if fallback else "UN"


# ─────────────────────────────────────────────
# Dates
# ─────────────────────────────────────────────

_DMY_INPUT_FORMATS = (
    "%Y%m%d", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y",
    "%d %B %Y", "%B %d, %Y", "%B %d %Y", "%d-%b-%Y",
    "%b %d, %Y", "%b. %d, %Y",
)
_YEAR_INPUT_FORMATS = (
    "%Y-%m-%d", "%d-%b-%Y", "%d/%m/%Y", "%m/%d/%Y",
    "%B %d, %Y", "%B %d %Y", "%d %B %Y",
)


def format_date_dmy(raw) -> Optional[str]:
    """Normalise a date to DD-MMM-YYYY (e.g. 05-Dec-2017). Unparseable
    values are returned unchanged; blanks return None."""
    if is_blank(raw):
        return None
    raw = str(raw).strip()
    for fmt in _DMY_INPUT_FORMATS:
        try:
            return datetime.strptime(raw, fmt).strftime("%d-%b-%Y")
        except ValueError:
            continue
    return raw


def year_from_value(val) -> Optional[float]:
    """Calendar year from a date-like cell (Timestamp, ISO date, '05-Dec-2017', '2031')."""
    if is_blank(val):
        return None
    if isinstance(val, (pd.Timestamp, datetime)):
        return float(val.year)
    s = str(val).strip()
    for fmt in _YEAR_INPUT_FORMATS:
        try:
            return float(datetime.strptime(s, fmt).year)
        except ValueError:
            continue
    m = re.search(r"(\d{4})", s)
    return float(m.group(1)) if m else None


# ─────────────────────────────────────────────
# IP Dimension 1 score
# ─────────────────────────────────────────────

def ip_dimension_score(avg_years: float) -> int:
    """Bucket average years to entry into the 1–5 IP Dimension 1 Score
    (config.SCORE_THRESHOLD_*). Fewer years until entry -> higher score."""
    if avg_years <= config.SCORE_THRESHOLD_5:
        return 5
    if avg_years <= config.SCORE_THRESHOLD_4:
        return 4
    if avg_years <= config.SCORE_THRESHOLD_3:
        return 3
    if avg_years <= config.SCORE_THRESHOLD_2:
        return 2
    return 1


# ─────────────────────────────────────────────
# BigQuery naming / cell coercion
# ─────────────────────────────────────────────

def bq_safe_name(name: str) -> str:
    """'PTE (months)' -> 'PTE_months', 'Est. Approval Year' -> 'Est_Approval_Year'."""
    s = re.sub(r"[^A-Za-z0-9]+", "_", str(name)).strip("_") or "col"
    return f"_{s}" if s[0].isdigit() else s


def stringify_cell(v) -> Optional[str]:
    """Convert one cell to a plain string (or None) for a STRING column."""
    if is_blank(v):
        return None
    if isinstance(v, (pd.Timestamp, datetime)):
        return v.strftime("%Y-%m-%d")
    return str(v).strip()
