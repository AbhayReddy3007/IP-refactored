"""
dim_scores.py
─────────────
Keeps the dim_scores_table (config.DIM_SCORES_TABLE) up to date: one row per drug per scored IP dimension.

    product    the drug name
    dimension  the dimension name (config.DIMENSION_*)
    pillar     "IP"
    score      the dimension's drug-level score
    rationale  the dimension's rationale
    timestamp  created_at (else updated_at) of the dimension's rows

The four scored dimensions are Primary Market Entry Horizon, Patent Strength and Invalidity Opportunity,
Patent Thicket and Circumvention Feasibility, and Global Launch Sequencing & Arbitrage. Litigations has no
score, so it is not here.

update_dim_score(drug, dimension) is called by utils.store_payload after each dimension run succeeds. It reads
the score, rationale and timestamp back from the tables that dimension just wrote, then replaces the
(drug, dimension) row, so a re-run never duplicates and one dimension's run leaves the others' rows alone.
"""

import logging
import re
from typing import Callable, Dict, Optional, Tuple

import pandas as pd

from . import config, gcp_helpers, gcp_utils
from .bq_utils import load_master_table

logger = logging.getLogger(__name__)

PILLAR = "IP"

_DRUG_MATCH = "REGEXP_REPLACE(LOWER({col}), r'\\s+', '') = REGEXP_REPLACE(LOWER(@drug_name), r'\\s+', '')"

# (score, rationale, timestamp); any may be None.
Extracted = Tuple[Optional[float], Optional[str], Optional[pd.Timestamp]]


def _norm(name: str) -> str:
    return re.sub(r"[^0-9a-z]", "", str(name).lower())


def _col(df: pd.DataFrame, name: str) -> Optional[str]:
    """The frame's column matching *name* ignoring case, spaces and punctuation (None if absent)."""
    target = _norm(name)
    return next((c for c in df.columns if _norm(c) == target), None)


def _first_number(df: pd.DataFrame, name: str) -> Optional[float]:
    col = _col(df, name)
    if col is None:
        return None
    values = pd.to_numeric(df[col], errors="coerce").dropna()
    return float(values.iloc[0]) if not values.empty else None


def _first_text(df: pd.DataFrame, name: str) -> Optional[str]:
    col = _col(df, name)
    if col is None:
        return None
    for value in df[col]:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _latest_timestamp(df: pd.DataFrame) -> Optional[pd.Timestamp]:
    """created_at if the rows have it, otherwise updated_at: the latest value."""
    for name in ("created_at", "updated_at"):
        col = _col(df, name)
        if col is None:
            continue
        stamps = pd.to_datetime(df[col], errors="coerce", utc=True).dropna()
        if not stamps.empty:
            return stamps.max()
    return None


def _from_frame(df: pd.DataFrame, score_col: str, rationale_col: str) -> Extracted:
    if df.empty:
        return None, None, None
    return _first_number(df, score_col), _first_text(df, rationale_col), _latest_timestamp(df)


def _query(table_id: str, drug: str, drug_col: str, extra_where: str = "") -> pd.DataFrame:
    sql = f"SELECT * FROM `{table_id}` WHERE {_DRUG_MATCH.format(col=drug_col)}{extra_where}"
    return gcp_helpers.run_query(sql, {"drug_name": drug})


# ─────────────────────────────────────────────
# One reader per dimension
# ─────────────────────────────────────────────

def _horizon(drug: str) -> Extracted:
    # The score and rationale are stamped on every glp_master_loe row of the drug.
    return _from_frame(load_master_table([drug]), "IP Dimension 1 Score", "Rationale")


def _strength(drug: str) -> Extracted:
    # final_patent_score and rationale are repeated on each country row.
    return _from_frame(_query(config.BQ_COUNTRY_SCORE_TABLE, drug, "drug_name"), "final_patent_score", "rationale")


def _thicket(drug: str) -> Extracted:
    # The drug-level score is the 'Final Score (Average)' row, not a jurisdiction row.
    df = _query(config.BQ_THICKET_SCORE_TABLE, drug, "Drug_Name", " AND Jurisdiction = 'Final Score (Average)'")
    return _from_frame(df, "Score", "Rationale")


def _launch(drug: str) -> Extracted:
    # Score (the Dimension IV score) and rationale are repeated on each jurisdiction row.
    return _from_frame(_query(config.BQ_ARBITRAGE_TABLE, drug, "drug_name"), "score", "rationale")


_READERS: Dict[str, Callable[[str], Extracted]] = {
    config.DIMENSION_PRIMARY_MARKET_ENTRY_HORIZON: _horizon,
    config.DIMENSION_PATENT_STRENGTH:              _strength,
    config.DIMENSION_PATENT_THICKET:               _thicket,
    config.DIMENSION_GLOBAL_LAUNCH:                _launch,
}


# ─────────────────────────────────────────────
# Write
# ─────────────────────────────────────────────

def update_dim_score(drug: str, dimension: str) -> bool:
    """Replace the (drug, dimension) row of dim_scores_table with the dimension's latest score. Does nothing for a
    dimension without a score (litigations). Never raises: a failure is logged and returns False."""
    reader = _READERS.get(dimension)
    if reader is None:
        return False
    try:
        score, rationale, stamp = reader(drug)
        if score is None and not rationale:
            logger.warning("[DIM_SCORE] No score or rationale found for %r / %r — row not written", drug, dimension)
            return False
        gcp_utils.upsert_dimension_score_to_bigquery(
            drug, dimension, score, pillar_name=PILLAR, rationale=rationale, timestamp=stamp)
        return True
    except Exception:
        logger.exception("[DIM_SCORE] Could not update dim_scores_table for %r / %r", drug, dimension)
        return False
