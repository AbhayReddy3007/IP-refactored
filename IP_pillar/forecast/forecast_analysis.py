"""
forecast_analysis.py
────────────────────
Orchestrates the IP "Forecast Analysis" for one drug: how many more patents of each type the innovator is
likely to file in each jurisdiction, scored like the real patents. Input is glp_patent_loe; the output,
forecasted_loe, is merged into glp_master_loe by the next primary_market_entry_horizon run.

    forecast_analysis(drug_name)   — async; runs every step below end to end

    1. Load the drug's glp_patent_loe rows (its blocking-analysis patents)         (bq_utils.load_patent_loe)
       and resolve the innovator company
    2. Steps 3-5 run together:
         step3  IP landscape, patent layering with AlloyDB patent text, and the clinical phase at filing
                and typical filing stage per patent type -> forecast_s3             (step3)
         step4  the innovator's filing pattern across its wider portfolio -> filing_pattern_table      (step4)
         step5  the company's business strategy / incentive to expand protection -> company_analysis_table
                (company level: reused from BigQuery unless reset)                  (step5)
    3. Forecast the future filings per patent type and jurisdiction with Gemini from steps 3-5, and
       score them (controlling expiry = lower filing year + 20, years to entry, 1-5 score)         (step6)
    4. forecast_s3, filing_pattern_table, company_analysis_table and forecasted_loe -> BigQuery;
       each run replaces only that drug's (or company's) rows                       (bq_utils)
    5. PDF report built from the run's results, uploaded to GCS on every run   (generate_report)

Input is glp_patent_loe, so run primary_market_entry_horizon for the drug first.

Usage:
    from IP_pillar.forecast import forecast_analysis

    result = await forecast_analysis("semaglutide")
"""

import asyncio
import logging
import time

import pandas as pd
from typing import Dict, List, Optional

from .. import config
from ..bq_utils import save_gemini_stats
from ..drug_list import resolve_drug_name
from ..gemini_utils import reset_gemini_call_stats
from .bq_utils import (
    load_company_analysis, load_filing_pattern, load_patent_loe, write_company_analysis,
    write_filing_pattern, write_forecast_s3, write_forecasted_loe,
)
from .step3 import merge_to_forecast_s3, step1_ip_landscape, step2_patent_layering, step3_filing_analysis
from .step4 import analyze_innovator_filing_patterns, filing_pattern_rows
from .step5 import _BQ_SCHEMA as COMPANY_ANALYSIS_SCHEMA
from .step5 import _assessment_to_bq_row, review_business_strategy
from .step6 import (
    _load_drug_company_map, _search_company_analysis, _search_company_for_drug, _search_innovator_patterns,
    forecast_and_score_drug,
)

logger = logging.getLogger(__name__)

THERAPY_AREA = "GLP-1"

# company_analysis_table sections: (column prefix, heading)
_COMPANY_SECTIONS = (
    ("rev", "Revenue dependency"), ("pipe", "Pipeline depth"), ("strat", "Strategic priority"),
    ("ind", "Indication expansion"), ("lc", "Lifecycle management"), ("comp", "Competitive pressure"),
)


# ─────────────────────────────────────────────
# Text the forecast prompt reads
# ─────────────────────────────────────────────

def _generate_report(drug: str, scored: List[Dict], patents: pd.DataFrame) -> dict:
    """Build and upload the dimension's report; {"error": ...} if it fails."""
    # Imported here so reportlab is only needed when a report is built.
    from .generate_report.generate_report import report_for_drug

    logger.info("[FORECAST] Generating the %s report ...", config.DIMENSION_FORECAST)
    try:
        report = report_for_drug(drug, scored, patents)
    except Exception as e:
        logger.exception("[FORECAST] Report failed for %r", drug)
        return {"error": str(e), "gcs_uri": None, "archive_uri": None}
    logger.info("[FORECAST] Report -> %s", report["gcs_uri"])
    return report


def _innovator_text(drug: str, rows: List[Dict]) -> str:
    """The step 4 rows as the 'innovator filing behaviour' source of the forecast prompt."""
    if not rows:
        return ""
    lines = [f"INNOVATOR FILING PATTERNS FOR {drug}:"]
    for row in rows:
        lines.append(f"  {row.get('company', 'N/A')}: {row.get('characterization', 'N/A')} (confidence: {row.get('confidence', 'N/A')})")
        lines.append(f"    {row.get('rationale', 'N/A')}")
    return "\n".join(lines)


def _company_text(company: str, row: Dict) -> str:
    """The step 5 row as the 'company business strategy' source of the forecast prompt."""
    if not row:
        return ""
    lines = [f"BUSINESS STRATEGY ASSESSMENT FOR {company}:"]
    for prefix, heading in _COMPANY_SECTIONS:
        lines.append(f"  {heading}: {row.get(f'{prefix}_rating') or 'N/A'}")
        details = str(row.get(f"{prefix}_details") or "").strip()
        if details and details.lower() not in ("nan", "n/a"):
            lines.append(f"    {details[:500]}")
    if row.get("pat_rating"):
        lines.append(f"  Patent expansion likelihood: {row['pat_rating']}")
    if row.get("overall_assessment"):
        lines.append(f"  Overall: {str(row['overall_assessment'])[:800]}")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# Steps
# ─────────────────────────────────────────────

