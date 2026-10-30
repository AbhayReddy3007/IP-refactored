"""The thicket's view of a drug's glp_master_loe rows: its claim categories and NON-BLOCKING patents."""

import logging

import pandas as pd

from ..scoring.constants import EXCLUDED_CATEGORIES

logger = logging.getLogger(__name__)


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
