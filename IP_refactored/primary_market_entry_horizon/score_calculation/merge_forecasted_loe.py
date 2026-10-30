"""
merge_forecasted_loe.py
───────────────────────
Builds the two LOE output tables for one drug from the pipeline's patent list:

    patents_to_dataframe(patents, drug_name)  -> glp_patent_loe rows (Type="Existing")
    run_merge(patents, drug_name)             -> glp_master_loe rows (Existing + Forecasted, re-scored)

run_merge() follows the reference combine() pipeline:
    1. load forecasted_loe for the drug (Type="Forecasted")
    2. drop the forecast if the drug is Marketed / Pre-registration (real data supersedes it)
    3. align forecasted_loe column names to ours, then concat (columns it lacks stay blank on Forecasted rows)
    4. Type blank -> "Existing"; Grant Date = Filing Date for Forecasted rows
    5. stamp forecasted_loe's per-drug Avg Years to Entry + IP Dimension 1 Score on every row
    6. scoring: forecasted expiry -> base Years to Entry -> LOE override -> fallback
    7. project to config.FINAL_COLUMNS, keeping our extra columns after them
    8. add "Rationale" (generate_report_and_rationale.generate_rationale)
"""

import logging
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

from ... import config
from ...utils import find_col, norm_col, norm_phase, normalize_drug_str
from ..bq_utils import fetch_drug_details_df, load_forecasted_loe_table
from ..generate_report_and_rationale.generate_rationale import rationale_for_drug
from . import scoring
from .phase_fetcher import _phases_by_jurisdiction_vwd, canonicalise_drug_name, drug_details_columns

logger = logging.getLogger(__name__)

EXISTING = "Existing"
FORECASTED = "Forecasted"


# ─────────────────────────────────────────────
# pipeline patents -> DataFrame
# ─────────────────────────────────────────────

def _approval_for_jurisdiction(patent: Dict) -> Tuple[Optional[str], Optional[str]]:
    jurisdiction = (patent.get("jurisdiction") or "").upper()
    if jurisdiction == "US":
        return patent.get("approval_date_us"), patent.get("approval_date_us_source")
    if jurisdiction in ("EP", "EU"):
        return patent.get("approval_date_eu"), patent.get("approval_date_eu_source")
    return (patent.get("approval_date_us") or patent.get("approval_date_eu"),
            patent.get("approval_date_us_source") or patent.get("approval_date_eu_source"))


_STEP2_ELEMENT_COLUMNS = {
    "active_ingredient_and_form": "S2 Active Ingredient Form",
    "formulation_details":        "S2 Formulation Details",
    "route_of_administration":    "S2 Route of Administration",
    "device_description":         "S2 Device Description",
    "combination_tech_process":   "S2 Combination Tech Process",
}


def _step2_columns(patent: Dict) -> Dict[str, Optional[object]]:
    """Step 2 detail: comma-joined matched elements + one column per element.
    All None when the patent never reached Step 2 (e.g. primary CoM)."""
    elements = patent.get("step2_elements_present")
    if not isinstance(elements, dict):
        return {"Step 2 Matched Elements": None, **{col: None for col in _STEP2_ELEMENT_COLUMNS.values()}}
    matched = [key for key in _STEP2_ELEMENT_COLUMNS if elements.get(key)]
    return {
        "Step 2 Matched Elements": ", ".join(matched),
        **{col: elements.get(key) for key, col in _STEP2_ELEMENT_COLUMNS.items()},
    }


