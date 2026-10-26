"""
scoring.py
──────────
"Avg Years to Entry (US & EP)" and "IP Dimension 1 Score" calculations for
the merged LOE table, applied in this order by merge_forecasted_loe.run_merge():

    apply_forecasted_filing_expiry()    Forecasted rows: Controlling Patent Expiry Year = filing year + 20
    calculate_base_years_to_entry()     Years to Entry = max(expiry, exclusivity) - current year
    override_avg_years_from_loe()       Marketed / Pre-reg drugs: recompute from vw_drug_details_full LOE_Date
    fill_missing_avg_score()            last resort: avg of max US / EP Years to Entry

All functions take and return the merged DataFrame (friendly column names
from config.FINAL_COLUMNS) and mutate it in place.
"""

import logging
from datetime import datetime
from typing import Dict, Optional

import numpy as np
import pandas as pd

from ... import config
from .phase_fetcher import drug_details_columns
from ...utils import find_col, ip_dimension_score, jurisdiction_code, normalize, split_jurisdiction_tokens, year_from_value

logger = logging.getLogger(__name__)

DRUG_COL = "Drug Name"
JURISDICTION_COL = "Jurisdiction"
TYPE_COL = "Type"
FILING_COL = "Filing Date"
YTE_COL = "Years to Entry"
AVG_COL = "Avg Years to Entry (US & EP)"
SCORE_COL = "IP Dimension 1 Score"
EXPIRY_COL = "Controlling Patent Expiry Year"
EXCLUSIVITY_COL = "Exclusivity Year"
EST_APPROVAL_COL = "Est. Approval Year"

def exclusivity_offsets() -> Dict[str, int]:
    """Years of regulatory exclusivity after approval, per jurisdiction token."""
    return {"US": config.EXCLUSIVITY_OFFSET_US, "EP": config.EXCLUSIVITY_OFFSET_EP}


def _missing_columns(df: pd.DataFrame, *cols: str) -> list:
    return [c for c in cols if c not in df.columns]


def _drug_mask(df: pd.DataFrame, drug: str) -> pd.Series:
    return df[DRUG_COL].astype(str).str.strip().str.lower() == drug.strip().lower()


# ─────────────────────────────────────────────
# Step 8 — expiry for Forecasted rows
# ─────────────────────────────────────────────

def apply_forecasted_filing_expiry(df: pd.DataFrame) -> pd.DataFrame:
    """Forecasted rows have no patent_master record, so their Controlling
    Patent Expiry Year is Filing Date year + 20. Existing rows keep the
    PTE-adjusted expiry computed by primary_market_entry_horizon."""
    missing = _missing_columns(df, TYPE_COL, FILING_COL, EXPIRY_COL)
    if missing:
        logger.warning("[SCORING] Forecasted expiry skipped — missing columns: %s", missing)
        return df

    is_forecasted = df[TYPE_COL].astype(str).str.strip().str.lower() == "forecasted"
    filing_year = df.loc[is_forecasted, FILING_COL].map(year_from_value).astype("float64")
    usable = filing_year.notna()
    df.loc[filing_year[usable].index, EXPIRY_COL] = filing_year[usable] + config.PATENT_TERM_YEARS
    logger.info("[SCORING] Forecasted expiry = filing year + %d for %d of %d forecasted row(s)",
                config.PATENT_TERM_YEARS, int(usable.sum()), int(is_forecasted.sum()))
    return df


# ─────────────────────────────────────────────
# Step 9 — base Years to Entry
# ─────────────────────────────────────────────

def calculate_base_years_to_entry(df: pd.DataFrame) -> pd.DataFrame:
    """Years to Entry = max(Controlling Patent Expiry Year, Exclusivity Year) - current year,
    for every row with a plausible expiry."""
    if _missing_columns(df, YTE_COL, EXPIRY_COL):
        return df

    current_year = datetime.now().year
    expiry = pd.to_numeric(df[EXPIRY_COL], errors="coerce")
    exclusivity = pd.to_numeric(
        df.get(EXCLUSIVITY_COL, pd.Series(index=df.index, dtype=float)), errors="coerce",
    ).reindex(df.index)

    effective_expiry = np.maximum(expiry.fillna(0), exclusivity.fillna(0))
    valid = effective_expiry >= (current_year - 50)
    if valid.any():
        df.loc[valid, YTE_COL] = (effective_expiry[valid] - current_year).round(1)
    logger.info("[SCORING] Years to Entry calculated for %d row(s); %d row(s) had no valid expiry",
                int(valid.sum()), int((~valid).sum()))
    return df


# ─────────────────────────────────────────────
# Step 11 — real LOE override for Marketed / Pre-registration drugs
# ─────────────────────────────────────────────

def _write_loe_based_result(df, mask, loe_year, approval_year, excl_years, current_year) -> float:
    """Write LOE-based expiry / exclusivity / Years to Entry for rows in *mask*
    and return the effective entry year."""
    df.loc[mask, EXPIRY_COL] = loe_year
    if approval_year is not None:
        exclusivity_year = approval_year + excl_years
        effective_expiry = max(loe_year, exclusivity_year)
        df.loc[mask, EST_APPROVAL_COL] = approval_year
        df.loc[mask, EXCLUSIVITY_COL] = exclusivity_year
    else:
        effective_expiry = loe_year
    df.loc[mask, YTE_COL] = round(effective_expiry - current_year, 1)
    return effective_expiry


