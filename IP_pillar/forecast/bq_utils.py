"""
bq_utils.py
───────────
BigQuery tasks specific to the forecast_analysis dimension (shared ones: IP_pillar/bq_utils.py).

Reads:
    load_patent_loe(drug)           one drug's glp_patent_loe rows (shared: IP_pillar/bq_utils.py)
    load_filing_pattern(drug)       the drug's rows from filing_pattern_table, if an earlier run wrote them
    load_company_analysis(company)  the company's row from company_analysis_table, if an earlier run wrote it

Writes (each run replaces only that drug's / company's rows):
    write_forecast_s3(drug, df)               forecast_s3            patent layering + filing stage, one row per patent
    write_filing_pattern(drug, rows)          filing_pattern_table   innovator filing patterns, one row per innovator
    write_company_analysis(company, row)      company_analysis_table the company's business-strategy assessment
    write_forecasted_loe(drug, scored)        forecasted_loe         the scored forecast, merged into glp_master_loe by the next
                                                                     primary_market_entry_horizon run
"""

import logging
from typing import Dict, List

import pandas as pd
from google.cloud import bigquery

from .. import config, gcp_utils
from ..bq_utils import coerce_to_schema, load_patent_loe, replace_drug_rows, table_schema, write_drug_rows

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Reads — earlier runs of this dimension
# ─────────────────────────────────────────────

def _normalised_rows(table_id: str, column: str, value: str) -> List[Dict]:
    """Rows of *table_id* whose *column* equals *value*, ignoring case and spaces; [] if the table is missing."""
    sql = (f"SELECT * FROM `{table_id}` WHERE REGEXP_REPLACE(LOWER({column}), r'\\s+', '') = "
           f"REGEXP_REPLACE(LOWER(@value), r'\\s+', '')")
    job_config = bigquery.QueryJobConfig(query_parameters=[bigquery.ScalarQueryParameter("value", "STRING", value)])
    try:
        return gcp_utils.get_bq_client().query(sql, job_config=job_config).to_dataframe().to_dict("records")
    except Exception as exc:
        logger.info("[FORECAST] %s not readable (%s) - treating as empty", table_id, type(exc).__name__)
        return []


def load_filing_pattern(drug: str) -> List[Dict]:
    """The drug's filing_pattern_table rows (one per innovator), [] if none."""
    return _normalised_rows(config.BQ_FILING_PATTERN_TABLE, "drug", drug)


def load_company_analysis(company: str) -> Dict:
    """The company's company_analysis_table row (the latest if there are several), {} if none."""
    rows = _normalised_rows(config.BQ_COMPANY_ANALYSIS_TABLE, "company", company)
    return max(rows, key=lambda r: str(r.get("inserted_at") or "")) if rows else {}


# ─────────────────────────────────────────────
# Writes
# ─────────────────────────────────────────────

def _default_schema(df: pd.DataFrame) -> List[bigquery.SchemaField]:
    """STRING / INT64 / FLOAT64 columns by dtype — used only when the table does not exist yet."""
    def kind(series: pd.Series) -> str:
        if pd.api.types.is_integer_dtype(series):
            return "INT64"
        if pd.api.types.is_float_dtype(series):
            return "FLOAT64"
        return "STRING"
    return [bigquery.SchemaField(col, kind(df[col])) for col in df.columns]


def write_forecast_s3(drug: str, forecast_s3: pd.DataFrame) -> None:
    """Replace the drug's rows in forecast_s3."""
    if forecast_s3.empty:
        logger.info("[FORECAST] No forecast_s3 rows to write for %r", drug)
        return
    df = forecast_s3.copy()
    df["created_at"] = pd.Timestamp.now(tz="UTC")
    schema = table_schema(config.BQ_FORECAST_S3_TABLE, _default_schema(df))
    df = coerce_to_schema(df, schema)
    replace_drug_rows(df, config.BQ_FORECAST_S3_TABLE, [f for f in schema if f.name in df.columns], drug)


def write_filing_pattern(drug: str, rows: List[Dict]) -> None:
    """Replace the drug's rows in filing_pattern_table (its column is 'drug', not 'drug_name')."""
    if not rows:
        logger.info("[FORECAST] No filing-pattern rows to write for %r", drug)
        return
    df = pd.DataFrame(rows)
    schema = table_schema(config.BQ_FILING_PATTERN_TABLE, _default_schema(df))
    df = coerce_to_schema(df, schema)
    replace_drug_rows(df, config.BQ_FILING_PATTERN_TABLE, [f for f in schema if f.name in df.columns], drug, drug_column="drug")


def write_company_analysis(company: str, row: Dict, schema: List[bigquery.SchemaField]) -> None:
    """Replace the company's row in company_analysis_table (company-level, shared by its drugs)."""
    df = pd.DataFrame([row])
    full_schema = table_schema(config.BQ_COMPANY_ANALYSIS_TABLE, schema)
    df = coerce_to_schema(df, full_schema)
    replace_drug_rows(df, config.BQ_COMPANY_ANALYSIS_TABLE, [f for f in full_schema if f.name in df.columns],
                      company, drug_column="company")


def write_forecasted_loe(drug: str, scored: List[Dict]) -> None:
    """Replace the drug's rows in forecasted_loe with the scored forecast."""
    if not scored:
        logger.info("[FORECAST] No scored rows to write for %r", drug)
        return
    write_drug_rows(scored, config.FORECASTED_LOE_TABLE, _default_schema(pd.DataFrame(scored)), drug)
