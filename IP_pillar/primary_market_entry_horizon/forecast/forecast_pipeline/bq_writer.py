"""
bq_writer.py
────────────
Writes the scored forecast to BigQuery.
"""

from typing import Dict, List
import pandas as pd
from .bq_loaders import _get_bq_client
from .constants import BQ_OUTPUT_TABLE


# ─────────────────────────────────────────────
# Write scored results to BigQuery
# ─────────────────────────────────────────────

def _write_to_bq(all_scored: List[Dict], table_id: str = None):
    """Writes all scored forecast rows to a BigQuery table."""
    table_id = table_id or BQ_OUTPUT_TABLE

    if not all_scored:
        print("[BQ OUTPUT] No scored data to write")
        return

    df = pd.DataFrame(all_scored)

    # Ensure consistent column types
    int_cols = ["filing_date_lower", "controlling_patent_expiry_year",
                "no_of_forecasted_patents", "ip_dimension_1_score"]
    float_cols = ["years_to_entry", "avg_years_to_entry_us_ep", "avg_years_to_entry",
                  "est_approval_year", "exclusivity_year"]

    for c in int_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")

    for c in float_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    try:
        from google.cloud import bigquery

        client = _get_bq_client()

        job_config = bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            autodetect=True,
        )

        job = client.load_table_from_dataframe(df, table_id, job_config=job_config)
        job.result()  # Wait for completion

        table = client.get_table(table_id)
        print(f"\n[BQ OUTPUT] Written {table.num_rows} rows to {table_id}")
        print(f"[BQ OUTPUT] Schema: {len(table.schema)} columns")

    except Exception as e:
        print(f"[BQ OUTPUT] Failed to write to {table_id}: {e}")
        # Fallback: save locally
        fallback_path = "forecasted_loe_fallback.parquet"
        df.to_parquet(fallback_path, index=False)
        print(f"[BQ OUTPUT] Saved fallback to {fallback_path}")
