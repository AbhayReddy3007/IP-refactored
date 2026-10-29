"""Shortlisting a drug's blocking patents for the launch sequencing analysis."""

import logging

import pandas as pd

from .constants import KEEP_CLAIM_CATEGORIES, KEEP_COLUMNS, TARGET_JURISDICTIONS
from .expiry import normalize_year_only

logger = logging.getLogger(__name__)


def shortlist_patents(df: pd.DataFrame) -> dict:
    """Apply the jurisdiction / grant date / tag / claim-category filters to a drug's master rows.
    Forecasted rows are not real patents, so they bypass every filter except the jurisdiction.

    Returns a dict with:
      shortlisted    : filtered rows restricted to KEEP_COLUMNS
      full_filtered  : copy of shortlisted, kept for category lookups
      filtered_df    : filtered rows with all original columns
      stats          : row counts after each filter step
    """
    df = df.copy()
    total = len(df)
    logger.info("[FILTER] Total rows loaded           : %d", total)

    in_jurisdiction = df["Jurisdiction"].isin(TARGET_JURISDICTIONS)
    logger.info("[FILTER] After Jurisdiction filter   : %d", in_jurisdiction.sum())

    df["Grant Date"] = df["Grant Date"].apply(normalize_year_only)
    has_grant_date = pd.to_datetime(df["Grant Date"], errors="coerce").notna()
    is_forecasted = df["Type"].str.strip().str.lower() == "forecasted"
    granted = in_jurisdiction & (has_grant_date | is_forecasted)
    logger.info("[FILTER] After Grant Date filter     : %d", granted.sum())

    blocking = granted & ((df["Tag"].str.upper() == "BLOCKING") | is_forecasted)
    logger.info("[FILTER] After Tag=BLOCKING filter   : %d (includes %d forecasted rows)", blocking.sum(), is_forecasted.sum())

    valid_category = df["Step 1 Claim Category"].str.strip().str.title().isin(KEEP_CLAIM_CATEGORIES)
    keep = blocking & (valid_category | is_forecasted)
    logger.info("[FILTER] After Claim Category filter : %d", keep.sum())

    shortlisted = df[keep].reset_index(drop=True)
    shortlisted = shortlisted[[c for c in KEEP_COLUMNS if c in shortlisted.columns]].copy()
    return {
        "shortlisted": shortlisted,
        "full_filtered": shortlisted.copy(),
        "filtered_df": df[keep].copy(),
        "stats": {
            "total": total,
            "jurisdiction": int(in_jurisdiction.sum()),
            "grant_date": int(granted.sum()),
            "blocking": int(blocking.sum()),
            "claim_category": int(keep.sum()),
        },
    }


def years_to_entry_map(df: pd.DataFrame) -> dict:
    """(drug, patent) -> 'Years to Entry' for the US rows of the master table."""
    if "Years to Entry" not in df.columns:
        logger.warning("[LAUNCH] 'Years to Entry' not found in the master rows — using the calculated expiry gap")
        return {}
    mapping = {}
    for _, row in df[df["Jurisdiction"] == "US"].dropna(subset=["Years to Entry"]).iterrows():
        try:
            mapping[(row["Drug Name"], row["Patent Number"])] = float(row["Years to Entry"])
        except (ValueError, TypeError):
            continue
    logger.info("[LAUNCH] 'Years to Entry' known for %d US patent(s)", len(mapping))
    return mapping
