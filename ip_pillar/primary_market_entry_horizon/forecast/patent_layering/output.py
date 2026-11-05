"""
output.py
─────────
Merge all steps into forecast_s3 and upload it to BigQuery.
"""

from pathlib import Path
import pandas as pd
from google.cloud import bigquery
from .... import config, gcp_utils
from .constants import _BQ_CLIENT


# ─────────────────────────────────────────────
# Output paths & BQ target
# ─────────────────────────────────────────────

OUTPUT_DIR        = None  # No longer used — output goes to GCS
OUTPUT_FILE       = None
_FORECAST_S3_BQ   = config.BQ_FORECAST_S3_TABLE


def _save_excel(df: pd.DataFrame, path: str, sheet_name: str) -> None:
    """Write an Excel file to GCS instead of local disk."""
    try:
        import io
        gcs_cache = gcp_utils

        filename = Path(path).name
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name=sheet_name)
            ws = writer.sheets[sheet_name]
            ws.freeze_panes = "B2"
            for col_cells in ws.columns:
                length = max(
                    len(str(cell.value)) if cell.value else 0
                    for cell in col_cells
                )
                ws.column_dimensions[col_cells[0].column_letter].width = min(length + 4, 60)
        buf.seek(0)

        uri = gcs_cache.write_bytes(
            "forecast_exports",
            filename,
            buf.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        print(f"Saved to GCS: {uri}")
    except Exception as e:
        print(f"[ERROR] Failed to save Excel to GCS: {e}")
    except Exception as e:
        print(f"Export failed: {e}")


# ─────────────────────────────────────────────
# Merge all steps into a single flat table
# ─────────────────────────────────────────────

def merge_to_forecast_s3(
    step1_df: pd.DataFrame,
    step2_df: pd.DataFrame,
    step3_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Merges Step 1, Step 2, and Step 3 outputs into a single flat table.

    Grain: one row per patent (from Step 2).
    Step 1 columns are joined by Drug Name (drug-level, repeated per patent).
    Step 3 columns are joined by Drug Name + Category (category-level, repeated per patent).

    Final columns (snake_case for BQ):
        drug_name, innovator, jurisdiction, layer, layer_reason, parent_patent,
        category, patent_number, filing_date, phase_at_filing,
        duration_from_parent, approval_date, description, insights,
        api_patent, formulation, device, method, dosing, combination,
        typical_filing_stage, filing_stage_reason
    """
    if step2_df.empty:
        print("[MERGE] Step 2 data is empty — nothing to merge.")
        return pd.DataFrame()

    result = step2_df.copy()

    # ── Join Step 1 (drug-level) ──────────────────────────────────────────
    if not step1_df.empty:
        step1_cols = ["Drug Name", "Innovator", "API Patent", "Formulation",
                      "Device", "Method", "Dosing", "Combination"]
        step1_join = step1_df[[c for c in step1_cols if c in step1_df.columns]].copy()
        # De-dup in case Step 1 has multiple rows per drug (shouldn't, but safety)
        step1_join = step1_join.drop_duplicates(subset=["Drug Name"], keep="first")
        result = result.merge(step1_join, on="Drug Name", how="left")
        print(f"[MERGE] Joined Step 1: +{len(step1_join.columns) - 1} columns")
    else:
        # Add empty Step 1 columns
        for col in ["Innovator", "API Patent", "Formulation", "Device",
                     "Method", "Dosing", "Combination"]:
            result[col] = "—"
        print("[MERGE] Step 1 data empty — added blank columns")

    # ── Join Step 3 (drug+category level) ─────────────────────────────────
    if not step3_df.empty:
        step3_join = step3_df.rename(columns={
            "Patent Type":          "Category",
            "Typical Filing Stage": "Typical Filing Stage",
            "Reason":               "Filing Stage Reason",
        })
        step3_join = step3_join[["Drug Name", "Category", "Typical Filing Stage",
                                  "Filing Stage Reason"]].copy()
        step3_join = step3_join.drop_duplicates(subset=["Drug Name", "Category"], keep="first")
        result = result.merge(step3_join, on=["Drug Name", "Category"], how="left")
        print(f"[MERGE] Joined Step 3: +2 columns")
    else:
        result["Typical Filing Stage"] = "—"
        result["Filing Stage Reason"]  = "—"
        print("[MERGE] Step 3 data empty — added blank columns")

    # ── Reorder columns ───────────────────────────────────────────────────
    _FINAL_COLUMN_ORDER = [
        "Drug Name", "Innovator", "Jurisdiction", "Layer", "Layer Reason",
        "Parent Patent", "Category", "Patent Number", "Filing Date",
        "Phase at Filing", "Duration from Parent", "Approval Date",
        "Description", "Insights",
        "API Patent", "Formulation", "Device", "Method", "Dosing", "Combination",
        "Typical Filing Stage", "Filing Stage Reason",
    ]
    # Only keep columns that exist
    ordered = [c for c in _FINAL_COLUMN_ORDER if c in result.columns]
    # Append any extra columns not in the order list
    extras = [c for c in result.columns if c not in ordered]
    result = result[ordered + extras]

    # ── Snake_case all column names for BQ ────────────────────────────────
    result.columns = [
        c.strip().lower().replace(" ", "_").replace("-", "_")
        for c in result.columns
    ]

    # Fill NaN with "—"
    result = result.fillna("—")

    print(f"[MERGE] Final table: {len(result)} rows × {len(result.columns)} columns")
    print(f"[MERGE] Columns: {list(result.columns)}")
    return result


def upload_to_bigquery(df: pd.DataFrame, table_id: str = _FORECAST_S3_BQ) -> None:
    """Uploads the merged forecast_s3 DataFrame to BigQuery, appending to the table."""
    if df.empty:
        print("[BQ UPLOAD] DataFrame is empty — skipping upload.")
        return

    try:
        job_config = bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            autodetect=True,
        )
        job = _BQ_CLIENT.load_table_from_dataframe(df, table_id, job_config=job_config)
        job.result()  # Wait for completion
        print(f"[BQ UPLOAD] Uploaded {len(df)} rows to: {table_id}")
    except Exception as e:
        print(f"[BQ UPLOAD] Failed: {e}")
