"""
patent_strength_analysis.py
───────────────────────────
Orchestrates the IP "Patent Strength and Invalidity Opportunity" dimension (package: patent_strength) for one drug:

    patent_strength(drug_name)   — async; runs every step below end to end

    1. Load the drug's glp_master_loe rows                (bq_utils.load_master_loe)
    2. Shortlist: Blocking + granted + not Forecasted + approved in patent_master   (shortlist)
    3. Load already-scored sub-factors                     (GCS checkpoint, BigQuery fallback)
    4. Score each patent on 4 legal-robustness sub-factors with Gemini
         SF1 (40%) Novelty & Non-Obviousness      SF2 (30%) Obvious-to-Combine Risk
         SF3 (20%) Prosecution History Vulnerability   SF4 (10%) Secondary Considerations
       using chunks retrieved once per patent from AlloyDB     (chunks, scoring)
    5. Country-weighted score and final patent score per drug (scoring.compute_country_wise_scores)
    6. Rationale: a short summary of the drug (<= 200 words)  (generate_report_and_rationale.generate_rationale)
    7. patent_strength_table + patent_strength_country_score_table (with the rationale) -> BigQuery   (bq_utils)
    8. PDF report built from those tables, uploaded to GCS     (generate_report_and_rationale.generate_report)

Input is glp_master_loe, so run primary_market_entry_horizon for the drug first.

Usage:
    from IP_refactored.patent_strength import patent_strength

    result = await patent_strength("semaglutide")
"""

import asyncio
import logging
import time
from typing import Optional

from .. import config
from ..drug_list import resolve_drug_name
from ..gemini_utils import reset_gemini_call_stats
from ..bq_utils import load_master_loe, save_gemini_stats
from .storage.bq_utils import build_output_bigquery
from .scoring.constants import ACTIVE_SFS, SF_DISPLAY_NAMES, SF_SCORE_LABELS
from .scoring.pipeline import load_score_cache, score_all_patents
from .scoring.scoring import compute_country_wise_scores
from .generate_report_and_rationale.generate_rationale import rationale_for_drug
from .retrieval.shortlist import build_shortlist

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Run summaries (written to the log)
# ─────────────────────────────────────────────

_RULE = "=" * 60


def log_country_scores(country_scores: dict) -> None:
    """Log the per-drug, per-jurisdiction weighted score tables."""
    logger.info(_RULE)
    logger.info("Country-wise weighted scores")
    logger.info(_RULE)

    for drug, drug_entry in country_scores["drug_details"].items():
        logger.info("Drug: %s", drug)
        logger.info(
            "  %-12s %-26s %-8s %-6s %-10s %s",
            "Jurisdiction", "Country", "Weight", "#Pat", "Avg WS", "Country WS",
        )
        logger.info("  %s", "-" * 80)
        for jurisdiction, jdata in sorted(
            drug_entry["jurisdictions"].items(),
            key=lambda item: (-item[1]["country_weight"], item[0]),
        ):
            avg = jdata["avg_weighted_score"]
            logger.info(
                "  %-12s %-26s %-8s %-6s %-10s %.4f",
                jurisdiction, jdata["label"], jdata["country_weight"],
                jdata["patent_count"], f"{avg:.2f}" if avg is not None else "N/A",
                jdata["country_weighted_score"],
            )
        logger.info("  FINAL PATENT SCORE: %.4f", drug_entry["final_patent_score"])


def log_sub_factor_summary(results: list[dict], total: int) -> None:
    """Log score counts and averages for each sub-factor."""
    for sf_key in ACTIVE_SFS:
        scored = [
            r[sf_key]["score"] for r in results
            if r.get(sf_key, {}).get("score") is not None
        ]
        if not scored:
            continue
        labels = SF_SCORE_LABELS[sf_key]
        logger.info("%s:", SF_DISPLAY_NAMES[sf_key])
        logger.info("  Patents scored : %d / %d", len(scored), total)
        logger.info("  Average score  : %.2f", sum(scored) / len(scored))
        for score in range(1, 6):
            logger.info("  Score %d (%-45s): %d", score, labels[score][:45], scored.count(score))


def log_final_scores(country_scores: dict) -> None:
    """Log the final country-weighted score for each drug."""
    logger.info(_RULE)
    logger.info("FINAL PATENT SCORES (Country-Weighted)")
    logger.info(_RULE)
    for drug, drug_entry in country_scores["drug_details"].items():
        logger.info("  %-30s -> %.4f", drug, drug_entry["final_patent_score"])


def log_output_tables() -> None:
    """Log where the results were written."""
    logger.info(_RULE)
    logger.info("BigQuery output complete:")
    logger.info("  -> %s", config.BQ_PATENT_STRENGTH_TABLE)
    logger.info("  -> %s", config.BQ_COUNTRY_SCORE_TABLE)
    logger.info(_RULE)


