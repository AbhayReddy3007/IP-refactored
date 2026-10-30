"""
primary_market_entry_horizon.py
────────────────────────────────
Orchestrates the whole IP Dimension 1 process for one drug:

    primary_market_entry_horizon(drug_name)   — async; runs every step below end to end

    1. List approved patent PDFs for the drug          (patent_filter)
    2. Fetch, chunk, embed, store them in AlloyDB       (chunking.indexer)
    3. Fetch the drug's clinical development timeline   (score_calculation.phase_fetcher)
    4. Run the 5-step blocking analysis                  (blocking_analysis)
    5. Assign phase_at_filing to each patent             (score_calculation.phase_fetcher)
    6. First-pass score calculation                      (score_calculation.calculate_scores)
    7. Real-world approval dates (Marketed only)         (score_calculation.approval_date_fetcher)
    8. Second-pass score calculation with real dates     (score_calculation.calculate_scores)
    9. glp_patent_loe: patents + scores -> BigQuery + JSON in GCS   (score_calculation.merge_forecasted_loe, bq_utils)
   10. forecast_analysis: reads glp_patent_loe, writes forecasted_loe (skipped when config.RUN_FORECAST is
       false or the drug is Marketed / Pre-registration; a failure is recorded in the result but does not fail the run)   (forecast)
   11. glp_master_loe: merged with the fresh forecasted_loe, re-scored -> BigQuery + JSON in GCS
   12. PDF report built from the glp_master_loe rows, uploaded to GCS  (generate_report_and_rationale.generate_report)
   13. Blocking Analysis PDF report, written from the analysed patents and uploaded next to it
                                                       (blocking_analysis.generate_report)

Usage:
    from IP_refactored.primary_market_entry_horizon import primary_market_entry_horizon

    result = await primary_market_entry_horizon("semaglutide")
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .. import config
from ..blocking_analysis import run_blocking_analysis
from ..drug_list import resolve_drug_name
from ..bq_utils import save_gemini_stats
from ..chunking.indexer import get_or_create_collection, indexer as run_indexer
from ..gemini_utils import isolated_gemini_call_stats, reset_gemini_call_stats
from ..patent_filter import patent_filter
from .bq_utils import fetch_drug_details_df, load_forecasted_loe_table, publish_table
from .score_calculation import (
    assign_patent_phases,
    calculate_scores,
    fetch_approval_dates,
    fetch_clinical_timeline,
    patents_to_dataframe,
    run_merge,
)
from .score_calculation.merge_forecasted_loe import is_marketed_or_prereg
from .score_calculation.phase_fetcher import canonicalise_drug_name

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _marketed_in(patents: List[Dict], jurisdiction: str) -> bool:
    return any(
        p.get("phase_at_filing") == "Marketed" and (p.get("jurisdiction") or "").upper() == jurisdiction
        for p in patents
    )


def _split_csv(value) -> List[str]:
    return [v.strip() for v in str(value or "").split(",") if v.strip()]


def publish_loe_tables(patents: List[Dict], drug_name: str, run_ts: str, only: Optional[List[str]] = None) -> Dict[str, Dict]:
    """Steps 9 and 11: build glp_patent_loe and glp_master_loe for this drug (or just the tables in
    *only*) and publish each to BigQuery + a JSON snapshot. A failure in one table does not stop the other."""
    builders = {
        config.BQ_PATENT_LOE_TABLE: lambda: patents_to_dataframe(patents, drug_name),
        config.BQ_MASTER_LOE_TABLE: lambda: run_merge(patents, drug_name),
    }
    builders = {table_id: build for table_id, build in builders.items() if only is None or table_id in only}
    # When this drug's scores were calculated: ISO 8601, microseconds, UTC.
    # One value for the drug, shared by both tables.
    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    outputs: Dict[str, Dict] = {}
    for table_id, build in builders.items():
        logger.info("[HORIZON] Building %s ...", table_id)
        try:
            table = build()
            table["updated_at"] = updated_at
            outputs[table_id] = publish_table(table, table_id, drug_name, run_ts)
        except Exception:
            logger.exception("[HORIZON] Could not build %s for %r", table_id, drug_name)
            outputs[table_id] = {"json_uri": None, "bq_table": None}
    return outputs


async def _run_forecast(drug_name: str, reset: bool) -> Dict:
    """Step 10: forecast_analysis for the drug, then drop the cached forecasted_loe so the glp_master_loe
    merge sees the rows it just wrote. {"error": ...} if it fails, {"skipped": reason} if it is not run:
    switched off, or the drug is Marketed / Pre-registration (the merge drops its forecasted rows)."""
    if not config.RUN_FORECAST:
        logger.info("[HORIZON] RUN_FORECAST is off - skipping forecast_analysis")
        return {"skipped": "RUN_FORECAST is off"}
    if not config.NO_MARKETED_SKIP:
        details = await asyncio.to_thread(fetch_drug_details_df, canonicalise_drug_name(drug_name))
        if is_marketed_or_prereg(details):
            logger.info("[HORIZON] %r is Marketed / Pre-registration - skipping forecast_analysis", drug_name)
            return {"skipped": "Marketed / Pre-registration"}
    # Imported here: the forecast package itself uses this package's approval-date fetcher.
    from ..forecast import forecast_analysis

    logger.info("[HORIZON] Running forecast_analysis ...")
    try:
        # forecast_analysis resets and saves the Gemini counters itself; keep this dimension's.
        with isolated_gemini_call_stats():
            result = await forecast_analysis(drug_name, reset=reset)
    except Exception as e:
        logger.exception("[HORIZON] forecast_analysis failed for %r", drug_name)
        return {"error": str(e)}
    load_forecasted_loe_table.cache_clear()
    if result.get("error"):
        logger.error("[HORIZON] forecast_analysis: %s", result["error"])
    return result


def _generate_report(drug_name: str) -> Dict:
    """Build and upload the dimension's report; {"error": ...} if it fails."""
    # Imported here so reportlab is only needed when a report is built.
    from .generate_report_and_rationale.generate_report import report_for_drug

    logger.info("[HORIZON] Generating the %s report ...", config.DIMENSION_PRIMARY_MARKET_ENTRY_HORIZON)
    try:
        report = report_for_drug(drug_name)
    except Exception as e:
        logger.exception("[HORIZON] Report failed for %r", drug_name)
        return {"error": str(e), "gcs_uri": None, "archive_uri": None}
    logger.info("[HORIZON] Report -> %s", report["gcs_uri"])
    return report


