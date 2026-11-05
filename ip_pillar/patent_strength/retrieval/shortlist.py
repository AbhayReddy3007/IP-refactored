"""
shortlist.py
Selects the patents to score from a drug's glp_master_loe rows.

Keeps patents where Tag = 'Blocking', a Grant Date is present and Type is not
'Forecasted'; then keeps only patents patent_master approves for the molecule.
"""

import logging

import pandas as pd

from ...patent_filter import get_approved_patent_numbers
from ...utils import normalize_patent_number

logger = logging.getLogger(__name__)


def _select_blocking_granted(df: pd.DataFrame) -> pd.DataFrame:
    is_blocking = df["Tag"].astype(str).str.strip().str.lower() == "blocking"
    is_granted = df["Grant Date"].notna() & (df["Grant Date"].astype(str).str.strip() != "")
    is_not_forecasted = df["Type"].astype(str).str.strip().str.lower() != "forecasted"

    shortlist = (
        df[is_blocking & is_granted & is_not_forecasted]
        .drop_duplicates(subset=["Patent Number"])
        .reset_index(drop=True)
    )
    logger.info(
        "Shortlisted %d patents from glp_master_loe "
        "(Tag=Blocking AND Grant Date present AND Type!=Forecasted).",
        len(shortlist),
    )
    return shortlist


def _apply_approval_filter(df: pd.DataFrame, drug: str) -> pd.DataFrame:
    """Keep patents patent_master approves for *drug* (patent_status + confidence).
    If patent_master returns nothing (e.g. the query failed) the shortlist is kept as is."""
    approved = {normalize_patent_number(n) for n in get_approved_patent_numbers(drug)}
    if not approved:
        logger.warning("[PATENT FILTER] No approved patents returned for '%s' — approval filter skipped", drug)
        return df
    before = len(df)
    df = df[df["Patent Number"].map(normalize_patent_number).isin(approved)].reset_index(drop=True)
    logger.info("[PATENT FILTER] %d -> %d patents after approval filter.", before, len(df))
    return df


def _warn_missing_source_files(df: pd.DataFrame) -> None:
    missing = df[df["Source File"].astype(str).str.strip() == ""]
    if missing.empty:
        return
    logger.warning(
        "%d shortlisted patent(s) have no Source File value; "
        "they will return empty chunks and score as N/A:",
        len(missing),
    )
    for _, row in missing.iterrows():
        logger.warning("  - %s (%s)", row.get("Patent Number", "N/A"), row.get("Drug Name", "N/A"))


def build_shortlist(df: pd.DataFrame, drug: str) -> pd.DataFrame:
    """Return the patents of *drug* to score.

    Raises:
        ValueError: if the input has no ``Source File`` column.
    """
    df = df.copy()
    df.columns = [c.strip() for c in df.columns]

    if "Source File" not in df.columns:
        raise ValueError(
            "glp_master_loe is missing a 'Source_File' column. "
            "Re-run the primary_market_entry_horizon dimension for this drug."
        )

    shortlist = _select_blocking_granted(df)
    if not shortlist.empty:
        shortlist = _apply_approval_filter(shortlist, drug)
    if not shortlist.empty:
        _warn_missing_source_files(shortlist)
    return shortlist
