"""
phase_fetcher.py
─────────────────
Handles:
  - Fetching clinical development stage from BigQuery (clinical_efficacy table)
  - Fetching clinical development stage from BigQuery (drug-details/"drug
    list" table — the same view chunking/drug_list.py resolves the GLP-1
    drug list from)
  - Merging clinical_efficacy + drug_details stages per jurisdiction
    (US / EP / JP / CN / KR / IN / AU / CA / BR / MX / TW / RU / PL / NL / ES)
  - Assigning phase_at_filing to each patent dict

No local Excel fallback — every phase comes from BigQuery
(clinical_efficacy + the drug-list/drug_details view) only.

Phase normalisation and jurisdiction/geography matching (roman numerals,
combined phases like "2/3", country-name <-> country-code aliases, the
Drug_Geo_New-with-Drug_Geography-fallback trick, and "pick the single
highest-priority phase pooled across BOTH sources" merge rule) follow the
same logic used in the reference combine_master_loe.py script's
_norm_phase() / _jurisdiction_token() / _location_tokens() /
_vwd_geo_tokens() / _vwd_geo_tokens_combined() / _pick_best_phase_multi().
"""

import asyncio
import os
import re
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd
from google.cloud import bigquery
from google.oauth2 import service_account

from .. import config
from .. import gcp_utils

# ─────────────────────────────────────────────
# Config — sourced from IP_refactored.config (single source of truth)
# ─────────────────────────────────────────────

BQ_PROJECT_ID         = config.BQ_PROJECT_ID
BQ_DATASET_ID         = config.BQ_DATASET_ID
BQ_TABLE_NAME         = config.CLINICAL_EFFICACY_TABLE
BQ_DRUG_DETAILS_TABLE = config.DRUG_DETAILS_TABLE
BQ_SERVICE_ACCOUNT    = config.GOOGLE_APPLICATION_CREDENTIALS

_TIMELINE_STAGES = [
    "Preclinical", "Phase 1", "Phase 2", "Phase 3", "Pre-registration", "Marketed"
]
_STAGE_RANK: Dict[str, int] = {s: i for i, s in enumerate(_TIMELINE_STAGES)}


def _resolve_fq_table(table_name: str, project_id: str, dataset_id: str) -> str:
    """If table_name is already fully-qualified ("project.dataset.table" —
    2 dots), use it as-is (e.g. CLINICAL_EFFICACY_TABLE =
    "cognito-dev-380506.data_mart.clinical_efficacy_glp1", a different GCP
    project from the rest of the pipeline). Otherwise treat it as a bare
    table/view name under project_id.dataset_id."""
    if table_name.count(".") == 2:
        return table_name
    return f"{project_id}.{dataset_id}.{table_name}"


def _clinical_project_dataset() -> Tuple[str, str]:
    """Project/dataset that "the drug list table" (BQ_DRUG_DETAILS_TABLE,
    e.g. vw_drug_details_full) should be looked up in: the SAME
    project/dataset as BQ_TABLE_NAME (clinical_efficacy) whenever that's
    fully-qualified, since the reference logic keeps both tables side by
    side in one dataset (cognito-dev-380506.data_mart). Falls back to
    BQ_PROJECT_ID/BQ_DATASET_ID if BQ_TABLE_NAME is just a bare name."""
    parts = BQ_TABLE_NAME.split(".")
    if len(parts) == 3:
        return parts[0], parts[1]
    return BQ_PROJECT_ID, BQ_DATASET_ID


print(f"[BQ] Config loaded: clinical_efficacy={_resolve_fq_table(BQ_TABLE_NAME, BQ_PROJECT_ID, BQ_DATASET_ID)} "
      f"| drug_details={_resolve_fq_table(BQ_DRUG_DETAILS_TABLE, *_clinical_project_dataset())}")


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _normalize(name: str) -> str:
    return re.sub(r"[\s\-_]+", "", str(name or "").lower().strip())


# ─────────────────────────────────────────────
# Drug alias map
# ─────────────────────────────────────────────
# Maps known salt forms / aliases to a single canonical INN name.
# Used by canonicalise_drug_name() which is called at the top of the
# pipeline so every downstream lookup uses the same canonical name.

_DRUG_ALIASES: Dict[str, str] = {
    "aleniglipron l-arginine": "aleniglipron",
    "aleniglipron l arginine": "aleniglipron",
    "aleniglipronlarginine":   "aleniglipron",   # normalised form
}

