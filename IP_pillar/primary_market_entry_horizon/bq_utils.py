"""
bq_utils.py
───────────
Every BigQuery table this package reads or writes, in one place.

Reads (each returns a DataFrame):
    import_from_gbq(drug)         clinical_efficacy phase rows          (config.CLINICAL_EFFICACY_TABLE)
    fetch_drug_details_df(drug)   vw_drug_details_full rows             (config.DRUG_DETAILS_TABLE)
    fetch_brands_df(drug)         marketed brand names                  (config.BQ_BRANDS_TABLE)
    load_forecasted_loe_table()   the whole forecasted_loe table, once  (config.FORECASTED_LOE_TABLE)

Writes (the LOE output tables glp_patent_loe / glp_master_loe):
    publish_table(df, table_id, drug, run_ts)
        prepare_table()        coerce columns to their BigQuery types, BigQuery-safe names
        bq_schema()            the explicit table schema
        export_table_json()    gs://{GCS_BUCKET}/{GCS_CACHE_PREFIX}/{GCS_JSON_EXPORT}/{table name}/{drug}_{run_ts}.json
        write_table_to_bq()    delete the drug's previous rows (IP_pillar/delete_rows.py), then append — re-runs replace, not duplicate

Reads of glp_master_loe and the Gemini usage rows are shared with the other dimensions: IP_pillar/bq_utils.py.
All BigQuery / GCS access goes through IP_pillar.gcp_utils; table IDs come from IP_pillar.config.
"""

import logging
from functools import lru_cache
from typing import Dict, List, Optional

import pandas as pd
from google.cloud import bigquery

from .. import config
from .. import gcp_utils, gcp_helpers
from ..delete_rows import delete_drug_rows
from ..utils import bq_safe_name, safe_name, stringify_cell

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Reads
# ─────────────────────────────────────────────

def import_from_gbq(drug_name: str) -> pd.DataFrame:
    """
    Fetches raw clinical phase rows for `drug_name` from
    config.CLINICAL_EFFICACY_TABLE: molecule_name, trial_location, phase,
    phase_status. Phase normalisation and jurisdiction splitting happen in
    Python (_normalize_phase / _location_tokens), not SQL.
    """
    table_id = config.CLINICAL_EFFICACY_TABLE
    query = f"""
    SELECT molecule_name, trial_location, phase, phase_status
    FROM `{table_id}`
    WHERE LOWER(REGEXP_REPLACE(COALESCE(molecule_name, ''), r'[\\s\\-_]+', ''))
          = LOWER(REGEXP_REPLACE(@drug_name, r'[\\s\\-_]+', ''))
    """
    try:
        df = gcp_helpers.run_query(query, {"drug_name": drug_name})
    except Exception as e:
        logger.warning(f"[BQ] clinical_efficacy query failed: {e}")
        return pd.DataFrame()
    logger.info(f"[BQ] clinical_efficacy: fetched {len(df)} raw row(s) for '{drug_name}' from {table_id}")
    for _, row in df.head(5).iterrows():
        logger.info(f"[BQ]   {row.get('molecule_name')} | {row.get('trial_location')} | {row.get('phase')}")
    return df


def fetch_drug_details_df(drug_name: str) -> pd.DataFrame:
    """Fetches every config.DRUG_DETAILS_TABLE (vw_drug_details_full) row
    for `drug_name`, matched on cleaned_generic_name ignoring case, spaces,
    hyphens and underscores. Columns are returned as the view defines them."""
    table_id = config.DRUG_DETAILS_TABLE
    query = f"""
    SELECT *
    FROM `{table_id}`
    WHERE LOWER(REGEXP_REPLACE(COALESCE(cleaned_generic_name, ''), r'[\\s\\-_]+', ''))
          = LOWER(REGEXP_REPLACE(@drug_name, r'[\\s\\-_]+', ''))
    """
    try:
        df = gcp_helpers.run_query(query, {"drug_name": drug_name})
    except Exception as e:
        logger.error(f"[DRUG_DETAILS] Query failed for '{drug_name}': {e}")
        return pd.DataFrame()
    logger.info(f"[DRUG_DETAILS] {table_id}: fetched {len(df)} row(s) for '{drug_name}'")
    return df