def _generate_blocking_report(drug_name: str, patents: List[Dict]) -> Dict:
    """Build and upload the Blocking Analysis report; {"error": ...} if it fails."""
    # Imported here so reportlab is only needed when a report is built.
    from ..blocking_analysis.generate_report import report_for_drug

    logger.info("[HORIZON] Generating the %s report ...", config.DIMENSION_BLOCKING_ANALYSIS)
    try:
        report = report_for_drug(drug_name, patents)
    except Exception as e:
        logger.exception("[HORIZON] Blocking Analysis report failed for %r", drug_name)
        return {"error": str(e), "gcs_uri": None, "archive_uri": None}
    logger.info("[HORIZON] Blocking Analysis report -> %s", report["gcs_uri"])
    return report


async def primary_market_entry_horizon(drug_name: str, reindex: bool = False) -> dict:
    """
    Full primary-market-entry-horizon pipeline for ONE drug, plus a Gemini usage
    row per model in config.GEMINI_STATS_TABLE (written even when the run fails).
    See _run_horizon() for the arguments and return value.
    """
    reset_gemini_call_stats()
    t0 = time.time()
    run_status, error = "success", None
    try:
        result = await _run_horizon(drug_name, reindex)
        if result.get("error"):
            run_status, error = "failed", result["error"]
        return result
    except Exception as e:
        run_status, error = "failed", str(e)
        raise
    finally:
        save_gemini_stats(resolve_drug_name(drug_name), config.DIMENSION_PRIMARY_MARKET_ENTRY_HORIZON,
                          run_status, error, time.time() - t0)