def canonicalise_drug_name(drug_name: str) -> str:
    """
    Returns the canonical INN for a drug name, resolving known salt forms
    and aliases to a single name.

    Steps:
      1. Exact lowercase match in alias map
      2. Normalised match (strip spaces/hyphens/underscores)
      3. No match -> return original name unchanged (preserving original casing)
    """
    lower = drug_name.strip().lower()

    # Exact lowercase match
    if lower in _DRUG_ALIASES:
        canonical = _DRUG_ALIASES[lower]
        print(f"[ALIAS] '{drug_name}' -> '{canonical}' (exact alias)")
        return canonical

    # Normalised match
    norm = _normalize(drug_name)
    for alias_key, canonical in _DRUG_ALIASES.items():
        if _normalize(alias_key) == norm:
            print(f"[ALIAS] '{drug_name}' -> '{canonical}' (normalised alias)")
            return canonical

    return drug_name


# ─────────────────────────────────────────────
# Phase normalisation — same logic as the reference script's _norm_phase()
# ─────────────────────────────────────────────

_ROMAN_PHASE_NUMERALS = {"i": 1, "ii": 2, "iii": 3, "iv": 4}

# canonical (lowercase, as produced by _norm_phase) -> internal timeline label
_NORM_TO_TIMELINE: Dict[str, str] = {
    "preclinical":       "Preclinical",
    "phase 1":           "Phase 1",
    "phase 2":           "Phase 2",
    "phase 3":           "Phase 3",
    "phase 4":           "Marketed",
    "pre-registration":  "Pre-registration",
    "approved/marketed": "Marketed",
}


def _norm_phase(s) -> str:
    """Normalise any phase spelling down to a canonical form:
      'Phase 3', 'Phase3', 'phase  3', '3', 'P3', 'PHASE-3'  -> 'phase 3'
      '3b', '3a', '3B'                                        -> 'phase 3'
      '2/3'  (combined phase)                                 -> 'phase 3' (highest)
      'Phase III', 'PhaseIII', 'phase iii'                   -> 'phase 3'
      3   (integer from BQ)                                   -> 'phase 3'
      3.0 (float from BQ)                                     -> 'phase 3'
      4, 'Phase 4'                                            -> 'phase 4' (-> Marketed)
      'Approved', 'Marketed', 'Approved/Marketed'            -> 'approved/marketed'
      'Pre-Registration', 'pre registration', 'preregistration' -> 'pre-registration'
      'Preclinical', 'Discovery'                              -> 'preclinical'
    Case-insensitive and whitespace/punctuation-insensitive throughout.
    Recognises both Arabic digits and roman numerals (I/II/III/IV).
    Returns '' if the input is blank/unrecognisable."""
    if isinstance(s, float):
        if pd.isna(s):
            return ""
        s = str(int(s)) if s == int(s) else str(s)
    elif isinstance(s, int):
        s = str(s)

    s = re.sub(r"\s+", " ", str(s or "").strip().lower())
    if not s:
        return ""
    if "approv" in s or "market" in s:
        return "approved/marketed"
    if "pre" in s and "regist" in s:
        return "pre-registration"
    if "preclin" in s or "discovery" in s:
        return "preclinical"

    # Combined-phase values like "2/3" or "2-3": split on non-digit
    # separators and take the highest digit present so "2/3" -> "phase 3".
    digit_parts = re.findall(r"\d+", s)
    if digit_parts:
        best = max(int(p) for p in digit_parts)
        return f"phase {best}"

    # No Arabic digit found — try a roman numeral instead, e.g. "Phase II",
    # "PhaseIII", "phase iii". Word-by-word first (whitespace/punctuation
    # already separates the numeral)...
    for token in re.split(r"[^a-z0-9]+", s):
        if token in _ROMAN_PHASE_NUMERALS:
            return f"phase {_ROMAN_PHASE_NUMERALS[token]}"
    # ...then strip a "phase"/"ph"/"p" prefix off the compact string, e.g.
    # "PhaseIII" -> "phaseiii" -> strip "phase" -> "iii".
    compact = re.sub(r"[^a-z0-9]", "", s)
    stripped = re.sub(r"^(phase|ph|p)", "", compact)
    if stripped in _ROMAN_PHASE_NUMERALS:
        return f"phase {_ROMAN_PHASE_NUMERALS[stripped]}"
    return s


