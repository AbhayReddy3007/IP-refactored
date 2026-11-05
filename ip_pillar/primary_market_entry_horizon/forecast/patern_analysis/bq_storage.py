"""
bq_storage.py
─────────────
BigQuery table, rows and insert for filing_pattern_table.
"""

from datetime import datetime, timezone
from google.cloud import bigquery
from .... import config, gcp_utils
from .constants import BQ_LOCATION


# ─────────────────────────────────────────────
# ✅ BIGQUERY SETUP
# ─────────────────────────────────────────────
def get_bq_client():
    return gcp_utils.get_bq_client()


def create_dataset_if_not_exists(client):
    dataset_ref = config.BQ_FILING_PATTERN_TABLE.rsplit(".", 1)[0]
    dataset = bigquery.Dataset(dataset_ref)
    dataset.location = BQ_LOCATION

    try:
        client.get_dataset(dataset_ref)
        print("[BQ] Dataset exists")
    except Exception:
        client.create_dataset(dataset)
        print("[BQ] Dataset created")


def create_table_if_not_exists(client):
    table_ref = config.BQ_FILING_PATTERN_TABLE

    table = bigquery.Table(table_ref, schema=FILING_PATTERN_SCHEMA)

    try:
        client.get_table(table_ref)
        print("[BQ] Table exists")
    except Exception:
        client.create_table(table)
        print("[BQ] Table created")


FILING_PATTERN_SCHEMA = [
    bigquery.SchemaField("drug", "STRING"),
    bigquery.SchemaField("company", "STRING"),
    bigquery.SchemaField("characterization", "STRING"),
    bigquery.SchemaField("confidence", "STRING"),
    bigquery.SchemaField("rationale", "STRING"),
    bigquery.SchemaField("created_at", "TIMESTAMP"),
]


def filing_pattern_rows(drug_name, result) -> list:
    """One filing_pattern_table row per innovator in an analyze_innovator_filing_patterns() result."""
    return [{
        "drug": drug_name,
        "company": inv.get("company"),
        "characterization": inv.get("characterization"),
        "confidence": str(inv.get("confidence")),
        "rationale": inv.get("rationale"),
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
    } for inv in result.get("innovators", [])]


def insert_into_bq(client, drug_name, result):
    table_ref = config.BQ_FILING_PATTERN_TABLE

    rows = filing_pattern_rows(drug_name, result)

    if rows:
        errors = client.insert_rows_json(table_ref, rows)
        if errors:
            print(f"[BQ ERROR] {errors}")
        else:
            print(f"[BQ] Inserted {len(rows)} rows for {drug_name}")