def _loe_columns(drug_details_df: pd.DataFrame) -> Dict[str, Optional[str]]:
    cols = drug_details_columns(drug_details_df)
    cols.update({
        "name":     (find_col(drug_details_df, ["cleaned", "generic"])
                     or find_col(drug_details_df, ["drug", "name"])
                     or find_col(drug_details_df, ["generic"])),
        "loe":      find_col(drug_details_df, ["loe", "date"]) or find_col(drug_details_df, ["loe"]),
        "approval": (find_col(drug_details_df, ["first", "approval"])
                     or find_col(drug_details_df, ["approval", "date"])),
    })
    return cols


def _first_year(rows: pd.DataFrame, col: Optional[str]) -> Optional[int]:
    if not col or rows.empty:
        return None
    for val in rows[col]:
        year = year_from_value(val)
        if year is not None:
            return int(year)
    return None


def override_avg_years_from_loe(df: pd.DataFrame, drug_details_df: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Recompute Years to Entry (per row) and Avg Years to Entry (US & EP) +
    IP Dimension 1 Score (per drug) from vw_drug_details_full's real LOE_Date,
    overriding the forecasted_loe value, for drugs that have one."""
    missing = _missing_columns(df, DRUG_COL, JURISDICTION_COL, YTE_COL, AVG_COL, SCORE_COL)
    if missing:
        logger.warning("[SCORING] LOE override skipped — missing columns: %s", missing)
        return df
    if drug_details_df is None or drug_details_df.empty:
        logger.info("[SCORING] LOE override skipped — no drug-details rows")
        return df

    cols = _loe_columns(drug_details_df)
    if not cols["name"] or not cols["loe"]:
        logger.warning("[SCORING] LOE override skipped — no name/LOE column in drug details: %s",
                       list(drug_details_df.columns))
        return df

    current_year = datetime.now().year
    details_name = drug_details_df[cols["name"]].map(normalize)
    jurisdictions = df[JURISDICTION_COL].map(jurisdiction_code)
    n_overridden = 0

    for drug in df[DRUG_COL].dropna().astype(str).str.strip().unique():
        drug_rows = _drug_mask(df, drug)
        details = drug_details_df[details_name == normalize(drug)]
        if details.empty:
            continue

        effective_by_token: Dict[str, float] = {}
        for token, excl_years in exclusivity_offsets().items():
            in_geo = details.apply(
                lambda r: token in (
                    split_jurisdiction_tokens(r.get(cols["geo"]), r"[,;]+")
                    or split_jurisdiction_tokens(r.get(cols["geo_fallback"]), r"[,;]+")
                ),
                axis=1,
            ) if (cols["geo"] or cols["geo_fallback"]) else pd.Series(True, index=details.index)
            geo_rows = details[in_geo]

            loe_year = _first_year(geo_rows, cols["loe"])
            if loe_year is None:
                continue

            token_rows = drug_rows & (jurisdictions == token)
            approval_year = _first_year(geo_rows, cols["approval"])
            if approval_year is None and EST_APPROVAL_COL in df.columns:
                existing = pd.to_numeric(df.loc[token_rows, EST_APPROVAL_COL], errors="coerce").dropna()
                approval_year = int(existing.iloc[0]) if not existing.empty else None

            effective_by_token[token] = _write_loe_based_result(
                df, token_rows, loe_year, approval_year, excl_years, current_year,
            )

        if not effective_by_token:
            continue
        avg_years = round(sum(effective_by_token.values()) / len(effective_by_token) - current_year, 1)
        df.loc[drug_rows, AVG_COL] = avg_years
        df.loc[drug_rows, SCORE_COL] = ip_dimension_score(avg_years)
        n_overridden += 1

    logger.info("[SCORING] LOE override applied to %d drug(s)", n_overridden)
    return df


# ─────────────────────────────────────────────
# Step 12 — last-resort fallback
# ─────────────────────────────────────────────

def fill_missing_avg_score(df: pd.DataFrame) -> pd.DataFrame:
    """For drugs still missing Avg Years to Entry (US & EP), use the mean of
    the max US and max EP Years to Entry, then score it."""
    missing = _missing_columns(df, DRUG_COL, JURISDICTION_COL, YTE_COL, AVG_COL, SCORE_COL)
    if missing:
        logger.warning("[SCORING] Avg fallback skipped — missing columns: %s", missing)
        return df

    avg = pd.to_numeric(df[AVG_COL], errors="coerce")
    missing_drugs = df.loc[avg.isna(), DRUG_COL].dropna().astype(str).str.strip().unique()
    if len(missing_drugs) == 0:
        return df

    yte = pd.to_numeric(df[YTE_COL], errors="coerce")
    jurisdictions = df[JURISDICTION_COL].map(jurisdiction_code)
    still_missing = []
    for drug in missing_drugs:
        drug_rows = _drug_mask(df, drug)
        maxima = [yte[drug_rows & (jurisdictions == token)].max() for token in exclusivity_offsets()]
        values = [v for v in maxima if pd.notna(v)]
        if not values:
            still_missing.append(drug)
            continue
        avg_val = sum(values) / len(values)
        df.loc[drug_rows, AVG_COL] = round(avg_val, 1)
        df.loc[drug_rows, SCORE_COL] = ip_dimension_score(avg_val)

    logger.info("[SCORING] Avg fallback filled %d drug(s)%s",
                len(missing_drugs) - len(still_missing),
                f"; no US/EP Years to Entry for: {sorted(still_missing)}" if still_missing else "")
    return df