def _normalize_phase(p: Optional[str]) -> Optional[str]:
    """Canonicalise a phase string to an internal timeline label (e.g.
    'Phase III' -> 'Phase 3'), via _norm_phase(). Returns None for falsy /
    truly unrecognisable input (kept for backward compatibility with
    callers that expect a timeline label, not the lowercase _norm_phase form)."""
    if p is None:
        return None
    norm = _norm_phase(p)
    if not norm:
        return None
    return _NORM_TO_TIMELINE.get(norm, str(p).strip())


def _highest_phase(a: Optional[str], b: Optional[str]) -> Optional[str]:
    """Returns whichever phase is further along. None is lower than any real stage.

    Both inputs are normalised first (Roman numerals, varying case/spacing,
    combined phases) so e.g. 'Phase III' and '2/3' compare correctly.
    """
    a = _normalize_phase(a)
    b = _normalize_phase(b)
    if a is None:
        return b
    if b is None:
        return a
    return a if _STAGE_RANK.get(a, -1) >= _STAGE_RANK.get(b, -1) else b


# Priority order used when pooling phases from BOTH sources for one
# drug+jurisdiction (same shape as the reference script's
# _PHASE_PRIORITY_ORDER, extended to the full 6-stage timeline since patent
# phase-at-filing cares about Preclinical/Phase 1 too, not just the phases
# that have an Est.-Approval-Year x-value in the reference script).
_PHASE_PRIORITY_ORDER = [_NORM_TO_TIMELINE[k] for k in
                         ("approved/marketed", "pre-registration", "phase 3", "phase 2", "phase 1", "preclinical")]


def _pick_best_phase_multi(clin_phases: Set[str], vwd_phases: Set[str]) -> Tuple[Optional[str], Optional[str]]:
    """Pool ALL phases (already normalised to internal timeline labels)
    present in clinical_efficacy and drug_details for one drug+jurisdiction,
    pick the single highest-priority one across both combined, then report
    which source(s) have it. 'clinical' is preferred as the source label
    when both have it (same tie-break as the reference script).

    Returns (best_phase, source) or (None, None) if neither set has
    anything recognised."""
    all_phases = {p for p in (clin_phases | vwd_phases) if p}
    if not all_phases:
        return None, None
    for phase in _PHASE_PRIORITY_ORDER:
        if phase in all_phases:
            source = "clinical" if phase in clin_phases else "vwd"
            return phase, source
    return None, None


# ─────────────────────────────────────────────
# Jurisdiction / geography token matching — same logic as the reference
# script's _jurisdiction_token() / _location_tokens() / _vwd_geo_tokens() /
# _vwd_geo_tokens_combined()
# ─────────────────────────────────────────────

