"""
bq_utils.py
───────────
BigQuery tasks shared by every IP dimension. Anything only one dimension needs lives in that
dimension's own bq_utils.py.

Reads:
    load_master_table(drugs)          glp_master_loe rows, friendly column names   (config.BQ_MASTER_LOE_TABLE)
    load_master_loe(drug)             one drug's glp_master_loe rows, as the analyses use them
    load_patent_loe(drug)             one drug's glp_patent_loe rows (the blocking-analysis patents), friendly column names

Writes:
    replace_drug_rows(df, ...)        delete a drug's rows from a table, then append — re-runs replace, not duplicate
    write_drug_rows(rows, ...)        the same for dict rows, typed to the table's existing schema (table_schema, coerce_to_schema)
    now_iso()                         the format of every updated_at column

Gemini usage (one row per model per drug run):
    append_gemini_stats_to_bigquery(drug, dimension, stats_by_model, run_status, ...)   (config.GEMINI_STATS_TABLE)
    save_gemini_stats(drug, dimension, run_status, error, elapsed)

All BigQuery access goes through IP_pillar.gcp_helpers / gcp_utils; table IDs come from IP_pillar.config.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd
from google.cloud import bigquery

from . import config, gcp_helpers, gcp_utils
from .gemini_utils import get_gemini_call_stats
from .utils import bq_safe_name

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Reads — glp_master_loe
# ─────────────────────────────────────────────

def load_master_table(drug_names: Optional[List[str]] = None) -> pd.DataFrame:
    """Rows of config.BQ_MASTER_LOE_TABLE (optionally for some drugs only),
    with BigQuery column names mapped back to the friendly names
    ('PTE_months' -> 'PTE (months)', ...)."""
    sql = f"SELECT * FROM `{config.BQ_MASTER_LOE_TABLE}`"
    job_config = None
    if drug_names:
        sql += f" WHERE LOWER(TRIM({bq_safe_name('Drug Name')})) IN UNNEST(@drugs)"
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ArrayQueryParameter("drugs", "STRING", [n.strip().lower() for n in drug_names]),
        ])
    df = gcp_utils.get_bq_client().query(sql, job_config=job_config).to_dataframe()

    friendly = {bq_safe_name(name): name for name, _ in config.FINAL_COLUMNS}
    df = df.rename(columns={c: friendly.get(c, c) for c in df.columns})
    logger.info("[REPORT] Loaded %d row(s) from %s", len(df), config.BQ_MASTER_LOE_TABLE)
    return df


def load_master_loe(drug: str) -> pd.DataFrame:
    """The drug's rows from glp_master_loe, with the friendly column names the
    analyses use ('Drug Name', 'Patent Number', 'Source File', ...)."""
    df = load_master_table([drug])
    return df.rename(columns={bq_safe_name("Source File"): "Source File"})


# ─────────────────────────────────────────────
# Reads — glp_patent_loe
# ─────────────────────────────────────────────

# glp_patent_loe columns the analyses read, beyond config.FINAL_COLUMNS.
_EXTRA_COLUMNS = (
    "Source File", "Approval Date Source",
    "Step 2 Matched Elements", "Step 3 Technical Barrier", "Step 3 Confidence", "Step 3 Evidence Type",
    "Step 3 Evidence Summary", "Step 4 Blocking Indicator", "Step 5 Novelty Signal", "Step 5 Reason",
)


def load_patent_loe(drug: str) -> pd.DataFrame:
    """The drug's rows from glp_patent_loe (the blocking-analysis patents, Type="Existing"), with the
    friendly column names the forecast steps use ('Drug Name', 'Patent Number', 'Source File', ...)."""
    sql = (f"SELECT * FROM `{config.BQ_PATENT_LOE_TABLE}` "
           f"WHERE LOWER(TRIM({bq_safe_name('Drug Name')})) = @drug")
    job_config = bigquery.QueryJobConfig(query_parameters=[bigquery.ScalarQueryParameter("drug", "STRING", drug.strip().lower())])
    df = gcp_utils.get_bq_client().query(sql, job_config=job_config).to_dataframe()

    friendly = {bq_safe_name(name): name for name in [n for n, _ in config.FINAL_COLUMNS] + list(_EXTRA_COLUMNS)}
    df = df.rename(columns={c: friendly.get(c, c) for c in df.columns})
    logger.info("[PATENT_LOE] Loaded %d row(s) for %r from %s", len(df), drug, config.BQ_PATENT_LOE_TABLE)
    return df


# ─────────────────────────────────────────────
# Writes
# ─────────────────────────────────────────────

def now_iso() -> str:
    """ISO 8601, microseconds, UTC — the format of every updated_at column."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def replace_drug_rows(df: pd.DataFrame, table_id: str, schema: list, drug: str,
                      drug_column: str = "drug_name") -> None:
    """Delete *drug*'s rows from the table (matched on *drug_column*, ignoring case and spaces),
    then append *df* (re-runs replace, not duplicate)."""
    if df.empty:
        logger.info("No rows to write to %s.", table_id)
        return
    deleted = gcp_helpers.delete_rows(
        table_id,
        f"REGEXP_REPLACE(LOWER({drug_column}), r'\\s+', '') = REGEXP_REPLACE(LOWER(@drug_name), r'\\s+', '')",
        {"drug_name": drug},
    )
    gcp_helpers.load_dataframe(df, table_id, schema)
    logger.info("%s: replaced %d old row(s) with %d row(s) for %r", table_id, deleted, len(df), drug)