# ─────────────────────────────────────────────
# Pipeline
# ─────────────────────────────────────────────


def _generate_report(drug_name: str) -> dict:
    """Build and upload the dimension's report; {"error": ...} if it fails."""
    # Imported here so reportlab is only needed when a report is built.
    from .generate_report_and_rationale.generate_report import report_for_drug

    logger.info("[STRENGTH] Generating the %s report ...", config.DIMENSION_PATENT_STRENGTH)
    try:
        report = report_for_drug(drug_name)
    except Exception as e:
        logger.exception("[STRENGTH] Report failed for %r", drug_name)
        return {"error": str(e), "gcs_uri": None, "archive_uri": None}
    logger.info("[STRENGTH] Report -> %s", report["gcs_uri"])
    return report


async def patent_strength(drug_name: str, reset: bool = False) -> dict:
    """
    Patent-strength pipeline for ONE drug, plus a Gemini usage row per model in
    config.GEMINI_STATS_TABLE (written even when the run fails).

    Args:
        drug_name: Drug name; resolved to the drug list's spelling and used for every output.
        reset:     Ignore the existing checkpoint / scores and re-score everything.

    Returns:
        {"drug_name", "patents_scored", "final_patent_score", "processing_time_seconds",
         "output_tables": [...], "rationale", "report": {"gcs_uri", "archive_uri"} or {"error"}}
        or {"drug_name", "error", ...} if the drug has nothing to score.
    """
    reset_gemini_call_stats()
    t0 = time.time()
    run_status, error = "success", None
    try:
        result = await _run_patent_strength(drug_name, reset)
        if result.get("error"):
            run_status, error = "failed", result["error"]
        return result
    except Exception as e:
        run_status, error = "failed", str(e)
        raise
    finally:
        save_gemini_stats(resolve_drug_name(drug_name), config.DIMENSION_PATENT_STRENGTH, run_status, error, time.time() - t0)


async def _run_patent_strength(drug_name: str, reset: bool) -> dict:
    t0 = time.time()
    drug_name = resolve_drug_name(drug_name)
    logger.info("%s\n[STRENGTH] Starting for: %s\n%s", "=" * 60, drug_name, "=" * 60)

    # Steps 1-2: glp_master_loe -> shortlist
    master = await asyncio.to_thread(load_master_loe, drug_name)
    if master.empty:
        return {
            "drug_name": drug_name,
            "error": f"No rows for '{drug_name}' in {config.BQ_MASTER_LOE_TABLE} — run primary_market_entry_horizon first.",
        }
    shortlist = build_shortlist(master, drug_name)
    if shortlist.empty:
        logger.info("[STRENGTH] No patents match the shortlist criteria for '%s'.", drug_name)
        return {
            "drug_name": drug_name, "patents_scored": 0, "final_patent_score": None,
            "processing_time_seconds": round(time.time() - t0, 1), "output_tables": [],
            "rationale": config.NO_QUALIFYING_PATENTS_RATIONALE,
        }

    # Steps 3-4: sub-factor scoring (cached sub-factors are not re-run)
    cache = await asyncio.to_thread(load_score_cache, drug_name, reset)
    results = await score_all_patents(shortlist, cache, drug_name)

    # Step 5: country-weighted scores
    country_scores = compute_country_wise_scores(shortlist, results)
    log_country_scores(country_scores)

    # Step 6: rationale — the drug's short summary (<= 200 words), stored on every country row
    rationale = await asyncio.to_thread(rationale_for_drug, drug_name, shortlist, results, country_scores)

    # Step 7: BigQuery
    await asyncio.to_thread(build_output_bigquery, shortlist, results, country_scores, drug_name, rationale)

    log_sub_factor_summary(results, total=len(shortlist))
    log_final_scores(country_scores)
    log_output_tables()

    # Step 8: the dimension's PDF report (built from the rows just written).
    # A failed report is recorded in the result but does not fail the run.
    report = await asyncio.to_thread(_generate_report, drug_name)

    final_score: Optional[float] = next(iter(country_scores["drug_details"].values()))["final_patent_score"]
    elapsed = time.time() - t0
    logger.info("[STRENGTH] Done in %.1fs - %d patent(s) | final score %s", elapsed, len(shortlist), final_score)
    return {
        "drug_name":               drug_name,
        "patents_scored":          len(shortlist),
        "final_patent_score":      final_score,
        "processing_time_seconds": round(elapsed, 1),
        "output_tables":           [config.BQ_PATENT_STRENGTH_TABLE, config.BQ_COUNTRY_SCORE_TABLE],
        "rationale":               rationale,
        "report":                  report,
    }
