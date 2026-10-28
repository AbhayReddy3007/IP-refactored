"""
bq_utils.py
BigQuery I/O for the patent strength dimension:
    load_master_loe(drug)            the drug's glp_master_loe rows       (config.BQ_MASTER_LOE_TABLE)
    load_existing_score_cache(drug)  fallback score cache                 (config.BQ_PATENT_STRENGTH_TABLE)
    build_output_bigquery(...)       patent_strength_table + country score table;
                                     each run replaces only that drug's rows

All BigQuery access goes through IP_refactored.gcp_helpers / gcp_utils; table IDs come from IP_refactored.config.
"""

import logging
from datetime import datetime, timezone

import pandas as pd
from google.cloud import bigquery

from ... import config, gcp_helpers
from ...primary_market_entry_horizon.bq_utils import load_master_table
from ...utils import bq_safe_name
from .checkpoint import checkpoint_key
from ..scoring.constants import ACTIVE_SFS, JURISDICTION_LABELS, SF_SCORE_LABELS
from ..scoring.scoring import compute_weighted_score, get_country_weight, infer_jurisdiction, summary_fields

logger = logging.getLogger(__name__)


# ── Schemas ───────────────────────────────────────────────────────────────────

def _sf_schema_fields(prefix: str) -> list[bigquery.SchemaField]:
    return [
        bigquery.SchemaField(f"{prefix}_score", "INTEGER"),
        bigquery.SchemaField(f"{prefix}_label", "STRING"),
        bigquery.SchemaField(f"{prefix}_score_reason", "STRING"),
        bigquery.SchemaField(f"{prefix}_reasoning", "STRING"),
        bigquery.SchemaField(f"{prefix}_key_finding_1", "STRING"),
        bigquery.SchemaField(f"{prefix}_key_finding_2", "STRING"),
        bigquery.SchemaField(f"{prefix}_key_finding_3", "STRING"),
        bigquery.SchemaField(f"{prefix}_chunks_used", "INTEGER"),
    ]


PATENT_STRENGTH_SCHEMA = [
    bigquery.SchemaField("drug_name", "STRING"),
    bigquery.SchemaField("patent_number", "STRING"),
    bigquery.SchemaField("patent_type", "STRING"),
    bigquery.SchemaField("jurisdiction", "STRING"),
    bigquery.SchemaField("country_weight", "FLOAT64"),
    bigquery.SchemaField("core_inventive_step", "STRING"),
    *[field for sf in ACTIVE_SFS for field in _sf_schema_fields(sf)],
    bigquery.SchemaField("weighted_score", "FLOAT64"),
    bigquery.SchemaField("key_vulnerabilities", "STRING"),
    bigquery.SchemaField("key_strengths", "STRING"),
    bigquery.SchemaField("filing_date", "STRING"),
    bigquery.SchemaField("grant_date", "STRING"),
    bigquery.SchemaField("created_at", "TIMESTAMP"),
    bigquery.SchemaField("updated_at", "STRING"),
]

COUNTRY_SCORE_SCHEMA = [
    bigquery.SchemaField("drug_name", "STRING"),
    bigquery.SchemaField("jurisdiction", "STRING"),
    bigquery.SchemaField("country_name", "STRING"),
    bigquery.SchemaField("country_weight", "FLOAT64"),
    bigquery.SchemaField("patent_count", "INTEGER"),
    bigquery.SchemaField("avg_weighted_score", "FLOAT64"),
    bigquery.SchemaField("country_weighted_score", "FLOAT64"),
    bigquery.SchemaField("final_patent_score", "FLOAT64"),
    bigquery.SchemaField("avg_sf_score", "FLOAT64"),
    bigquery.SchemaField("rationale", "STRING"),
    bigquery.SchemaField("updated_at", "STRING"),
]


# ── Writers ───────────────────────────────────────────────────────────────────

def _replace_drug_rows(rows: list[dict], table_id: str, schema: list, drug: str) -> None:
    """Delete *drug*'s rows from the table, then append *rows* (re-runs replace, not duplicate)."""
    if not rows:
        logger.info("No rows to write to %s.", table_id)
        return
    deleted = gcp_helpers.delete_rows(
        table_id,
        "REGEXP_REPLACE(LOWER(drug_name), r'\\s+', '') = REGEXP_REPLACE(LOWER(@drug_name), r'\\s+', '')",
        {"drug_name": drug},
    )
    gcp_helpers.load_dataframe(pd.DataFrame(rows).drop_duplicates(), table_id, schema)
    logger.info("%s: replaced %d old row(s) with %d row(s) for %r", table_id, deleted, len(rows), drug)


def _sf_fields(result: dict, sf_key: str) -> dict:
    """Flatten one sub-factor result into BigQuery column values."""
    sf = result.get(sf_key, {})
    score = sf.get("score")
    findings = sf.get("key_findings", [])
    return {
        f"{sf_key}_score": score,
        f"{sf_key}_label": SF_SCORE_LABELS[sf_key].get(score, "") if score else "",
        f"{sf_key}_score_reason": sf.get("score_reason", ""),
        f"{sf_key}_reasoning": sf.get("reasoning", ""),
        f"{sf_key}_key_finding_1": findings[0] if len(findings) > 0 else "",
        f"{sf_key}_key_finding_2": findings[1] if len(findings) > 1 else "",
        f"{sf_key}_key_finding_3": findings[2] if len(findings) > 2 else "",
        f"{sf_key}_chunks_used": sf.get("chunks_used", 0),
    }