async def _resolve_company(drug: str) -> str:
    """The drug's parent company: the drug/company mapping in BigQuery, else a Gemini web search."""
    mapping = await asyncio.to_thread(_load_drug_company_map)
    info = mapping.get(drug.lower())
    if not info:
        norm = drug.lower().replace(" ", "").replace("-", "")
        info = next((v for k, v in mapping.items() if norm in k.replace(" ", "").replace("-", "")), None)
    if info and info.get("company"):
        return info["company"]
    logger.info("[FORECAST] %s: company not in the drug/company mapping - searching the web", drug)
    return (await _search_company_for_drug(drug)) or ""


async def _layering(drug: str, loe_df: pd.DataFrame) -> pd.DataFrame:
    """Step 3: IP landscape + patent layering + filing stage, merged into forecast_s3."""
    landscape = await step1_ip_landscape(drug_filter={drug}, loe_df=loe_df)
    layering = await step2_patent_layering(drug_filter={drug}, loe_df=loe_df)
    filing_stage = await step3_filing_analysis(layering) if not layering.empty else layering
    return merge_to_forecast_s3(landscape, layering, filing_stage)


async def _filing_pattern(drug: str, reset: bool) -> List[Dict]:
    """Step 4: the innovator's filing pattern; the rows of an earlier run are reused unless reset."""
    if not reset:
        cached = await asyncio.to_thread(load_filing_pattern, drug)
        if cached:
            logger.info("[FORECAST] %s: reusing %d filing-pattern row(s) from %s", drug, len(cached), config.BQ_FILING_PATTERN_TABLE)
            return cached
    result = await analyze_innovator_filing_patterns(drug)
    rows = filing_pattern_rows(drug, result) if result else []
    await asyncio.to_thread(write_filing_pattern, drug, rows)
    return rows


async def _company_analysis(company: str, drug: str, reset: bool) -> Dict:
    """Step 5: the company's business strategy (shared by its drugs); an earlier run's row is reused unless reset."""
    if not company:
        return {}
    if not reset:
        cached = await asyncio.to_thread(load_company_analysis, company)
        if cached:
            logger.info("[FORECAST] %s: reusing the %s business-strategy analysis from %s", drug, company, config.BQ_COMPANY_ANALYSIS_TABLE)
            return cached
    assessment = await asyncio.to_thread(review_business_strategy, company, THERAPY_AREA, [drug], False)
    if "error" in assessment:
        logger.warning("[FORECAST] %s: no business-strategy analysis for %s (%s)", drug, company, assessment["error"])
        return {}
    row = _assessment_to_bq_row(assessment)
    try:
        await asyncio.to_thread(write_company_analysis, company, row, COMPANY_ANALYSIS_SCHEMA)
    except Exception:
        # The analysis is still used for this forecast; only its storage failed.
        logger.exception("[FORECAST] %s: could not store the %s analysis in %s", drug, company, config.BQ_COMPANY_ANALYSIS_TABLE)
    return row


# ─────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────

async def forecast_analysis(drug_name: str, reset: bool = False) -> dict:
    """
    Forecast-analysis pipeline for ONE drug, plus a Gemini usage row per model in
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
        result = await _run(drug, reset, t0)
        if result.get("error"):
            run_status, error = "failed", result["error"]
        return result
    except Exception as e:
        run_status, error = "failed", str(e)
        raise
    finally:
        save_gemini_stats(drug, config.DIMENSION_FORECAST, run_status, error, time.time() - t0)


async def _run(drug: str, reset: bool, t0: float) -> dict:
    logger.info("%s\n[FORECAST] Starting for: %s\n%s", "=" * 60, drug, "=" * 60)

    # Step 1: glp_patent_loe -> the drug's patents, and its company
    loe_df = await asyncio.to_thread(load_patent_loe, drug)
    if loe_df.empty:
        return {"drug_name": drug,
                "error": f"No rows for '{drug}' in {config.BQ_PATENT_LOE_TABLE} — run primary_market_entry_horizon first."}
    company = await _resolve_company(drug)
    logger.info("[FORECAST] %s | %d patent(s) | company: %s", drug, len(loe_df), company or "unknown")

    # Step 2: layering (3), filing pattern (4) and business strategy (5) don't depend on one another
    forecast_s3, filing_rows, company_row = await asyncio.gather(
        _layering(drug, loe_df), _filing_pattern(drug, reset), _company_analysis(company, drug, reset))
    await asyncio.to_thread(write_forecast_s3, drug, forecast_s3)

    # Step 3: forecast + score. Sources a step could not produce are searched for on the web, as in step6.
    innovator_patterns = _innovator_text(drug, filing_rows) or await _search_innovator_patterns(drug, company)
    company_analysis = _company_text(company, company_row) or await _search_company_analysis(company, drug)
    scored, error = await forecast_and_score_drug(drug, company, forecast_s3 if not forecast_s3.empty else None,
                                                  innovator_patterns, company_analysis)
    if error:
        return {"drug_name": drug, "company": company, "error": f"Forecast failed: {error}",
                "processing_time_seconds": round(time.time() - t0, 1)}

    # Step 4: BigQuery (forecast_s3, filing_pattern_table and company_analysis_table were written above)
    await asyncio.to_thread(write_forecasted_loe, drug, scored)

    # Step 5: the dimension's PDF report, from this run's results. A failed report is recorded in the result
    # but does not fail the run.
    report = await asyncio.to_thread(_generate_report, drug, scored, loe_df)

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
