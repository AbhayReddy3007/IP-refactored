"""
bq_utils.py
───────────
BigQuery tasks specific to the litigation dimension (shared ones: IP_pillar/bq_utils.py).

The litigation table (config.BQ_LITIGATION_TABLE) holds one row per case, and one row with no case for a
patent that was searched and has none, so a patent is never searched twice.

Reads:
    load_existing_rows(drug)          the drug's rows from earlier runs, to skip the patents already in the table

Rows:
    build_rows(...)                   every row of the drug: the earlier ones plus this run's, with the drug-level columns

Writes:
    write_litigation(drug, rows)      the drug's rows, written before its older ones are deleted, so a failed
                                      run never leaves the drug without rows
"""

import json
import logging
from typing import Dict, List, Optional

import pandas as pd
from google.cloud.bigquery import SchemaField

from .. import config, gcp_helpers
from ..bq_utils import coerce_to_schema, now_iso, table_schema
from ..utils import normalize_patent_number

logger = logging.getLogger(__name__)

# Explicit schema avoids autodetect mis-typing nullable string columns. New columns are added to an existing table.
LITIGATION_SCHEMA = [
    SchemaField("drug_name", "STRING", mode="REQUIRED"),
    SchemaField("innovator", "STRING"),
    SchemaField("brand_names", "STRING"),            # JSON array as text; only looked up for a Marketed drug
    SchemaField("patent_number", "STRING"),
    SchemaField("filing_date", "STRING"),            # the patent's, from glp_patent_loe
    SchemaField("case_type", "STRING"),
    SchemaField("challenger", "STRING"),
    SchemaField("case_number", "STRING"),
    SchemaField("court", "STRING"),
    SchemaField("status", "STRING"),
    SchemaField("outcome", "STRING"),
    SchemaField("source_url", "STRING"),
    SchemaField("summary", "STRING"),
    SchemaField("analysis_date", "DATE"),
    SchemaField("search_time_seconds", "FLOAT64"),
    SchemaField("total_cases", "INT64"),
    SchemaField("unique_challengers", "INT64"),
    SchemaField("loaded_at", "TIMESTAMP", mode="REQUIRED"),
]

COLUMNS = [field.name for field in LITIGATION_SCHEMA]
CASE_FIELDS = ("case_type", "challenger", "case_number", "court", "status", "outcome", "source_url")

_SAME_DRUG = "REGEXP_REPLACE(LOWER(drug_name), r'\\s+', '') = REGEXP_REPLACE(LOWER(@drug_name), r'\\s+', '')"


# ─────────────────────────────────────────────
# Reads
# ─────────────────────────────────────────────

def load_existing_rows(drug: str) -> List[Dict]:
    """The drug's rows from earlier runs (empty if the table does not exist yet). A failed read raises, so a
    transient error can never look like "nothing searched yet"."""
    if not gcp_helpers.table_exists(config.BQ_LITIGATION_TABLE):
        return []
    df = gcp_helpers.run_query(f"SELECT * FROM `{config.BQ_LITIGATION_TABLE}` WHERE {_SAME_DRUG}", {"drug_name": drug})
    df = df[[c for c in COLUMNS if c in df.columns]].astype(object)
    rows = df.where(df.notna(), None).to_dict("records")
    logger.info("[LITIGATION] %d row(s) already in %s for %r", len(rows), config.BQ_LITIGATION_TABLE, drug)
    return rows


# ─────────────────────────────────────────────
# Rows
# ─────────────────────────────────────────────

def _case_row(case: Dict) -> Dict:
    return {"patent_number": case["patent_number"], **{field: str(case.get(field) or "") for field in CASE_FIELDS}}


def merge_case_rows(existing: List[Dict], cases: List[Dict], searched: List[str]) -> List[Dict]:
    """Patent rows of the drug: the earlier rows that name a patent, a row per new case, and a row with no case
    for every newly searched patent that has none."""
    rows = [row for row in existing if row.get("patent_number")]
    rows += [_case_row(case) for case in cases]
    with_cases = {normalize_patent_number(case["patent_number"]) for case in cases}
    rows += [{"patent_number": patent, **{field: None for field in CASE_FIELDS}}
             for patent in searched if normalize_patent_number(patent) not in with_cases]
    return rows


def build_rows(
    drug: str,
    case_rows: List[Dict],
    summary: str,
    innovator: str,
    brand_names: List[str],
    filing_dates: Dict[str, str],
    analysis_date: str,
    search_time_seconds: float,
) -> List[Dict]:
    """The drug's final rows: *case_rows* with the drug-level columns, the patent's filing date and the totals."""
    cases = [row for row in case_rows if row.get("case_type")]
    challengers = {str(row["challenger"]).strip().lower() for row in cases if row.get("challenger")}
    common = {
        "drug_name":           drug,
        "innovator":           innovator or None,
        "brand_names":         json.dumps(brand_names) if brand_names else None,
        "summary":             summary,
        "analysis_date":       analysis_date,
        "search_time_seconds": search_time_seconds,
        "total_cases":         len(cases),
        "unique_challengers":  len(challengers),
        "loaded_at":           now_iso(),
    }
    rows = []
    for row in case_rows:
        number = normalize_patent_number(row["patent_number"])
        filing_date: Optional[str] = filing_dates.get(number) or row.get("filing_date")
        rows.append({**common, **{field: row.get(field) for field in ("patent_number", *CASE_FIELDS)}, "filing_date": filing_date})
    return rows


# ─────────────────────────────────────────────
# Writes
# ─────────────────────────────────────────────

def write_litigation(drug: str, rows: List[Dict]) -> int:
    """Write the drug's *rows*, then delete its older rows (those loaded before this write). Returns the rows written."""
    if not rows:
        return 0
    schema = table_schema(config.BQ_LITIGATION_TABLE, LITIGATION_SCHEMA)
    df = coerce_to_schema(pd.DataFrame(rows)[COLUMNS], schema)
    gcp_helpers.load_dataframe(df, config.BQ_LITIGATION_TABLE, [field for field in schema if field.name in df.columns])

    run_ts = rows[0]["loaded_at"]
    deleted = gcp_helpers.delete_rows(
        config.BQ_LITIGATION_TABLE,
        f"{_SAME_DRUG} AND (loaded_at IS NULL OR loaded_at < TIMESTAMP(@run_ts))",
        {"drug_name": drug, "run_ts": run_ts},
    )
    logger.info("%s: wrote %d row(s) for %r, replacing %d older row(s)", config.BQ_LITIGATION_TABLE, len(df), drug, deleted)
    return len(df)
