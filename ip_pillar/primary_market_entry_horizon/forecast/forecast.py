"""
forecast.py
───────────
Orchestrates the IP "Forecast" dimension for one drug: how many more patents of each type the innovator is
likely to file in each jurisdiction, scored like the real patents. Input is glp_patent_loe; the output,
forecasted_loe, is merged into glp_master_loe by the next primary_market_entry_horizon run.

    forecast(drug_name)   — async; runs every step below end to end

    1. Load the drug's glp_patent_loe rows (its blocking-analysis patents)         (bq_utils.load_patent_loe)
       and resolve the innovator company                                           (forecast_helpers.resolve_company)
    2. Three analyses run together:
         patent_layering    IP landscape, patent layering with AlloyDB patent text, and the clinical phase at
                            filing and typical filing stage per patent type -> forecast_s3
         patern_analysis    the innovator's filing pattern across its wider portfolio -> filing_pattern_table
         strategy_analysis  the company's business strategy / incentive to expand protection
                            -> company_analysis_table (company level: reused from BigQuery unless reset)
    3. Forecast the future filings per patent type and jurisdiction with Gemini from those analyses, and
       score them (controlling expiry = lower filing year + 20, years to entry, 1-5 score)   (forecast_pipeline)
    4. forecast_s3, filing_pattern_table, company_analysis_table and forecasted_loe -> BigQuery;
       each run replaces only that drug's (or company's) rows                       (bq_utils)
    5. PDF report built from the run's results, uploaded to GCS on every run   (generate_report)

Input is glp_patent_loe, so run primary_market_entry_horizon for the drug first.

Usage:
    from ip_pillar.primary_market_entry_horizon.forecast import forecast

    result = await forecast("semaglutide")
"""

import asyncio
import logging
import time

from ... import config
from ...bq_utils import save_gemini_stats
from ...drug_list import resolve_drug_name
from ...gemini_utils import reset_gemini_call_stats
from .bq_utils import load_patent_loe, write_forecast_s3, write_forecasted_loe
from .forecast_helpers import (
    analyse_company,
    company_text,
    filing_pattern,
    generate_report,
    innovator_text,
    layering,
    resolve_company,
)
from .forecast_pipeline import _search_company_analysis, _search_innovator_patterns, forecast_and_score_drug

logger = logging.getLogger(__name__)


async def forecast(drug_name: str, reset: bool = False) -> dict:
    """
    Forecast pipeline for ONE drug, plus a Gemini usage row per model in
    config.GEMINI_STATS_TABLE (written even when the run fails).

    Args:
        drug_name: Drug name; resolved to the drug list's spelling and used for every output.
        reset:     Look up the innovator's filing pattern and the company's business strategy again
                   instead of reusing the rows an earlier run stored.

    Returns:
        {"drug_name", "company", "patents", "forecast_s3_rows", "innovators", "forecasted_patents",
         "processing_time_seconds", "output_tables": [...], "report": {"gcs_uri", "archive_uri"} or {"error"}}
        or {"drug_name", "error", ...} if the drug has no glp_patent_loe rows or the forecast fails.
    """
    reset_gemini_call_stats()
    t0 = time.time()
    drug = resolve_drug_name(drug_name)
    run_status, error = "success", None
    try:
        logger.info("%s\n[FORECAST] Starting for: %s\n%s", "=" * 60, drug, "=" * 60)

        # Step 1: glp_patent_loe -> the drug's patents, and its company
        loe_df = await asyncio.to_thread(load_patent_loe, drug)
        if loe_df.empty:
            run_status = "failed"
            error = f"No rows for '{drug}' in {config.BQ_PATENT_LOE_TABLE} — run primary_market_entry_horizon first."
            return {"drug_name": drug, "error": error}
        company = await resolve_company(drug)
        logger.info("[FORECAST] %s | %d patent(s) | company: %s", drug, len(loe_df), company or "unknown")

        # Step 2: layering, filing pattern and business strategy don't depend on one another
        forecast_s3, filing_rows, company_row = await asyncio.gather(
            layering(drug, loe_df), filing_pattern(drug, reset), analyse_company(company, drug, reset))
        await asyncio.to_thread(write_forecast_s3, drug, forecast_s3)

        # Step 3: forecast + score. Sources an analysis could not produce are searched for on the web.
        innovator_patterns = innovator_text(drug, filing_rows) or await _search_innovator_patterns(drug, company)
        company_analysis = company_text(company, company_row) or await _search_company_analysis(company, drug)
        scored, forecast_error = await forecast_and_score_drug(
            drug, company, forecast_s3 if not forecast_s3.empty else None, innovator_patterns, company_analysis)
        if forecast_error:
            run_status = "failed"
            error = f"Forecast failed: {forecast_error}"
            return {"drug_name": drug, "company": company, "error": error,
                    "processing_time_seconds": round(time.time() - t0, 1)}

        # Step 4: BigQuery (forecast_s3, filing_pattern_table and company_analysis_table were written above)
        await asyncio.to_thread(write_forecasted_loe, drug, scored)

        # Step 5: the dimension's PDF report, from this run's results. A failed report is recorded in the
        # result but does not fail the run.
        report = await asyncio.to_thread(generate_report, drug, scored, loe_df)

        elapsed = time.time() - t0
        logger.info("[FORECAST] Done in %.1fs - %d forecasted entr(ies)", elapsed, len(scored))
        return {
            "drug_name":               drug,
            "company":                 company,
            "patents":                 len(loe_df),
            "forecast_s3_rows":        len(forecast_s3),
            "innovators":              len(filing_rows),
            "forecasted_patents":      len(scored),
            "processing_time_seconds": round(elapsed, 1),
            "output_tables":           [config.BQ_FORECAST_S3_TABLE, config.BQ_FILING_PATTERN_TABLE,
                                        config.BQ_COMPANY_ANALYSIS_TABLE, config.FORECASTED_LOE_TABLE],
            "report":                  report,
        }
    except Exception as e:
        run_status, error = "failed", str(e)
        raise
    finally:
        save_gemini_stats(drug, config.DIMENSION_FORECAST, run_status, error, time.time() - t0)