def _build_strength_rows(df_shortlist: pd.DataFrame, results: list[dict], drug: str, updated_at: str) -> list[dict]:
    created_at = datetime.now(tz=timezone.utc)
    rows = []
    for i, (_, row_data) in enumerate(df_shortlist.iterrows()):
        result = results[i]
        patent_number = str(row_data.get("Patent Number", "")).strip()
        jurisdiction = infer_jurisdiction(patent_number)

        row_out = {
            "drug_name": drug,
            "patent_number": patent_number,
            "patent_type": str(row_data.get("Step 1 Claim Category", "")).strip(),
            "jurisdiction": jurisdiction,
            "country_weight": get_country_weight(jurisdiction),
            "weighted_score": compute_weighted_score(result),
            # Built from the sub-factor results (no separate summary call is made).
            **summary_fields(result),
            "filing_date": str(row_data.get("Filing Date", "")),
            "grant_date": str(row_data.get("Grant Date", "")),
            "created_at": created_at,
            "updated_at": updated_at,
        }
        for sf_key in ACTIVE_SFS:
            row_out.update(_sf_fields(result, sf_key))
        rows.append(row_out)
    return rows


def _build_country_rows(country_scores: dict, drug: str, updated_at: str, rationale: str) -> list[dict]:
    rows = []
    for _, drug_entry in country_scores["drug_details"].items():
        final_score = drug_entry["final_patent_score"]
        for jurisdiction, jdata in sorted(
            drug_entry["jurisdictions"].items(),
            key=lambda item: (-item[1]["country_weight"], item[0]),
        ):
            rows.append({
                "drug_name": drug,
                "jurisdiction": jurisdiction,
                "country_name": JURISDICTION_LABELS.get(jurisdiction, jurisdiction),
                "country_weight": jdata["country_weight"],
                "patent_count": jdata["patent_count"],
                "avg_weighted_score": jdata["avg_weighted_score"],
                "country_weighted_score": round(jdata["country_weighted_score"], 4),
                "final_patent_score": round(final_score, 4),
                "avg_sf_score": drug_entry["avg_sf_score"],  # drug-level, repeated on each of its rows
                "rationale": rationale,  # the drug's summary, repeated on each of its rows
                "updated_at": updated_at,
            })
    return rows


def build_output_bigquery(df_shortlist: pd.DataFrame, results: list[dict], country_scores: dict, drug: str,
                          rationale: str = "") -> None:
    """Write patent_strength_table and patent_strength_country_score_table for *drug*.
    ``updated_at`` is when the scores were calculated: ISO 8601, microseconds, UTC."""
    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    logger.info("Writing %s ...", config.BQ_PATENT_STRENGTH_TABLE)
    _replace_drug_rows(
        _build_strength_rows(df_shortlist, results, drug, updated_at),
        config.BQ_PATENT_STRENGTH_TABLE, PATENT_STRENGTH_SCHEMA, drug,
    )
    logger.info("Writing %s ...", config.BQ_COUNTRY_SCORE_TABLE)
    _replace_drug_rows(
        _build_country_rows(country_scores, drug, updated_at, rationale),
        config.BQ_COUNTRY_SCORE_TABLE, COUNTRY_SCORE_SCHEMA, drug,
    )


# ── Readers ───────────────────────────────────────────────────────────────────

def load_master_loe(drug: str) -> pd.DataFrame:
    """The drug's rows from glp_master_loe, with the friendly column names the
    pipeline uses ('Drug Name', 'Patent Number', 'Source File', ...)."""
    df = load_master_table([drug])
    return df.rename(columns={bq_safe_name("Source File"): "Source File"})


def load_existing_score_cache(drug: str) -> dict:
    """
    Fallback cache: rebuild already-scored patents of *drug* from the latest row
    per patent_number in patent_strength_table (by created_at), used when the GCS
    checkpoint is missing or incomplete.

    Returns ``{checkpoint_key(drug, patent_number): {sf1: {...}, sf2: {...}, ...}}``.
    """
    cache: dict = {}
    try:
        query = f"""
        SELECT * EXCEPT(rn) FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY patent_number
                ORDER BY created_at DESC NULLS LAST
            ) AS rn
            FROM `{config.BQ_PATENT_STRENGTH_TABLE}`
            WHERE REGEXP_REPLACE(LOWER(drug_name), r'\\s+', '') = REGEXP_REPLACE(LOWER(@drug_name), r'\\s+', '')
        ) WHERE rn = 1
        """
        df = gcp_helpers.run_query(query, {"drug_name": drug})
    except Exception as exc:
        logger.warning("[BQ FALLBACK] Could not read %s (%s) - skipping", config.BQ_PATENT_STRENGTH_TABLE, exc)
        return cache

    if df.empty:
        return cache

    for _, row in df.iterrows():
        patent_number = str(row.get("patent_number", "")).strip()
        if not patent_number:
            continue

        entry = {}
        for sf_key in ACTIVE_SFS:
            score_val = row.get(f"{sf_key}_score")
            if pd.isna(score_val):
                # Sub-factor not completed — leave it out so the normal
                # scoring loop still fills it in.
                continue
            findings = [row.get(f"{sf_key}_key_finding_{n}", "") for n in (1, 2, 3)]
            entry[sf_key] = {
                "score": int(score_val),
                "score_reason": row.get(f"{sf_key}_score_reason", "") or "",
                "reasoning": row.get(f"{sf_key}_reasoning", "") or "",
                "key_findings": [f for f in findings if f],
                "chunks_used": int(row.get(f"{sf_key}_chunks_used", 0) or 0),
            }
        if entry:
            cache[checkpoint_key(drug, patent_number)] = entry

    logger.info(
        "[BQ FALLBACK] Reconstructed %d patent score(s) for '%s' from %s "
        "(used only to fill gaps in the GCS checkpoint)",
        len(cache), drug, config.BQ_PATENT_STRENGTH_TABLE,
    )
    return cache
