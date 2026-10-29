"""Load, clean and filter a drug's glp_master_loe rows, and select its NON-BLOCKING patents."""

import logging

import pandas as pd

from ...bq_utils import load_master_loe
from ...patent_filter import get_approved_patent_numbers
from ...utils import normalize_patent_number
from ..scoring.constants import EXCLUDED_CATEGORIES

logger = logging.getLogger(__name__)

_FORECASTED_COLUMN_NAMES = ("noofforecastedpatents", "numberofforecastedpatents", "forecastedpatents", "noofforecasted")


def _apply_approval_filter(df: pd.DataFrame, drug: str) -> pd.DataFrame:
    """Keep the patents patent_master approves for *drug*. Forecasted rows are not real patents
    and are kept as they are. If patent_master returns nothing the rows are kept as they are."""
    approved = {normalize_patent_number(n) for n in get_approved_patent_numbers(drug)}
    if not approved:
        logger.warning("[PATENT FILTER] No approved patents returned for '%s' — approval filter skipped", drug)
        return df
    forecasted = df["Type"].str.lower() == "forecasted"
    keep = forecasted | df["Patent Number"].map(normalize_patent_number).isin(approved)
    logger.info("[PATENT FILTER] %d -> %d rows after the approval filter", len(df), int(keep.sum()))
    return df[keep]


def load_master_data(drug: str) -> pd.DataFrame:
    """The drug's glp_master_loe rows, normalised, with only approved, de-duplicated patents."""
    df = load_master_loe(drug)
    if df.empty:
        return df
    df.columns = df.columns.str.strip()
    df = df.drop_duplicates().copy()
    df["Drug Name"] = drug  # one spelling of the drug, whatever case the rows were written with

    for col in ["Tag", "Step 1 Claim Category", "Phase", "Type", "Jurisdiction"]:
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()
        else:
            logger.warning("Expected column '%s' not found in glp_master_loe.", col)
            df[col] = ""

    # "Method of Treatment" and "Method of treatment" collapse to one category.
    df["Step 1 Claim Category"] = df["Step 1 Claim Category"].str.title()

    # "No Of Forecasted Patents" must be numeric (default 0).
    forecasted_col = next(
        (c for c in df.columns if c.lower().replace(" ", "").replace("_", "") in _FORECASTED_COLUMN_NAMES), None)
    if forecasted_col is None:
        logger.warning("'No Of Forecasted Patents' column not found — treating it as 0.")
        df["No Of Forecasted Patents"] = 0
    else:
        df["No Of Forecasted Patents"] = pd.to_numeric(df[forecasted_col], errors="coerce").fillna(0).astype(int)

    df = _apply_approval_filter(df, drug)
    real = df["Type"].str.lower() != "forecasted"
    deduped = df[real].assign(_key=lambda d: d["Patent Number"].map(normalize_patent_number)).drop_duplicates("_key")
    return pd.concat([deduped.drop(columns="_key"), df[~real]], ignore_index=True)


def get_claim_categories(df: pd.DataFrame) -> list:
    """Sorted Step 1 claim categories present in the data, minus the excluded ones."""
    return sorted(
        c for c in df["Step 1 Claim Category"].replace("nan", pd.NA).dropna().unique()
        if c not in EXCLUDED_CATEGORIES
    )


def select_non_blocking(df: pd.DataFrame, drug_name: str) -> pd.DataFrame:
    """NON-BLOCKING rows of one drug (empty if it has none)."""
    non_blocking = df[
        (df["Tag"].str.upper() == "NON-BLOCKING")
        & (df["Drug Name"].str.lower() == drug_name.strip().lower())
    ].copy()
    if non_blocking.empty:
        logger.info("No NON-BLOCKING rows found for drug: '%s'", drug_name)
    else:
        logger.info("[Filter] Processing drug: '%s' (%d NON-BLOCKING rows)", drug_name, len(non_blocking))
    return non_blocking