def patents_to_dataframe(patents: List[Dict], drug_name: str) -> pd.DataFrame:
    """One row per analysed patent, with every config.FINAL_COLUMNS column (in
    order) followed by the extra blocking-analysis detail columns."""
    rows = []
    for p in patents:
        approval_date, approval_source = _approval_for_jurisdiction(p)
        rows.append({
            "Drug Name":                      drug_name,
            "Jurisdiction":                   (p.get("jurisdiction") or "").upper(),
            "Tag":                            p.get("tag"),
            "Type":                           EXISTING,
            "Filing Date":                    p.get("filing_date"),
            "Patent Number":                  p.get("patent_number"),
            "No Of Forecasted Patents":       None,
            "Years to Entry":                 p.get("years_to_entry"),
            "Avg Years to Entry (US & EP)":   p.get("avg_years_to_entry_us_ep"),
            "IP Dimension 1 Score":           p.get("ip_dimension_1_score"),
            "Step 1 Claim Category":          p.get("claim_category"),
            "Grant Date":                     p.get("grant_date"),
            "PTE (months)":                   p.get("pte"),
            "Pediatric Exclusivity":          p.get("pediatric_exclusivity"),
            "Controlling Patent Expiry Year": p.get("controlling_patent_expiry_year"),
            "Exclusivity Year":               p.get("exclusivity_year"),
            "Approval Date":                  approval_date,
            "Est. Approval Year":             p.get("estimated_approval_year"),
            "Phase":                          p.get("phase_at_filing"),
            "Strategic Purpose":              None,
            "Risk Assessment":                None,
            "Overall Forecast":               None,
            "Portfolio Gaps":                 None,
            "Existing Patent Summary":        None,
            "Reason":                         p.get("reason"),
            "Approval Date Source":           approval_source,
            "Source File":                    p.get("source_file"),
            "Step 2 Elements Present":        p.get("step2_elements_present"),
            **_step2_columns(p),
            "Step 3 Technical Barrier":       p.get("step3_is_technical_barrier"),
            "Step 3 Confidence":              p.get("step3_confidence"),
            "Step 3 Evidence Type":           p.get("step3_evidence_type"),
            "Step 3 Evidence Summary":        p.get("step3_evidence_summary"),
            "Step 4 Blocking Indicator":      p.get("step4_is_blocking_indicator"),
            "Step 4 Confidence":              p.get("step4_confidence"),
            "Step 4 Regulatory Failure if Removed": p.get("step4_regulatory_failure_if_removed"),
            "Step 4 Bridging Studies Required": p.get("step4_bridging_studies_required"),
            "Step 4 Formulation Consistent Across Phases": p.get("step4_formulation_consistent_across_phases"),
            "Step 4 Reason":                  p.get("step4_reason"),
            "Step 5 Novel & Difficult":       p.get("step5_is_novel_and_difficult"),
            "Step 5 Novelty Signal":          p.get("step5_novelty_signal"),
            "Step 5 First in Class":          p.get("step5_first_in_class"),
            "Step 5 Prior Failed Attempts":   p.get("step5_prior_failed_attempts"),
            "Step 5 Complex Implementation":  p.get("step5_complex_implementation"),
            "Step 5 Confidence":              p.get("step5_confidence"),
            "Step 5 Reason":                  p.get("step5_reason"),
            "Launch Date":                    None,
            "Avg Years to Entry":             p.get("avg_years_to_entry"),
            "Score":                          p.get("score"),
        })
    return pd.DataFrame(rows)


# ─────────────────────────────────────────────
# Column alignment helpers
# ─────────────────────────────────────────────

def ensure_column(df: pd.DataFrame, canonical_name: str, alternatives: Sequence[Sequence[str]],
                  exclude_tokens: Sequence[str] = (), label: str = "") -> pd.DataFrame:
    """Guarantee *canonical_name* exists: rename the first fuzzy match, or add a blank column."""
    for required in alternatives:
        match = next((c for c in df.columns
                      if all(t in norm_col(c) for t in required)
                      and not any(t in norm_col(c) for t in exclude_tokens)), None)
        if match:
            if match != canonical_name:
                logger.info("[MERGE] Mapped %r -> %r (%s)", match, canonical_name, label)
                df = df.rename(columns={match: canonical_name})
            return df
    logger.info("[MERGE] No column resembling %r in %s — adding a blank one", canonical_name, label)
    df[canonical_name] = pd.NA
    return df


def align_columns(df_source: pd.DataFrame, df_reference: pd.DataFrame) -> pd.DataFrame:
    """Rename df_source columns to df_reference's spelling where they are the
    same field (identical normalised name, or a config.MANUAL_COLUMN_ALIASES entry)."""
    ref_by_norm = {norm_col(c): c for c in df_reference.columns}
    rename_map = {}
    for c in df_source.columns:
        target_norm = config.MANUAL_COLUMN_ALIASES.get(norm_col(c), norm_col(c))
        target = ref_by_norm.get(target_norm)
        if target and target != c:
            rename_map[c] = target
    if rename_map:
        logger.info("[MERGE] Aligned forecasted_loe columns: %s", rename_map)
    return df_source.rename(columns=rename_map)


