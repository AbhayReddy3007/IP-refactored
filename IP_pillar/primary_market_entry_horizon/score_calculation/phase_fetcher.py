"""
phase_fetcher.py
─────────────────
Handles:
  - Fetching clinical development stage from BigQuery (clinical_efficacy table)
  - Fetching clinical development stage from BigQuery (drug-details/"drug
    list" table — the same view drug_list.py resolves the GLP-1
    drug list from)
  - Merging clinical_efficacy + drug_details stages per jurisdiction
    (US / EP / JP / CN / KR / IN / AU / CA / BR / MX / TW / RU / PL / NL / ES)
  - Assigning phase_at_filing to each patent dict

No local Excel fallback — every phase comes from BigQuery
(clinical_efficacy + the drug-list/drug_details view) only.

Phase normalisation and jurisdiction tokens come from IP_pillar.utils
(norm_phase / split_jurisdiction_tokens). This module adds the
Drug_Geo_New-with-Drug_Geography fallback and the "highest-priority phase
pooled across BOTH sources" merge rule (_pick_best_phase_multi).
The BigQuery fetches (import_from_gbq, fetch_drug_details_df) live in bq_utils.py.
"""

import logging
import asyncio
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd

from ... import config
from ...utils import find_col, norm_phase, normalize, split_jurisdiction_tokens
from ..bq_utils import fetch_drug_details_df, import_from_gbq

logger = logging.getLogger(__name__)

_TIMELINE_STAGES = [
    "Preclinical", "Phase 1", "Phase 2", "Phase 3", "Pre-registration", "Marketed"
]
_STAGE_RANK: Dict[str, int] = {s: i for i, s in enumerate(_TIMELINE_STAGES)}


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
        logger.info(f"[ALIAS] '{drug_name}' -> '{canonical}' (exact alias)")
        return canonical

    # Normalised match
    norm = normalize(drug_name)
    for alias_key, canonical in _DRUG_ALIASES.items():
        if normalize(alias_key) == norm:
            logger.info(f"[ALIAS] '{drug_name}' -> '{canonical}' (normalised alias)")
            return canonical

    return drug_name


# ─────────────────────────────────────────────
# Phase normalisation (utils.norm_phase) -> internal timeline labels
# ─────────────────────────────────────────────

# canonical (lowercase, as produced by norm_phase) -> internal timeline label
_NORM_TO_TIMELINE: Dict[str, str] = {
    "preclinical":       "Preclinical",
    "phase 1":           "Phase 1",
    "phase 2":           "Phase 2",
    "phase 3":           "Phase 3",
    "phase 4":           "Marketed",
    "pre-registration":  "Pre-registration",
    "approved/marketed": "Marketed",
}


def _normalize_phase(p: Optional[str]) -> Optional[str]:
    """Canonicalise a phase string to an internal timeline label (e.g.
    'Phase III' -> 'Phase 3'), via utils.norm_phase(). Returns None for
    blank input."""
    if p is None:
        return None
    norm = norm_phase(p)
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
# Jurisdiction / geography token matching
# ─────────────────────────────────────────────

def _location_tokens(location) -> Set[str]:
    """Jurisdiction tokens present in a clinical_efficacy trial_location
    value, e.g. 'US/EU', 'United States, China', 'Global'."""
    return split_jurisdiction_tokens(location)


def _vwd_geo_tokens(geo) -> Set[str]:
    """Jurisdiction tokens present in a drug_details Drug_Geo_New (or
    Drug_Geography) value, e.g. 'US, Germany, Japan'."""
    return split_jurisdiction_tokens(geo)


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

def drug_details_columns(df: pd.DataFrame) -> Dict[str, Optional[str]]:
    """Locate the phase / geography columns of a drug-details DataFrame
    (column casing in vw_drug_details_full varies)."""
    return {
        "phase":        find_col(df, ["highest", "development", "stage"]) or find_col(df, ["development", "stage"]),
        "geo":          find_col(df, ["drug", "geo", "new"]) or find_col(df, ["drug", "geo"]) or find_col(df, ["geo"]),
        "geo_fallback": find_col(df, ["drug", "geography"]),
    }


