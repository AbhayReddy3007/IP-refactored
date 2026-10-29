"""
global_launch_sequencing_and_geographic_arbitrage.py
────────────────────────────────────────────────────
Orchestrates the IP "Global Launch Sequencing and Geographic Arbitrage" dimension (the fourth) for one drug:

    global_launch_sequencing_and_geographic_arbitrage(drug_name)   — async; runs every step below end to end

    1. Load the drug's glp_master_loe rows, approved patents only          (master_loe.load_master_data)
    2. Shortlist: target jurisdictions, granted, BLOCKING, key claim categories   (filters.shortlist_patents)
    3. Approval date per marketed jurisdiction and PTE per patent with Gemini; lookups of an earlier
       run are reused from BigQuery                                         (approval, pte, bq_utils.load_analysis_cache)
    4. Patent expiry per jurisdiction (filing + 20 years, PTE, post-approval cap)   (expiry)
    5. Arbitrage per jurisdiction: launch order, LOE gaps, protection gap, 1-5 score, and the
       drug's Dimension IV score and rating                                 (arbitrage)
    6. Rationale: a summary of the drug written from the launch order and arbitrage scores;
       generated on every run                                               (generate_report_and_rationale.generate_rationale)
    7. Two PDF reports built from the run's results, uploaded to GCS on every run: the Global Launch
       Sequencing and Geographic Arbitrage report and the PTE Analysis report
                                                                            (generate_report_and_rationale.generate_report)
    8. shortlisted_secondary_patents_table + arbitrage_summary_table (with the rationale) -> BigQuery;
       each run replaces only that drug's rows                              (bq_utils)

Input is glp_master_loe, so run primary_market_entry_horizon for the drug first.

Usage:
    from IP_refactored.global_launch_sequencing_and_geographic_arbitrage import (
        global_launch_sequencing_and_geographic_arbitrage)

    result = await global_launch_sequencing_and_geographic_arbitrage("semaglutide")
"""

import asyncio
import logging
import time

from .. import config
from ..bq_utils import save_gemini_stats
from ..drug_list import resolve_drug_name
from ..gemini_utils import reset_gemini_call_stats
from ..master_loe import load_master_data
from .approval import fill_approval_dates, marketed_jurisdictions
from .arbitrage import attach_dimension_iv, compute_arbitrage
from .bq_utils import load_analysis_cache, write_arbitrage, write_launch_patents
from .constants import FINAL_COLUMNS
from .expiry import add_expiry_gap, apply_type_overrides, compute_expiries, longest_expiry_per_jurisdiction
from .filters import shortlist_patents, years_to_entry_map
from .generate_report_and_rationale.generate_rationale import rationale_for_drug
from .pte import fill_pte

logger = logging.getLogger(__name__)


def _generate_reports(drug: str, shortlisted, arbitrage, rationale: str) -> dict:
    """Build and upload the dimension's two reports: {"report": ..., "pte_report": ...}, each with an
    "error" instead of its URIs if it fails."""
    # Imported here so reportlab is only needed when a report is built.
    from .generate_report_and_rationale.generate_report import generate_dim4_report, generate_pte_report

    builds = {
        "report": (config.DIMENSION_GLOBAL_LAUNCH, lambda: generate_dim4_report.report_for_drug(drug, shortlisted, arbitrage, rationale)),
        "pte_report": (config.DIMENSION_PTE_ANALYSIS, lambda: generate_pte_report.report_for_drug(drug, shortlisted, arbitrage)),
    }
    reports = {}
    for key, (name, build) in builds.items():
        logger.info("[LAUNCH] Generating the %s report ...", name)
        try:
            reports[key] = build()
        except Exception as e:
            logger.exception("[LAUNCH] %s report failed for %r", name, drug)
            reports[key] = {"error": str(e), "gcs_uri": None, "archive_uri": None}
            continue
        logger.info("[LAUNCH] %s report -> %s", name, reports[key]["gcs_uri"])
    return reports