def table_schema(table_id: str, default: list) -> list:
    """The table's existing schema plus any default column it lacks (just the default if it does not exist)."""
    try:
        existing = list(gcp_utils.get_bq_client().get_table(table_id).schema)
    except Exception as exc:
        logger.info("[BQ] %s not readable yet (%s) — using the default schema", table_id, type(exc).__name__)
        return list(default)
    known = {field.name for field in existing}
    return existing + [field for field in default if field.name not in known]


def coerce_to_schema(df: pd.DataFrame, schema: list) -> pd.DataFrame:
    """Cast each column to its table type so the load job accepts it."""
    df = df.copy()
    for field in schema:
        if field.name not in df.columns:
            continue
        kind = field.field_type
        if kind == "STRING":
            df[field.name] = df[field.name].map(
                lambda v: None if v is None or (not isinstance(v, str) and pd.isna(v)) else str(v))
        elif kind in ("INTEGER", "INT64"):
            df[field.name] = pd.to_numeric(df[field.name], errors="coerce").round(0).astype("Int64")
        elif kind in ("FLOAT", "FLOAT64", "NUMERIC"):
            df[field.name] = pd.to_numeric(df[field.name], errors="coerce")
        elif kind in ("TIMESTAMP", "DATETIME"):
            # updated_at is an ISO string; a TIMESTAMP column needs real datetimes.
            df[field.name] = pd.to_datetime(df[field.name], errors="coerce", utc=True)
        elif kind == "DATE":
            df[field.name] = pd.to_datetime(df[field.name], errors="coerce").dt.date
    return df


def write_drug_rows(rows: list, table_id: str, default_schema: list, drug: str, drug_column: str = "drug_name") -> None:
    """Replace *drug*'s rows in the table with *rows* (dicts), typed to the table's existing schema
    (new columns of *default_schema* are added) and stamped with created_at."""
    if not rows:
        logger.info("[BQ] No rows to write to %s.", table_id)
        return
    df = pd.DataFrame(rows).drop_duplicates()
    df["created_at"] = pd.Timestamp.now(tz="UTC")
    schema = table_schema(table_id, default_schema)
    df = coerce_to_schema(df, schema)
    replace_drug_rows(df, table_id, [field for field in schema if field.name in df.columns], drug, drug_column=drug_column)


# ─────────────────────────────────────────────
# Gemini usage
# ─────────────────────────────────────────────

GEMINI_STATS_SCHEMA: List[bigquery.SchemaField] = [
    bigquery.SchemaField("molecule_name", "STRING"),
    bigquery.SchemaField("dimension", "STRING"),
    bigquery.SchemaField("model_used", "STRING"),
    bigquery.SchemaField("service_tier", "STRING"),
    bigquery.SchemaField("execution_time_seconds", "FLOAT64"),
    bigquery.SchemaField("input_token_count", "INT64"),
    bigquery.SchemaField("output_token_count", "INT64"),
    bigquery.SchemaField("thought_token_count", "INT64"),
    bigquery.SchemaField("tool_token_count", "INT64"),
    bigquery.SchemaField("total_token_count", "INT64"),
    bigquery.SchemaField("total_num_of_calls", "INT64"),
    bigquery.SchemaField("num_of_search_enabled_calls", "INT64"),
    bigquery.SchemaField("num_of_search_queries", "INT64"),
    bigquery.SchemaField("run_status", "STRING"),
    bigquery.SchemaField("error_message", "STRING"),
    bigquery.SchemaField("cloud_run_job", "STRING"),
    bigquery.SchemaField("cloud_run_task_index", "INT64"),
    bigquery.SchemaField("created_at", "TIMESTAMP"),
]


def append_gemini_stats_to_bigquery(
    molecule_name: str,
    dimension_name: str,
    stats_by_model: List[Dict[str, Any]],
    run_status: str,
    error_message: Optional[str] = None,
    execution_time_seconds: Optional[float] = None,
) -> None:
    """Append one row per model used by a dimension-molecule run."""
    if not stats_by_model:
        logger.info("No Gemini calls recorded for %s/%s; no stats row written", molecule_name, dimension_name)
        return

    common_fields = {
        "molecule_name": molecule_name.strip(),
        "dimension": dimension_name.strip(),
        "execution_time_seconds": execution_time_seconds,
        "run_status": run_status,
        "error_message": error_message,
        "cloud_run_job": os.getenv("CLOUD_RUN_EXECUTION", ""),
        "cloud_run_task_index": config.CLOUD_RUN_TASK_INDEX,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    rows = [{**common_fields, **model_stats} for model_stats in stats_by_model]

    job_config = bigquery.LoadJobConfig(
        schema=GEMINI_STATS_SCHEMA,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        create_disposition=bigquery.CreateDisposition.CREATE_IF_NEEDED,
        schema_update_options=[bigquery.SchemaUpdateOption.ALLOW_FIELD_ADDITION],
    )
    table_id = config.GEMINI_STATS_TABLE
    gcp_utils.get_bq_client().load_table_from_json(rows, table_id, job_config=job_config).result()
    logger.info("Appended %d Gemini stats row(s) for %s/%s to %s", len(rows), molecule_name, dimension_name, table_id)


def save_gemini_stats(drug_name: str, dimension_name: str, run_status: str,
                      error: Optional[str], elapsed: float) -> None:
    """Write this run's Gemini usage (gemini_utils counters) to config.GEMINI_STATS_TABLE; never raises."""
    try:
        append_gemini_stats_to_bigquery(
            drug_name, dimension_name, get_gemini_call_stats(),
            run_status, error_message=error, execution_time_seconds=round(elapsed, 3),
        )
    except Exception:
        logger.exception("Could not write Gemini stats for %s/%s", drug_name, dimension_name)