async def _run_horizon(drug_name: str, reindex: bool = False) -> dict:
    """
    Full primary-market-entry-horizon pipeline for ONE drug.

    Args:
        drug_name: Drug name (must match a GCS patent folder, fuzzy-matched). It is
                   resolved to the drug list's spelling and used for every output.
        reindex:   Force re-indexing and re-analysis, ignoring caches.

    Returns:
        {"drug_name", "analysis_date", "patents", "source_files",
         "processing_time_seconds", "phase_data_source", "loe_tables": {table: {"bq_table", "json_uri"}}}
        or {"drug_name", "error", ...} if the drug has no approved PDFs.
    """
    t0 = time.time()
    drug_name = resolve_drug_name(drug_name)
    now = datetime.now()
    analysis_date, run_ts = now.strftime("%Y-%m-%d"), now.strftime("%Y%m%d_%H%M%S")
    logger.info("%s\n[HORIZON] Starting for: %s\n%s", "=" * 60, drug_name, "=" * 60)

    # Step 1: list approved PDFs
    pdf_refs = patent_filter(drug_name)
    if not pdf_refs:
        return {
            "drug_name":     drug_name,
            "error":         f"No approved PDFs found for '{drug_name}' in gs://{config.GCS_BUCKET}/{config.GCS_PATENTS_PREFIX}.",
            "patents":       [],
            "source_files":  [],
            "analysis_date": analysis_date,
        }

    # Step 2: fetch + chunk + embed + store in AlloyDB
    logger.info("[HORIZON] Indexing %d patent(s)...", len(pdf_refs))
    await run_indexer(drug_name, reindex=reindex)
    collection = get_or_create_collection(drug_name)

    # Step 3: clinical timeline (drives blocking-analysis phase routing)
    timeline = await fetch_clinical_timeline(drug_name)
    geography_stages = timeline.get("geography_stages", {})
    drug_phase = {"US": geography_stages.get("United States"), "EP": geography_stages.get("EU")}
    logger.info("[HORIZON] Drug phase -> US: %s | EP: %s", drug_phase["US"], drug_phase["EP"])

    # Step 4: blocking analysis (Steps 1-5)
    patents = await run_blocking_analysis(
        drug_name, pdf_refs, collection, drug_phase=drug_phase, force_reanalyse=reindex,
    )
    logger.info("[HORIZON] %d patent(s) analysed", len(patents))

    # Steps 5-6: phase_at_filing + first-pass scores
    patents = calculate_scores(assign_patent_phases(patents, timeline))

    # Step 7: real-world approval dates (Marketed jurisdictions only)
    us_marketed, eu_marketed = _marketed_in(patents, "US"), _marketed_in(patents, "EP")
    logger.info("[HORIZON] US Marketed: %s | EU Marketed: %s", us_marketed, eu_marketed)
    approval = await fetch_approval_dates(
        drug_name=drug_name,
        bq_companies=_split_csv(timeline.get("company_name")),
        bq_brands=_split_csv(timeline.get("brand_name")),
        fetch_us=us_marketed, fetch_eu=eu_marketed,
    )
    for p in patents:
        p["approval_date_us"]        = approval["US"]["date"]
        p["approval_date_eu"]        = approval["EU"]["date"]
        p["approval_date_us_source"] = approval["US"]["source"]
        p["approval_date_eu_source"] = approval["EU"]["source"]

    # Step 8: recalculate with real approval dates
    patents = calculate_scores(patents)

    # Step 9: glp_patent_loe -> BigQuery + JSON
    loe_tables = publish_loe_tables(patents, drug_name, run_ts, only=[config.BQ_PATENT_LOE_TABLE])

    # Step 10: forecast_analysis reads glp_patent_loe and writes forecasted_loe. Its failure does not fail the run
    # (glp_master_loe is then merged with whatever forecasted_loe already holds for the drug).
    forecast = await _run_forecast(drug_name, reindex)

    # Step 11: glp_master_loe, merged with the fresh forecasted_loe -> BigQuery + JSON
    loe_tables.update(publish_loe_tables(patents, drug_name, run_ts, only=[config.BQ_MASTER_LOE_TABLE]))

    # Step 12: the dimension's PDF report (built from the glp_master_loe rows just written).
    # A failed report is recorded in the result but does not fail the run.
    report = _generate_report(drug_name)

    # Step 13: the Blocking Analysis report, from the analysed patents. Same handling as step 11.
    blocking_report = _generate_blocking_report(drug_name, patents)

    elapsed = time.time() - t0
    logger.info("[HORIZON] Done in %.1fs - %d patent(s)", elapsed, len(patents))
    for table_id, out in loe_tables.items():
        logger.info("[HORIZON] %s -> BigQuery: %s | JSON: %s", table_id, out["bq_table"], out["json_uri"])

    return {
        "drug_name":               drug_name,
        "analysis_date":           analysis_date,
        "patents":                 patents,
        "source_files":            [ref["filename"] for ref in pdf_refs],
        "processing_time_seconds": round(elapsed, 1),
        "phase_data_source":       timeline.get("source", "unavailable"),
        "loe_tables":              loe_tables,
        "forecast":                forecast,
        "report":                  report,
        "blocking_report":         blocking_report,
    }
