"""
bq_utils.py
───────────
The BigQuery tasks specific to the global launch sequencing and geographic arbitrage dimension
(the glp_master_loe read and the replace-a-drug's-rows write are shared: IP_pillar/bq_utils.py).

Reads:
    load_analysis_cache(drug)         approval dates and PTE lookups of the drug's previous run
                                      (config.BQ_LAUNCH_PATENTS_TABLE), so a re-run does not repeat Gemini calls

Writes (each run replaces only that drug's rows, so re-runs never duplicate):
    write_launch_patents(drug, df)    one row per shortlisted jurisdiction patent   (config.BQ_LAUNCH_PATENTS_TABLE)
    write_arbitrage(drug, df, rationale)   one row per jurisdiction, in launch order, each with the drug's
                                      rationale; Dimension IV Score is stored as Score  (config.BQ_ARBITRAGE_TABLE)

All BigQuery access goes through IP_pillar.gcp_helpers / the shared bq_utils; table IDs come from IP_pillar.config.
"""

import logging
import re

import pandas as pd
from google.cloud import bigquery

from .. import config, gcp_helpers
from ..bq_utils import now_iso, write_drug_rows
from ..utils import is_blank, stringify_cell

logger = logging.getLogger(__name__)

_DRUG_MATCH = "REGEXP_REPLACE(LOWER(drug_name), r'\\s+', '') = REGEXP_REPLACE(LOWER(@drug_name), r'\\s+', '')"

_PATENT_COLUMNS = (
    "Drug Name", "Patent Number", "Jurisdiction", "Tag", "Step 1 Claim Category",
    "Filing Date", "Grant Date", "Approval Date", "Base Patent Term (Filing+20y)",
    "PTE Status", "PTE Months (Granted)", "PTE Source", "Adjusted Expiry (with PTE)",
    "PTE Capped (Post-Approval Cap)", "Expiry Gap (Years)", "Dimension IV Score", "Dimension IV Rating",
)
_ARBITRAGE_COLUMNS = (
    "Drug Name", "Launch Order", "Jurisdiction", "Product LOE (Year)", "Gap vs US (Years)",
    "Gap vs Longest LOE (Years)", "Country-to-Country Arbitrage (Years)", "Key Protection Gap",
    "Arbitrage Score", "Arbitrage Signal", "Dimension IV Score", "Dimension IV Rating",
)
# Columns stored under another name in the arbitrage table.
_ARBITRAGE_RENAMES = {"Dimension IV Score": "Score"}


def snake_case(name: str) -> str:
    """'PTE Months (Granted)' -> 'pte_months_granted'."""
    return re.sub(r"[^0-9a-zA-Z]+", "_", name.strip()).strip("_").lower()


# Default schemas, used when a table does not exist yet. Values keep their text form (some columns mix
# numbers and 'N/A'), so every column is a STRING; created_at is what lets a later run find the latest row.
LAUNCH_PATENTS_SCHEMA = [
    *[bigquery.SchemaField(snake_case(c), "STRING") for c in _PATENT_COLUMNS],
    bigquery.SchemaField("updated_at", "STRING"),
    bigquery.SchemaField("created_at", "TIMESTAMP"),
]
ARBITRAGE_SCHEMA = [
    *[bigquery.SchemaField(snake_case(_ARBITRAGE_RENAMES.get(c, c)), "STRING") for c in _ARBITRAGE_COLUMNS],
    bigquery.SchemaField("rationale", "STRING"),
    bigquery.SchemaField("updated_at", "STRING"),
    bigquery.SchemaField("created_at", "TIMESTAMP"),
]


# ─────────────────────────────────────────────
# Read — the previous run's lookups
# ─────────────────────────────────────────────

def load_analysis_cache(drug: str) -> tuple[dict, dict]:
    """(approval_cache, pte_cache) from the drug's previous run:
        approval_cache: {jurisdiction: approval date}
        pte_cache:      {(patent number, jurisdiction): {"pte_status", "pte_months", "pte_source"}}
    glp_master_loe never gets the looked-up approval dates / PTE written back, so without this every
    re-run would issue the same Gemini calls. Both are empty when there is no earlier run."""
    approval_cache: dict = {}
    pte_cache: dict = {}
    try:
        df = gcp_helpers.run_query(
            f"SELECT patent_number, jurisdiction, approval_date, pte_status, pte_months_granted, pte_source "
            f"FROM `{config.BQ_LAUNCH_PATENTS_TABLE}` WHERE {_DRUG_MATCH}",
            {"drug_name": drug},
        )
    except Exception as exc:
        logger.info("[LAUNCH] No earlier analysis in %s (%s) — all lookups run fresh", config.BQ_LAUNCH_PATENTS_TABLE, type(exc).__name__)
        return approval_cache, pte_cache

    for _, row in df.iterrows():
        jurisdiction, patent = str(row["jurisdiction"]).strip(), str(row["patent_number"]).strip()
        if not is_blank(row["approval_date"]) and str(row["approval_date"]).strip() != "Not found":
            approval_cache.setdefault(jurisdiction, str(row["approval_date"]).strip())
        if patent and not is_blank(row["pte_status"]) and not str(row["pte_status"]).startswith("Overridden"):
            months = pd.to_numeric(row["pte_months_granted"], errors="coerce")
            pte_cache[(patent, jurisdiction)] = {
                "pte_status": str(row["pte_status"]).strip(),
                "pte_months": 0 if pd.isna(months) else int(months),
                "pte_source": "" if is_blank(row["pte_source"]) else str(row["pte_source"]).strip(),
            }
    logger.info("[LAUNCH] Reusing %d approval date(s) and %d PTE lookup(s) from the previous run",
                len(approval_cache), len(pte_cache))
    return approval_cache, pte_cache


# ─────────────────────────────────────────────
# Writes
# ─────────────────────────────────────────────

def _rows(df: pd.DataFrame, columns: tuple, renames: dict = None, **extra) -> list:
    """The frame's rows as dicts with snake_case keys (columns in *renames* stored under the new name) and
    text values, plus the *extra* fields, stamped with updated_at."""
    renames = renames or {}
    updated_at = now_iso()
    rows = []
    for record in df.reindex(columns=list(columns)).to_dict("records"):
        rows.append({**{snake_case(renames.get(c, c)): stringify_cell(v) for c, v in record.items()},
                     **extra, "updated_at": updated_at})
    return rows


def write_launch_patents(drug: str, df: pd.DataFrame) -> None:
    """Replace the drug's rows in config.BQ_LAUNCH_PATENTS_TABLE with the shortlist."""
    write_drug_rows(_rows(df, _PATENT_COLUMNS), config.BQ_LAUNCH_PATENTS_TABLE, LAUNCH_PATENTS_SCHEMA, drug)


def write_arbitrage(drug: str, df: pd.DataFrame, rationale: str = "") -> None:
    """Replace the drug's rows in config.BQ_ARBITRAGE_TABLE with the arbitrage summary; every row carries
    the drug's rationale, and the Dimension IV score is stored as Score."""
    rows = _rows(df, _ARBITRAGE_COLUMNS, _ARBITRAGE_RENAMES, rationale=rationale)
    write_drug_rows(rows, config.BQ_ARBITRAGE_TABLE, ARBITRAGE_SCHEMA, drug)