async def global_launch_sequencing_and_geographic_arbitrage(drug_name: str, reset: bool = False) -> dict:
    """
    Global-launch-sequencing and geographic-arbitrage pipeline for ONE drug, plus a Gemini usage row per
    model in config.GEMINI_STATS_TABLE (written even when the run fails).

    Args:
        drug_name: Drug name; resolved to the drug list's spelling and used for every output.
        reset:     Ignore the approval dates and PTE of the previous run and look everything up again.

    Returns:
        {"drug_name", "shortlisted_patents", "launch_order": [jurisdictions], "dimension_iv_score",
         "dimension_iv_rating", "processing_time_seconds", "output_tables": [...], "rationale",
         "report" and "pte_report": {"gcs_uri", "archive_uri"} or {"error"}}
        or {"drug_name", "error", ...} if the drug has no glp_master_loe rows.
    """
    reset_gemini_call_stats()
    t0 = time.time()
    drug = resolve_drug_name(drug_name)
    run_status, error = "success", None
    try:
        logger.info("%s\n[LAUNCH] Starting for: %s\n%s", "=" * 60, drug, "=" * 60)

        def result(**fields) -> dict:
            return {"drug_name": drug, "processing_time_seconds": round(time.time() - t0, 1), **fields}

        # Steps 1-2: glp_master_loe -> shortlist
        df = await asyncio.to_thread(load_master_data, drug)
        if df.empty:
            run_status = "failed"
            error = f"No rows for '{drug}' in {config.BQ_MASTER_LOE_TABLE} — run primary_market_entry_horizon first."
            return result(error=error)
        filtered = shortlist_patents(df)
        shortlisted = filtered["shortlisted"]
        if shortlisted.empty:
            logger.info("[LAUNCH] No patents match the shortlist criteria for '%s'.", drug)
            return result(shortlisted_patents=0, launch_order=[], dimension_iv_score=None,
                          dimension_iv_rating=None, output_tables=[],
                          rationale=config.NO_QUALIFYING_PATENTS_RATIONALE)

        # Steps 3-4: approval dates, PTE, expiries (lookups of the previous run are reused)
        approval_cache, pte_cache = ({}, {}) if reset else await asyncio.to_thread(load_analysis_cache, drug)
        await fill_approval_dates(shortlisted, drug, approval_cache, marketed_jurisdictions(df))
        await fill_pte(shortlisted, drug, pte_cache)
        compute_expiries(shortlisted)
        apply_type_overrides(shortlisted, filtered["filtered_df"])
        shortlisted = longest_expiry_per_jurisdiction(shortlisted)
        add_expiry_gap(shortlisted, years_to_entry_map(df))

        # Step 5: arbitrage and the Dimension IV score
        arbitrage = compute_arbitrage(shortlisted, filtered["full_filtered"])
        shortlisted, dimension_iv_score, dimension_iv_rating = attach_dimension_iv(shortlisted, arbitrage)
        logger.info("[LAUNCH] %s | Dimension IV score %s — %s", drug, dimension_iv_score, dimension_iv_rating)
        shortlisted = shortlisted[[c for c in FINAL_COLUMNS if c in shortlisted.columns]]

        # Step 6: rationale — the drug's summary, stored on every arbitrage row
        rationale = await asyncio.to_thread(rationale_for_drug, drug, arbitrage)

        # Step 7: the dimension's PDF report, from this run's results. A failed report is recorded in
        # the result but does not fail the run.
        reports = (await asyncio.to_thread(_generate_reports, drug, shortlisted, arbitrage, rationale)
                   if not arbitrage.empty else {})

        # Step 8: BigQuery
        await asyncio.to_thread(write_launch_patents, drug, shortlisted)
        await asyncio.to_thread(write_arbitrage, drug, arbitrage, rationale)

        logger.info("[LAUNCH] Done in %.1fs - %d shortlisted patent(s)", time.time() - t0, len(shortlisted))
        return result(
            shortlisted_patents=len(shortlisted),
            launch_order=arbitrage["Jurisdiction"].tolist() if not arbitrage.empty else [],
            dimension_iv_score=dimension_iv_score,
            dimension_iv_rating=dimension_iv_rating,
            output_tables=[config.BQ_LAUNCH_PATENTS_TABLE, config.BQ_ARBITRAGE_TABLE],
            rationale=rationale,
            **reports,
        )
    except Exception as e:
        run_status, error = "failed", str(e)
        raise
    finally:
        save_gemini_stats(drug, config.DIMENSION_GLOBAL_LAUNCH, run_status, error, time.time() - t0)