def final_column_matches(df: pd.DataFrame) -> Dict[str, str]:
    """{friendly name: source column} for every config.FINAL_COLUMNS entry
    found in *df*, using its token rules. Each source column is used once."""
    used: set = set()
    matches: Dict[str, str] = {}
    for friendly, alternatives in config.FINAL_COLUMNS:
        for alt in alternatives:
            required, exclude = alt if isinstance(alt, tuple) else (alt, [])
            match = next((c for c in df.columns if c not in used
                          and all(t in norm_col(c) for t in required)
                          and not any(t in norm_col(c) for t in exclude)), None)
            if match:
                matches[friendly] = match
                used.add(match)
                break
    return matches


def rename_to_final_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename fuzzy-matched columns to their config.FINAL_COLUMNS friendly names."""
    rename_map = {src: friendly for friendly, src in final_column_matches(df).items()
                  if src != friendly and friendly not in df.columns}
    return df.rename(columns=rename_map)


def select_final_columns(df: pd.DataFrame) -> Tuple[pd.DataFrame, set]:
    """Project *df* onto config.FINAL_COLUMNS (friendly names, in order).
    Returns (result, set of source columns used)."""
    matches = final_column_matches(df)
    data: Dict[str, object] = {}
    for friendly, _ in config.FINAL_COLUMNS:
        if friendly in matches:
            data[friendly] = df[matches[friendly]].values
        else:
            logger.warning("[MERGE] No source column for %r — filling blank", friendly)
            data[friendly] = [pd.NA] * len(df)
    return pd.DataFrame(data, index=df.index), set(matches.values())


def filter_to_drug(df: pd.DataFrame, drug_name: str) -> pd.DataFrame:
    drug_col = find_col(df, ["drug"])
    if not drug_col:
        logger.warning("[MERGE] forecasted_loe has no drug column — cannot filter to %r", drug_name)
        return df.iloc[0:0]
    target = normalize_drug_str(drug_name)
    return df[df[drug_col].map(normalize_drug_str) == target].copy()


# ─────────────────────────────────────────────
# Source tables
# ─────────────────────────────────────────────

def load_forecasted_loe(drug_name: str) -> pd.DataFrame:
    df = filter_to_drug(load_forecasted_loe_table(), drug_name)
    logger.info("[MERGE] forecasted_loe: %d row(s) for %r", len(df), drug_name)
    return df


PREREG_JURISDICTIONS = ("US", "EP")   # "EU" is normalised to "EP"


def is_marketed(drug_details_df: pd.DataFrame) -> bool:
    """True if the drug is Marketed in any geography."""
    if drug_details_df.empty:
        return False
    phase_col = drug_details_columns(drug_details_df)["phase"]
    if not phase_col:
        logger.warning("[MERGE] No development-stage column in drug details — treating as not marketed")
        return False
    return bool(drug_details_df[phase_col].map(norm_phase).eq("approved/marketed").any())


def is_marketed_or_prereg(drug_details_df: pd.DataFrame) -> bool:
    """True if the drug is Marketed (any geography) or Pre-registration in the
    US or EP/EU. Pre-registration elsewhere (e.g. China only) does not count."""
    if is_marketed(drug_details_df):
        return True
    if drug_details_df.empty or not drug_details_columns(drug_details_df)["phase"]:
        return False
    phases_by_jur = _phases_by_jurisdiction_vwd(drug_details_df)
    return any("Pre-registration" in phases_by_jur.get(jur, ()) for jur in PREREG_JURISDICTIONS)


# ─────────────────────────────────────────────
# Merge + score
# ─────────────────────────────────────────────

def _per_drug_first(df: pd.DataFrame, value_col: str) -> Dict[str, object]:
    """{lower(drug name): first value of value_col} — forecasted_loe repeats
    its drug-level Avg / Score on every row."""
    if df.empty or value_col not in df.columns or scoring.DRUG_COL not in df.columns:
        return {}
    firsts = df.groupby(scoring.DRUG_COL)[value_col].first().to_dict()
    return {str(k).strip().lower(): v for k, v in firsts.items()}


def run_merge(patents: List[Dict], drug_name: str) -> pd.DataFrame:
    """Merge the pipeline's patents (Existing) with forecasted_loe (Forecasted)
    for one drug and re-score. Returns glp_master_loe rows."""
    existing = patents_to_dataframe(patents, drug_name)
    existing_columns = list(existing.columns)

    forecasted = ensure_column(
        load_forecasted_loe(drug_name), "no_of_forecasted_patents",
        [["noofforecastedpatents"], ["forecasted", "patents"], ["forecastedcount"], ["patents"]],
        label="forecasted_loe",
    )

    drug_details = fetch_drug_details_df(canonicalise_drug_name(drug_name))
    if not config.NO_MARKETED_SKIP and not forecasted.empty and is_marketed_or_prereg(drug_details):
        logger.info("[MERGE] %r is Marketed / Pre-registration — dropping its %d forecasted row(s)",
                    drug_name, len(forecasted))
        forecasted = forecasted.iloc[0:0]

    # Columns forecasted_loe shares with ours (including our extra detail
    # columns) are carried onto the Forecasted rows; the rest stay blank.
    forecasted = align_columns(forecasted, existing)
    # A bare "Avg Years to Entry" in forecasted_loe is also the drug-level US & EP
    # average (as before the extra column existed): copy it so the fuzzy rename
    # below does not move it out of the extra column.
    if "Avg Years to Entry" in forecasted.columns and scoring.AVG_COL not in forecasted.columns:
        forecasted[scoring.AVG_COL] = forecasted["Avg Years to Entry"]
    forecasted = rename_to_final_columns(forecasted)
    # One spelling of the drug across both frames (forecasted_loe may differ in case/spacing).
    if scoring.DRUG_COL in forecasted.columns:
        forecasted[scoring.DRUG_COL] = drug_name
    avg_map = _per_drug_first(forecasted, scoring.AVG_COL)
    score_map = _per_drug_first(forecasted, scoring.SCORE_COL)

    combined = pd.concat([forecasted, existing], ignore_index=True, sort=False)

    type_values = combined[scoring.TYPE_COL]
    combined.loc[type_values.isna() | (type_values.astype(str).str.strip() == ""), scoring.TYPE_COL] = EXISTING
    is_forecasted = combined[scoring.TYPE_COL].astype(str).str.strip().str.lower() == FORECASTED.lower()
    combined["Grant Date"] = combined["Grant Date"].astype(object)
    combined.loc[is_forecasted, "Grant Date"] = combined.loc[is_forecasted, scoring.FILING_COL]

    drop = {norm_col(d) for d in config.DROP_COLUMNS}
    combined = combined.drop(columns=[c for c in combined.columns if norm_col(c) in drop])

    # forecasted_loe's per-drug Avg / Score apply to every row of the drug;
    # drugs it doesn't cover are left blank so the scoring steps recompute them.
    drug_key = combined[scoring.DRUG_COL].astype(str).str.strip().str.lower()
    combined[scoring.AVG_COL] = drug_key.map(avg_map)
    combined[scoring.SCORE_COL] = drug_key.map(score_map)

    combined = scoring.apply_forecasted_filing_expiry(combined)
    combined = scoring.calculate_base_years_to_entry(combined)
    combined = scoring.override_avg_years_from_loe(combined, drug_details)
    combined = scoring.fill_missing_avg_score(combined)

    final_df, used = select_final_columns(combined)
    for c in existing_columns:
        if c in combined.columns and c not in used:
            final_df[c] = combined[c].values

    # Per-drug LOE rationale (same text on every row of the drug).
    try:
        final_df["Rationale"] = rationale_for_drug(final_df)
    except Exception:
        logger.exception("[MERGE] Could not build the Rationale for %r — leaving it blank", drug_name)
        final_df["Rationale"] = pd.NA

    n_forecasted = int((final_df["Type"].astype(str).str.lower() == FORECASTED.lower()).sum())
    logger.info("[MERGE] %r: %d row(s) (%d Existing, %d Forecasted)",
                drug_name, len(final_df), len(final_df) - n_forecasted, n_forecasted)
    return final_df