def fetch_brands_df(drug_name: str) -> pd.DataFrame:
    """Marketed GLP-1 brand rows (cleaned_generic_name, Brand_Name, Drug_Geography)
    for a drug from config.BQ_BRANDS_TABLE. Empty when the table is not
    configured or the query fails."""
    if not config.BQ_BRANDS_TABLE:
        logger.warning("[BRANDS BQ] BQ_BRANDS_TABLE not configured")
        return pd.DataFrame()
    query = f"""
    SELECT DISTINCT cleaned_generic_name, Brand_Name, Drug_Geography
    FROM `{config.BQ_BRANDS_TABLE}`
    WHERE Brand_Name IS NOT NULL
      AND TRIM(Brand_Name) <> ''
      AND LOWER(cleaned_generic_name) = LOWER(@drug_name)
      AND (
        UPPER(cleaned_Target) LIKE '%GLUCAGON LIKE PEPTIDE 1%'
        OR UPPER(cleaned_Target) LIKE '%GLP-1%'
        OR UPPER(cleaned_Target) LIKE '%GLUCAGON LIKE PEPTIDE-1%'
        OR (data_source = 'IPD' AND Mechanism_of_Action = 'Glucagon-like peptide-1 (GLP-1) agonist')
      )
      AND LOWER(highest_development_stage) = 'marketed'
    ORDER BY cleaned_generic_name, Brand_Name
    """
    try:
        df = gcp_helpers.run_query(query, {"drug_name": drug_name})
    except Exception as e:
        logger.error(f"[BRANDS BQ] Query failed for '{drug_name}': {e}")
        return pd.DataFrame()
    logger.info(f"[BRANDS BQ] '{drug_name}' → {len(df)} row(s)")
    return df


@lru_cache(maxsize=1)
def load_forecasted_loe_table() -> pd.DataFrame:
    """Whole forecasted_loe table, loaded once per process (a shard runs many drugs)."""
    df = gcp_helpers.run_query(f"SELECT * FROM `{config.FORECASTED_LOE_TABLE}`")
    logger.info("[MERGE] Loaded %d forecasted_loe row(s) from %s", len(df), config.FORECASTED_LOE_TABLE)
    return df


# ─────────────────────────────────────────────
# Writes — LOE output tables
# ─────────────────────────────────────────────

def short_name(table_id: str) -> str:
    """'project.dataset.table' -> 'table' (used for the JSON snapshot folder)."""
    return table_id.rsplit(".", 1)[-1]


def _coerce(series: pd.Series, bq_type: str) -> pd.Series:
    if bq_type == "INT64":
        return pd.to_numeric(series, errors="coerce").round().astype("Int64")
    if bq_type == "FLOAT64":
        return pd.to_numeric(series, errors="coerce").astype("float64")
    return series.map(stringify_cell).astype(object)


def prepare_table(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce each column to its BigQuery type (config.BQ_COLUMN_TYPES, else
    STRING) and rename columns to BigQuery-safe names."""
    out = pd.DataFrame(index=df.index)
    for col in df.columns:
        out[bq_safe_name(col)] = _coerce(df[col], config.BQ_COLUMN_TYPES.get(col, "STRING"))
    return out.reset_index(drop=True)


def bq_schema(df: pd.DataFrame) -> List[bigquery.SchemaField]:
    """Schema for a frame returned by prepare_table()."""
    types_by_safe_name = {bq_safe_name(k): v for k, v in config.BQ_COLUMN_TYPES.items()}
    return [bigquery.SchemaField(c, types_by_safe_name.get(c, "STRING"), mode="NULLABLE") for c in df.columns]


def export_table_json(table: pd.DataFrame, table_name: str, drug_name: str, run_ts: str) -> str:
    """Write *table* (from prepare_table) as a JSON array of row objects to GCS."""
    payload = table.to_json(orient="records", indent=2, force_ascii=False)
    uri = gcp_utils.write_bytes(
        f"{config.GCS_JSON_EXPORT}/{table_name}",
        f"{safe_name(drug_name)}_{run_ts}.json",
        payload.encode("utf-8"),
        content_type="application/json",
    )
    logger.info("[EXPORT] %s: %d row(s) -> %s", table_name, len(table), uri)
    return uri


def write_table_to_bq(table: pd.DataFrame, target: str, drug_name: str) -> str:
    """Replace *drug_name*'s rows in the BigQuery table with *table*."""
    deleted = delete_drug_rows(target, drug_name)
    gcp_helpers.load_dataframe(table, target, bq_schema(table))
    logger.info("[EXPORT] %s: replaced %d old row(s) with %d row(s) for %r", target, deleted, len(table), drug_name)
    return target


def publish_table(df: pd.DataFrame, table_id: str, drug_name: str, run_ts: str) -> Dict[str, Optional[str]]:
    """Write the JSON snapshot, then the BigQuery rows. Each destination is
    attempted even if the other fails; a failed one is returned as None."""
    table = prepare_table(df)
    table_name = short_name(table_id)
    result: Dict[str, Optional[str]] = {"json_uri": None, "bq_table": None}
    try:
        result["json_uri"] = export_table_json(table, table_name, drug_name, run_ts)
    except Exception:
        logger.exception("[EXPORT] JSON export failed for %s / %r", table_name, drug_name)
    try:
        result["bq_table"] = write_table_to_bq(table, table_id, drug_name)
    except Exception:
        logger.exception("[EXPORT] BigQuery write failed for %s / %r", table_name, drug_name)
    return result