# Two-letter country code <-> full name synonyms for countries beyond US/EU
# that show up in trial_location / Drug_Geo_New. Canonical form is the code.
_COUNTRY_CODE_ALIASES = {
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

_US_NAMES = {"US", "USA", "U.S.", "U.S.A.", "UNITED STATES", "UNITED STATES OF AMERICA", "UNITES STATES"}
_EU_NAMES = {"EU", "EP", "EUROPE", "EUROPEAN UNION"}

ALL_JURISDICTION_TOKENS = {"US", "EP"} | set(_COUNTRY_CODE_ALIASES.values())


def _jurisdiction_token(jurisdiction) -> Optional[str]:
    """Map a single jurisdiction/country string to its canonical token
    ('US', 'EP', or a country code like 'CN'/'AU'/...). Returns None for an
    unrecognised value."""
    j = re.sub(r"\s+", " ", str(jurisdiction or "").strip()).upper()
    if not j:
        return None
    if j in _US_NAMES:
        return "US"
    if j in _EU_NAMES:
        return "EP"
    if j in _COUNTRY_CODE_ALIASES:
        return _COUNTRY_CODE_ALIASES[j]
    return None


def _split_tokens(value) -> Set[str]:
    """Split a comma/semicolon/slash-separated geography/location string
    into a set of canonical jurisdiction tokens. 'Global' (or 'Worldwide')
    expands to every known token."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return set()
    raw = str(value).strip().strip("'\"").strip()
    if not raw:
        return set()
    if raw.upper() in ("GLOBAL", "WORLDWIDE"):
        return set(ALL_JURISDICTION_TOKENS)

    tokens: Set[str] = set()
    for part in re.split(r"[,;/]+", raw):
        p = re.sub(r"\s+", " ", part.strip().strip("'\"").strip()).upper()
        if not p:
            continue
        if p in ("GLOBAL", "WORLDWIDE"):
            tokens |= set(ALL_JURISDICTION_TOKENS)
            continue
        token = _jurisdiction_token(p)
        if token:
            tokens.add(token)
    return tokens


def _location_tokens(location) -> Set[str]:
    """Jurisdiction tokens present in a clinical_efficacy trial_location
    value, e.g. 'US/EU', 'United States, China', 'Global'."""
    return _split_tokens(location)


def _vwd_geo_tokens(geo) -> Set[str]:
    """Jurisdiction tokens present in a drug_details Drug_Geo_New (or
    Drug_Geography) value, e.g. 'US, Germany, Japan'."""
    return _split_tokens(geo)


def _vwd_geo_tokens_combined(geo_new, geo_fallback) -> Set[str]:
    """Same jurisdiction tokens as _vwd_geo_tokens, preferring Drug_Geo_New
    but falling back to Drug_Geography whenever Drug_Geo_New produces NO
    tokens at all for this row (Drug_Geo_New has been observed blank for
    some single-jurisdiction drugs despite Drug_Geography holding a valid
    value)."""
    tokens = _vwd_geo_tokens(geo_new)
    if tokens:
        return tokens
    return _vwd_geo_tokens(geo_fallback)


# ─────────────────────────────────────────────
# BigQuery fetch — clinical_efficacy
# ─────────────────────────────────────────────

def _get_bq_client(project_id: str, service_account_path: Optional[str]) -> bigquery.Client:
    if service_account_path and os.path.exists(service_account_path):
        credentials = service_account.Credentials.from_service_account_file(
            service_account_path,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        return bigquery.Client(credentials=credentials, project=project_id)
    return bigquery.Client(project=project_id)


def import_from_gbq(
    drug_name:            str,
    table_name:           str,
    project_id:           str,
    dataset_id:           str,
    service_account_path: Optional[str] = None,
) -> pd.DataFrame:
    """
    Fetches raw clinical phase rows for `drug_name` from the
    clinical_efficacy table: molecule_name, trial_location, phase,
    phase_status (the reference script's status_col — NOT "trial_status",
    which was the old clinical_efficacy table's column name; the new
    clinical_efficacy_glp1 table uses phase_status). No phase bucketing
    happens in SQL — phase
    normalisation and jurisdiction splitting are done in Python via
    _norm_phase() / _location_tokens(), same as the reference script, so
    the exact same string forms (roman numerals, combined "2/3" phases,
    "Global" locations, etc.) are handled consistently with drug_details.
    """
    try:
        client = _get_bq_client(project_id, service_account_path)
        fq_table = _resolve_fq_table(table_name, project_id, dataset_id)

        query = f"""
        SELECT
            molecule_name,
            trial_location,
            phase,
            phase_status
        FROM `{fq_table}`
        WHERE LOWER(REGEXP_REPLACE(COALESCE(molecule_name, ''), r'[\\s\\-_]+', ''))
              = LOWER(REGEXP_REPLACE(@drug_name, r'[\\s\\-_]+', ''))
        """

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("drug_name", "STRING", drug_name),
            ]
        )

        df = client.query(query, job_config=job_config).to_dataframe()
        print(f"[BQ] clinical_efficacy: fetched {len(df)} raw row(s) for '{drug_name}' from {fq_table}")
        for _, row in df.head(5).iterrows():
            print(f"[BQ]   {row.get('molecule_name')} | {row.get('trial_location')} | {row.get('phase')}")
        return df

    except Exception as e:
        print(f"[BQ] clinical_efficacy query failed: {e}")
        return pd.DataFrame()


def _phases_by_jurisdiction_clinical(df: pd.DataFrame) -> Tuple[Dict[str, Set[str]], Dict[str, str]]:
    """From raw clinical_efficacy rows (already filtered to one drug),
    build {jurisdiction_token: {normalised_phase, ...}} plus a best
    phase_status per jurisdiction (first non-blank one seen for that
    jurisdiction's winning phase, informational only)."""
    phases_by_jur: Dict[str, Set[str]] = {}
    status_by_jur: Dict[str, str] = {}
    if df.empty or "phase" not in df.columns:
        return phases_by_jur, status_by_jur

    for _, row in df.iterrows():
        phase_label = _normalize_phase(row.get("phase"))
        if phase_label is None:
            continue
        status = str(row.get("phase_status") or "").strip()
        tokens = _location_tokens(row.get("trial_location"))
        if not tokens:
            continue  # unknown/blank location — contributes to overall fallback separately
        for jur in tokens:
            phases_by_jur.setdefault(jur, set()).add(phase_label)
            if status and status.lower() not in ("", "nan", "none"):
                # Keep the status associated with the highest-ranked phase seen so far.
                existing_rank = _STAGE_RANK.get(status_by_jur.get(jur, ""), -1)
                if _STAGE_RANK.get(phase_label, -1) >= 0:
                    status_by_jur[jur] = status
    return phases_by_jur, status_by_jur


def _overall_phase_clinical(df: pd.DataFrame) -> Optional[str]:
    """Highest phase across ALL rows for this drug, regardless of location
    — used as the US/EP fallback when nothing resolves a jurisdiction."""
    if df.empty or "phase" not in df.columns:
        return None
    best = None
    for raw in df["phase"].dropna():
        best = _highest_phase(best, _normalize_phase(raw))
    return best


# ─────────────────────────────────────────────
# BigQuery fetch — drug_details ("the drug list table")
# ─────────────────────────────────────────────

def _fetch_drug_details_df(
    drug_name:  str,
    project_id: str,
    dataset_id: str,
    sa_path:    Optional[str] = None,
) -> pd.DataFrame:
    """Fetches raw rows for `drug_name` from BQ_DRUG_DETAILS_TABLE (the
    "drug list" view, e.g. vw_drug_details_full): Cleaned_Generic_Name,
    Highest_Development_Stage, Drug_Geo_New, Drug_Geography. Column names
    are matched case-insensitively against whatever the view actually
    returns, since vw_drug_details_full's casing can vary."""
    try:
        client = _get_bq_client(project_id, sa_path)
        fq_table = _resolve_fq_table(BQ_DRUG_DETAILS_TABLE, project_id, dataset_id)

        query = f"""
        SELECT *
        FROM `{fq_table}`
        WHERE LOWER(REGEXP_REPLACE(COALESCE(cleaned_generic_name, ''), r'[\\s\\-_]+', ''))
              = LOWER(REGEXP_REPLACE(@drug_name, r'[\\s\\-_]+', ''))
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("drug_name", "STRING", drug_name),
            ]
        )
        df = client.query(query, job_config=job_config).to_dataframe()
        print(f"[DRUG_DETAILS] {fq_table}: fetched {len(df)} row(s) for '{drug_name}'")
        return df
    except Exception as e:
        print(f"[DRUG_DETAILS] Query failed for '{drug_name}': {e}")
        return pd.DataFrame()


def _find_col(df: pd.DataFrame, required_tokens) -> Optional[str]:
    """Find the first column whose normalised (letters/digits only,
    lowercased) name contains all required_tokens."""
    def norm(c): return re.sub(r"[^a-z0-9]", "", str(c).lower())
    for c in df.columns:
        if all(t in norm(c) for t in required_tokens):
            return c
    return None


def _phases_by_jurisdiction_vwd(df: pd.DataFrame) -> Dict[str, Set[str]]:
    """From raw drug_details rows (already filtered to one drug), build
    {jurisdiction_token: {normalised_phase, ...}}, preferring Drug_Geo_New
    and falling back to Drug_Geography per-row (_vwd_geo_tokens_combined)."""
    phases_by_jur: Dict[str, Set[str]] = {}
    if df.empty:
        return phases_by_jur

    phase_col = _find_col(df, ["highest", "development", "stage"]) or _find_col(df, ["development", "stage"])
    geo_col = _find_col(df, ["drug", "geo", "new"]) or _find_col(df, ["drug", "geo"]) or _find_col(df, ["geo"])
    geo_fallback_col = _find_col(df, ["drug", "geography"])

    if not phase_col:
        print("[DRUG_DETAILS] No Highest_Development_Stage-like column found — skipping.")
        return phases_by_jur
    if not geo_col:
        print("[DRUG_DETAILS] No Drug_Geo_New-like column found — skipping.")
        return phases_by_jur

    for _, row in df.iterrows():
        phase_label = _normalize_phase(row.get(phase_col))
        if phase_label is None:
            continue
        geo_new = row.get(geo_col)
        geo_fallback = row.get(geo_fallback_col) if geo_fallback_col else None
        tokens = _vwd_geo_tokens_combined(geo_new, geo_fallback)
        for jur in tokens:
            phases_by_jur.setdefault(jur, set()).add(phase_label)
    return phases_by_jur


def _overall_phase_vwd(df: pd.DataFrame) -> Optional[str]:
    """Highest phase across ALL rows for this drug, regardless of geography."""
    if df.empty:
        return None
    phase_col = _find_col(df, ["highest", "development", "stage"]) or _find_col(df, ["development", "stage"])
    if not phase_col:
        return None
    best = None
    for raw in df[phase_col].dropna():
        best = _highest_phase(best, _normalize_phase(raw))
    return best


# Kept for backward compatibility with any external caller that imported
# the old per-source dict-returning function directly.
def _fetch_from_drug_details(
    drug_name:  str,
    project_id: str,
    dataset_id: str,
    sa_path:    Optional[str] = None,
) -> Dict[str, Optional[str]]:
    df = _fetch_drug_details_df(drug_name, project_id, dataset_id, sa_path)
    phases_by_jur = _phases_by_jurisdiction_vwd(df)
    result: Dict[str, Optional[str]] = {}
    for jur, phases in phases_by_jur.items():
        best = None
        for p in phases:
            best = _highest_phase(best, p)
        result[jur] = best
    if not result:
        overall = _overall_phase_vwd(df)
        if overall:
            for jur in ("US", "EP", "JP", "CN", "KR", "IN", "AU", "CA", "BR", "MX"):
                result[jur] = overall
    return result


# ─────────────────────────────────────────────
# Combined timeline fetch
# ─────────────────────────────────────────────

async def fetch_clinical_timeline(
    drug_name:          str,
    bq_table_name:      Optional[str] = None,
    bq_project_id:      Optional[str] = None,
    bq_dataset_id:      Optional[str] = None,
    bq_service_account: Optional[str] = None,
) -> Dict:
    """
    Queries BigQuery (clinical_efficacy + the drug-list/drug_details view)
    for clinical stage data and pools both sources per jurisdiction (highest
    priority phase wins, same rule as the reference script's
    _pick_best_phase_multi), so geography_stages reflects the single best
    phase per jurisdiction across both BigQuery sources. No local Excel
    fallback is used.

    Returns:
        Timeline dict with keys: current_stage, geography_stages, source, drug_name, …
        geography_stages uses "United States"/"EU" (back-compat) plus
        every other resolved jurisdiction token (US/EP/JP/CN/...).
    """
    # ── Canonicalise alias -> INN before any lookup ───────────────────────
    drug_name = canonicalise_drug_name(drug_name)

    empty = {
        "current_stage":    None,
        "all_stages":       _TIMELINE_STAGES,
        "completed_stages": [],
        "stage_years":      {s: None for s in _TIMELINE_STAGES},
        "geography_stages": {},
        "notes":            None,
        "source":           "unavailable",
        "drug_name":        drug_name,
    }

    _bq_table   = bq_table_name      or BQ_TABLE_NAME
    _bq_project = bq_project_id      or BQ_PROJECT_ID
    _bq_dataset = bq_dataset_id      or BQ_DATASET_ID
    _bq_sa      = bq_service_account or BQ_SERVICE_ACCOUNT

    loop = asyncio.get_event_loop()

    # ── Step 1: BigQuery (clinical_efficacy) — raw rows ──────────────────
    clinical_df = pd.DataFrame()
    if _bq_table and _bq_project and _bq_dataset:
        print(f"[TIMELINE] Querying clinical_efficacy for '{drug_name}'...")
        try:
            clinical_df = await loop.run_in_executor(
                None,
                lambda: import_from_gbq(
                    drug_name            = drug_name,
                    table_name           = _bq_table,
                    project_id           = _bq_project,
                    dataset_id           = _bq_dataset,
                    service_account_path = _bq_sa,
                ),
            )
        except Exception as e:
            print(f"[TIMELINE] clinical_efficacy error: {e}")
    else:
        print("[TIMELINE] No BigQuery config — skipping clinical_efficacy lookup")

    clin_phases_by_jur, clin_status_by_jur = _phases_by_jurisdiction_clinical(clinical_df)
    clin_overall = _overall_phase_clinical(clinical_df)

    # ── Step 1b: BigQuery (drug_details — "the drug list table") ─────────
    vwd_df = pd.DataFrame()
    vwd_project, vwd_dataset = _clinical_project_dataset()
    if vwd_project and vwd_dataset:
        print(f"[TIMELINE] Querying drug_details ('{BQ_DRUG_DETAILS_TABLE}') for '{drug_name}'...")
        try:
            vwd_df = await loop.run_in_executor(
                None,
                lambda: _fetch_drug_details_df(
                    drug_name  = drug_name,
                    project_id = vwd_project,
                    dataset_id = vwd_dataset,
                    sa_path    = _bq_sa,
                ),
            )
        except Exception as e:
            print(f"[TIMELINE] drug_details error: {e}")

    vwd_phases_by_jur = _phases_by_jurisdiction_vwd(vwd_df)
    vwd_overall = _overall_phase_vwd(vwd_df)

    # ── Step 1c: pool both sources per jurisdiction (highest wins) ───────
    all_jurisdictions = set(clin_phases_by_jur.keys()) | set(vwd_phases_by_jur.keys())
    bq_geography: Dict[str, Optional[str]] = {}
    trial_status_by_jur: Dict[str, str] = {}
    for jur in all_jurisdictions:
        best_phase, source = _pick_best_phase_multi(
            clin_phases_by_jur.get(jur, set()), vwd_phases_by_jur.get(jur, set())
        )
        bq_geography[jur] = best_phase
        if best_phase and source == "clinical" and jur in clin_status_by_jur:
            trial_status_by_jur[jur] = clin_status_by_jur[jur]
        print(f"[TIMELINE] {drug_name} | {jur}: clinical={sorted(clin_phases_by_jur.get(jur, set()))} "
              f"vwd={sorted(vwd_phases_by_jur.get(jur, set()))} -> {best_phase} (source={source})")

    # No jurisdiction resolved anything from either source — apply the
    # overall (location-agnostic) highest phase to US/EP, same fallback
    # behaviour as before.
    if not bq_geography:
        overall_best = _highest_phase(clin_overall, vwd_overall)
        if overall_best:
            bq_geography["US"] = overall_best
            bq_geography["EP"] = overall_best
            print(f"[TIMELINE] No per-jurisdiction phase found for '{drug_name}' — "
                  f"applying overall phase '{overall_best}' to US/EP")

    # Back-compat keys used by _parse_args()/CLI and any older caller.
    if bq_geography.get("US"):
        bq_geography["United States"] = bq_geography["US"]
    if bq_geography.get("EP"):
        bq_geography["EU"] = bq_geography["EP"]

    print(f"[TIMELINE] clinical_efficacy + drug_details merged -> {bq_geography}")

    # ── Final result — BigQuery only, no fallback Excel merge ────────────
    merged: Dict[str, Optional[str]] = dict(bq_geography)

    if not any(merged.values()):
        print(f"[TIMELINE] No phase data found from BigQuery for '{drug_name}'")
        return empty

    current_stage = (
        max((v for v in merged.values() if v), key=lambda s: _STAGE_RANK.get(s, 0))
        if any(merged.values()) else "Preclinical"
    )
    current_idx = _TIMELINE_STAGES.index(current_stage) if current_stage in _TIMELINE_STAGES else 0

    source = "bigquery"

    print(f"[TIMELINE] '{drug_name}' -> Overall: '{current_stage}' | Per-geo: {merged} | Source: {source}")

    result = {
        "current_stage":    current_stage,
        "all_stages":       _TIMELINE_STAGES,
        "completed_stages": _TIMELINE_STAGES[: current_idx + 1],
        "stage_years":      {s: None for s in _TIMELINE_STAGES},
        "geography_stages": merged,   # fully merged — includes United States / EU keys
        "notes":            f"Stage from {source}. Per-geography: {merged}",
        "source":           source,
        "drug_name":        drug_name,
    }
    if trial_status_by_jur:
        result["geography_stages"]["_trial_status"] = trial_status_by_jur
    return result


def assign_patent_phases(patents: List[Dict], timeline: Dict) -> List[Dict]:
    """
    Assigns phase_at_filing to each patent based on:
      1. The patent's jurisdiction (US / EP / ...)
      2. The overall geography_stages resolved by fetch_clinical_timeline()
         (BigQuery clinical_efficacy + drug_details only — no local Excel
         fallback, no year-based lookup).

    Args:
        patents:  List of patent dicts (from blocking_analyser)
        timeline: Timeline dict (from fetch_clinical_timeline)

    Returns:
        patents list with phase_at_filing set on each entry.
    """
    source           = timeline.get("source", "unavailable")
    geography_stages = timeline.get("geography_stages", {})
    drug_name        = timeline.get("drug_name", "")

    print(f"[PHASE] ── assign_patent_phases called ──")
    print(f"[PHASE]   drug_name        = '{drug_name}'")
    print(f"[PHASE]   source           = '{source}'")
    print(f"[PHASE]   geography_stages = {geography_stages}")

    # Build per-jurisdiction fallback from geography_stages
    fallback_stages: Dict[str, Optional[str]] = {}
    fallback_trial_status: Dict[str, str] = {}
    _trial_status_map = geography_stages.pop("_trial_status", {})
    _JUR_ALIASES = {
        "united states": "US", "us": "US", "usa": "US",
        "eu": "EP", "europe": "EP", "european union": "EP", "ep": "EP",
        "japan": "JP", "jp": "JP",
        "china": "CN", "cn": "CN",
        "korea": "KR", "south korea": "KR", "kr": "KR",
        "india": "IN", "in": "IN",
        "australia": "AU", "au": "AU",
        "canada": "CA", "ca": "CA",
        "brazil": "BR", "br": "BR",
        "mexico": "MX", "mx": "MX",
        "taiwan": "TW", "tw": "TW",
        "russia": "RU", "ru": "RU",
        "poland": "PL", "pl": "PL",
        "netherlands": "NL", "nl": "NL",
        "spain": "ES", "es": "ES",
    }
    if isinstance(_trial_status_map, dict):
        for geo_key, ts in _trial_status_map.items():
            jur = _JUR_ALIASES.get(geo_key.lower().strip(), geo_key.upper().strip())
            fallback_trial_status[jur] = ts

    for geo_key, phase in geography_stages.items():
        jur = _JUR_ALIASES.get(geo_key.lower().strip(), geo_key.upper().strip())
        fallback_stages[jur] = _highest_phase(fallback_stages.get(jur), phase)

    print(f"[PHASE] Per-jurisdiction fallback -> {fallback_stages}")

    # ── Assign to each patent — jurisdiction lookup only (no year-based
    #    Excel lookup) ──────────────────────────────────────────────────
    any_assigned = False
    for patent in patents:
        if patent.get("tag") == "SKIPPED":
            patent["phase_at_filing"] = None
            continue

        jurisdiction = (patent.get("jurisdiction") or "").upper()

        stage = fallback_stages.get(jurisdiction)
        if stage:
            print(f"[PHASE] {patent.get('patent_number')} | {jurisdiction} -> {stage}")
        else:
            print(f"[PHASE] {patent.get('patent_number')} | {jurisdiction} | "
                  f"Phase not available for this jurisdiction")

        patent["phase_at_filing"] = stage
        patent["trial_status"] = fallback_trial_status.get(jurisdiction, "")
        if stage:
            any_assigned = True

    if not any_assigned:
        print(f"[PHASE] No phase data available — phase_at_filing = None for all patents")

    return patents


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def _parse_args():
    import argparse

    parser = argparse.ArgumentParser(
        description="Fetch the clinical development phase for a drug."
    )
    parser.add_argument(
        "--drug",
        required=True,
        help="Drug name to look up, e.g. \"Orforglipron Calcium\"",
    )
    return parser.parse_args()


async def _run_cli():
    args = _parse_args()

    timeline = await fetch_clinical_timeline(drug_name=args.drug)

    print("\n" + "=" * 60)
    print(f"Drug:              {timeline['drug_name']}")
    print(f"Current phase:     {timeline['current_stage']}")
    print(f"Source:            {timeline['source']}")
    print(f"Per-geography:     {timeline['geography_stages']}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(_run_cli())