def _phases_by_jurisdiction_vwd(df: pd.DataFrame) -> Dict[str, Set[str]]:
    """From raw drug_details rows (already filtered to one drug), build
    {jurisdiction_token: {normalised_phase, ...}}, preferring Drug_Geo_New
    and falling back to Drug_Geography per-row (_vwd_geo_tokens_combined)."""
    phases_by_jur: Dict[str, Set[str]] = {}
    if df.empty:
        return phases_by_jur

    cols = drug_details_columns(df)
    phase_col, geo_col, geo_fallback_col = cols["phase"], cols["geo"], cols["geo_fallback"]

    if not phase_col:
        logger.warning("[DRUG_DETAILS] No Highest_Development_Stage-like column found — skipping.")
        return phases_by_jur
    if not geo_col:
        logger.warning("[DRUG_DETAILS] No Drug_Geo_New-like column found — skipping.")
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
    phase_col = drug_details_columns(df)["phase"]
    if not phase_col:
        return None
    best = None
    for raw in df[phase_col].dropna():
        best = _highest_phase(best, _normalize_phase(raw))
    return best


# ─────────────────────────────────────────────
# Combined timeline fetch
# ─────────────────────────────────────────────

async def fetch_clinical_timeline(drug_name: str) -> Dict:
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

    loop = asyncio.get_running_loop()

    # ── Step 1: BigQuery (clinical_efficacy) — raw rows ──────────────────
    logger.info(f"[TIMELINE] Querying clinical_efficacy for '{drug_name}'...")
    clinical_df = await loop.run_in_executor(None, import_from_gbq, drug_name)

    clin_phases_by_jur, clin_status_by_jur = _phases_by_jurisdiction_clinical(clinical_df)
    clin_overall = _overall_phase_clinical(clinical_df)

    # ── Step 1b: BigQuery (drug_details — "the drug list table") ─────────
    logger.info(f"[TIMELINE] Querying drug_details ('{config.DRUG_DETAILS_TABLE}') for '{drug_name}'...")
    vwd_df = await loop.run_in_executor(None, fetch_drug_details_df, drug_name)

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
        logger.info(f"[TIMELINE] {drug_name} | {jur}: clinical={sorted(clin_phases_by_jur.get(jur, set()))} "
              f"vwd={sorted(vwd_phases_by_jur.get(jur, set()))} -> {best_phase} (source={source})")

    # No jurisdiction resolved anything from either source — apply the
    # overall (location-agnostic) highest phase to US/EP, same fallback
    # behaviour as before.
    if not bq_geography:
        overall_best = _highest_phase(clin_overall, vwd_overall)
        if overall_best:
            bq_geography["US"] = overall_best
            bq_geography["EP"] = overall_best
            logger.warning(f"[TIMELINE] No per-jurisdiction phase found for '{drug_name}' — "
                  f"applying overall phase '{overall_best}' to US/EP")

    # Back-compat keys used by _parse_args()/CLI and any older caller.
    if bq_geography.get("US"):
        bq_geography["United States"] = bq_geography["US"]
    if bq_geography.get("EP"):
        bq_geography["EU"] = bq_geography["EP"]

    logger.info(f"[TIMELINE] clinical_efficacy + drug_details merged -> {bq_geography}")

    # ── Final result — BigQuery only, no fallback Excel merge ────────────
    merged: Dict[str, Optional[str]] = dict(bq_geography)

    if not any(merged.values()):
        logger.warning(f"[TIMELINE] No phase data found from BigQuery for '{drug_name}'")
        return empty

    current_stage = (
        max((v for v in merged.values() if v), key=lambda s: _STAGE_RANK.get(s, 0))
        if any(merged.values()) else "Preclinical"
    )
    current_idx = _TIMELINE_STAGES.index(current_stage) if current_stage in _TIMELINE_STAGES else 0

    source = "bigquery"

    logger.info(f"[TIMELINE] '{drug_name}' -> Overall: '{current_stage}' | Per-geo: {merged} | Source: {source}")

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

    logger.info("[PHASE] ── assign_patent_phases called ──")
    logger.info(f"[PHASE]   drug_name        = '{drug_name}'")
    logger.info(f"[PHASE]   source           = '{source}'")
    logger.info(f"[PHASE]   geography_stages = {geography_stages}")

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

    logger.info(f"[PHASE] Per-jurisdiction fallback -> {fallback_stages}")

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
            logger.info(f"[PHASE] {patent.get('patent_number')} | {jurisdiction} -> {stage}")
        else:
            logger.info(f"[PHASE] {patent.get('patent_number')} | {jurisdiction} | "
                  f"Phase not available for this jurisdiction")

        patent["phase_at_filing"] = stage
        patent["trial_status"] = fallback_trial_status.get(jurisdiction, "")
        if stage:
            any_assigned = True

    if not any_assigned:
        logger.info("[PHASE] No phase data available — phase_at_filing = None for all patents")

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