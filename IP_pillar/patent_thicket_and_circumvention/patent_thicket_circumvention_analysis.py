"""
patent_thicket_circumvention_analysis.py
────────────────────────────────────────
Orchestrates the IP "Patent Thicket and Circumvention Feasibility" dimension for one drug:

    patent_thicket_and_circumvention(drug_name)   — async; runs every step below end to end

    1. Load the drug's glp_master_loe rows                    (bq_utils.load_master_loe, data_prep)
       and keep the patents patent_master approves
    2. Select its NON-BLOCKING patents                         (data_prep.select_non_blocking)
    3. Thicket score per jurisdiction (density and diversity of the claim categories) and
       the drug-level average                                  (scoring.score_drug)
    4. Circumvention analysis per claim category: patent chunks from AlloyDB + Gemini +
       Google Search grounding; finished categories are cached in GCS   (circumvention, checkpoint)
    5. Rationale: a summary of the drug written from the scores and the analysis; generated on
       every run; stored in the Rationale column of the score table                       (generate_report_and_rationale.generate_rationale)
    6. PDF report built from the run's results, uploaded to GCS on every run
                                                               (generate_report_and_rationale.generate_report)
    7. Circumvention_Table + Patent_Thicket_Score_Table -> BigQuery; each run replaces only
       that drug's rows                                        (bq_utils)

Input is glp_master_loe, so run primary_market_entry_horizon for the drug first.

Usage:
    from IP_pillar.patent_thicket_and_circumvention import patent_thicket_and_circumvention

    result = await patent_thicket_and_circumvention("semaglutide")
"""

import asyncio
import logging
import time

from .. import config
from ..drug_list import resolve_drug_name
from ..gemini_utils import reset_gemini_call_stats
from ..master_loe import load_master_data
from ..bq_utils import save_gemini_stats
from .bq_utils import write_circumvention_to_bq, write_score_to_bq
from .scoring.checkpoint import load_circumvention_cache, save_circumvention_cache
from .scoring.circumvention import group_patents_by_category, run_circumvention_analysis
from .retrieval.data_prep import get_claim_categories, select_non_blocking
from .scoring.scoring import score_drug
from .generate_report_and_rationale.generate_rationale import rationale_for_drug

logger = logging.getLogger(__name__)


def _generate_report(drug: str, drug_score: dict, circumvention: dict, rationale: str) -> dict:
    """Build and upload the dimension's report; {"error": ...} if it fails."""
    # Imported here so reportlab is only needed when a report is built.
    from .generate_report_and_rationale.generate_report import report_for_drug

    logger.info("[THICKET] Generating the %s report ...", config.DIMENSION_PATENT_THICKET)
    try:
        report = report_for_drug(drug, drug_score, circumvention, rationale)
    except Exception as e:
        logger.exception("[THICKET] Report failed for %r", drug)
        return {"error": str(e), "gcs_uri": None, "archive_uri": None}
    logger.info("[THICKET] Report -> %s", report["gcs_uri"])
    return report


async def patent_thicket_and_circumvention(drug_name: str, reset: bool = False) -> dict:
    """
    Patent-thicket and circumvention pipeline for ONE drug, plus a Gemini usage row per model in
    config.GEMINI_STATS_TABLE (written even when the run fails).

    Args:
        drug_name: Drug name; resolved to the drug list's spelling and used for every output.
        reset:     Ignore the cached circumvention results and analyse every category again.

    Returns:
        {"drug_name", "non_blocking_patents", "avg_final_score", "categories_analysed",
         "processing_time_seconds", "output_tables": [...], "rationale",
         "report": {"gcs_uri", "archive_uri"} or {"error"}}
        or {"drug_name", "error", ...} if the drug has no glp_master_loe rows.
    """
    reset_gemini_call_stats()
    t0 = time.time()
    run_status, error = "success", None
    try:
        result = await _run(drug_name, reset)
        if result.get("error"):
            run_status, error = "failed", result["error"]
        return result
    except Exception as e:
        run_status, error = "failed", str(e)
        raise
    finally:
        save_gemini_stats(resolve_drug_name(drug_name), config.DIMENSION_PATENT_THICKET,
                          run_status, error, time.time() - t0)


async def _run(drug_name: str, reset: bool) -> dict:
    t0 = time.time()
    drug = resolve_drug_name(drug_name)
    logger.info("%s\n[THICKET] Starting for: %s\n%s", "=" * 60, drug, "=" * 60)

    # Steps 1-2: glp_master_loe -> the drug's NON-BLOCKING patents
    df = await asyncio.to_thread(load_master_data, drug)
    if df.empty:
        return {"drug_name": drug,
                "error": f"No rows for '{drug}' in {config.BQ_MASTER_LOE_TABLE} — run primary_market_entry_horizon first."}
    all_categories = get_claim_categories(df)
    non_blocking = select_non_blocking(df, drug)
    if non_blocking.empty:
        return {"drug_name": drug, "non_blocking_patents": 0, "avg_final_score": None,
                "categories_analysed": [], "processing_time_seconds": round(time.time() - t0, 1), "output_tables": [],
                "rationale": config.NO_QUALIFYING_PATENTS_RATIONALE}

    # Step 3: thicket score
    drug_score = score_drug(drug, non_blocking, df, all_categories)
    for js in drug_score["jurisdiction_scores"]:
        logger.info("[THICKET] %s | %s | patents %s | areas %s | density %s | diversity %s | final %s",
                    drug, js["jurisdiction"], js["combined_total"], js["active_areas"],
                    js["density_score"], js["diversity_score"], js["final_score"])
    logger.info("[THICKET] %s | average final score %s", drug, drug_score["avg_final_score"])

    # Step 4: circumvention analysis (finished categories come from the cache)
    cached = {} if reset else await asyncio.to_thread(load_circumvention_cache, drug)
    circumvention = await run_circumvention_analysis(drug, group_patents_by_category(non_blocking), cached)
    await asyncio.to_thread(save_circumvention_cache, drug, circumvention["results_by_category"])

    # Step 5: rationale — the drug's summary, generated on every run (not stored in BigQuery)
    rationale = await asyncio.to_thread(rationale_for_drug, drug, drug_score, circumvention)

    # Step 6: the dimension's PDF report, built from this run's results. A failed report is
    # recorded in the result but does not fail the run.
    report = await asyncio.to_thread(_generate_report, drug, drug_score, circumvention, rationale)

    # Step 7: BigQuery
    await asyncio.to_thread(write_circumvention_to_bq, drug, circumvention)
    await asyncio.to_thread(write_score_to_bq, drug_score, rationale)

    elapsed = time.time() - t0
    logger.info("[THICKET] Done in %.1fs - %d NON-BLOCKING row(s), %d categor(ies) | average score %s",
                elapsed, len(non_blocking), len(circumvention["categories_analysed"]), drug_score["avg_final_score"])
    return {
        "drug_name":               drug,
        "non_blocking_patents":    len(non_blocking),
        "avg_final_score":         drug_score["avg_final_score"],
        "categories_analysed":     circumvention["categories_analysed"],
        "processing_time_seconds": round(elapsed, 1),
        "output_tables":           [config.BQ_CIRCUMVENTION_TABLE, config.BQ_THICKET_SCORE_TABLE],
        "rationale":               rationale,
        "report":                  report,
    }
