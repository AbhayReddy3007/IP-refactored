"""
litigation_analysis.py
──────────────────────
Orchestrates the IP "Litigations" dimension (the fifth) for one drug: the patent litigation and court cases
found for its patents.

    litigation_analysis(drug_name)   — async; runs every step below end to end

    1. Load the drug's patent numbers and filing dates from patent_master (confidence above the threshold); the patents
       already in the litigation table are skipped, so a patent is never searched twice
                                                                        (retrieval, bq_utils)
    2. Innovator, and the brand names too when the drug is Marketed, with Gemini + Google Search
                                                                         (retrieval.is_marketed_drug, search)
    3. Litigation search per group of patents, with Gemini + Google Search: ANDA Paragraph IV, IPR at
       PTAB, EPO oppositions, India (Delhi High Court) and compounding-pharmacy suits. A group whose reply
       cannot be parsed is split and searched again                     (search.search_litigations)
    4. Case types normalised, duplicates dropped, cases grouped and summarised   (search)
    5. The litigation table -> BigQuery: the earlier rows plus this run's, with each patent's filing date
       from patent_master; the drug's older rows are replaced               (bq_utils)
    6. PDF report built from the drug's rows, uploaded to GCS on every run, also when every patent was
       already in the table                                                 (generate_report)

Input is patent_master only; it does not depend on primary_market_entry_horizon.

Usage:
    from IP_pillar.litigations import litigation_analysis

    result = await litigation_analysis("semaglutide")
"""

import asyncio
import logging
import time
from datetime import date

from .. import config
from ..bq_utils import save_gemini_stats
from ..drug_list import resolve_drug_name
from ..gemini_utils import reset_gemini_call_stats
from ..utils import normalize_patent_number
from .bq_utils import build_rows, load_existing_rows, merge_case_rows, write_litigation
from .retrieval import get_patents, is_marketed_drug
from .search import (
    build_summary,
    deduplicate_litigations,
    get_brand_and_innovator,
    group_by_type,
    search_litigations,
    unique_challengers,
)

logger = logging.getLogger(__name__)


def _generate_report(drug: str, rows: list) -> dict:
    """Build and upload the dimension's report; {"error": ...} if it fails."""
    if not rows:
        return {"skipped": "no patents", "gcs_uri": None, "archive_uri": None}
    # Imported here so reportlab is only needed when a report is built.
    from .generate_report.generate_report import report_for_drug

    logger.info("[LITIGATION] Generating the %s report ...", config.DIMENSION_LITIGATION)
    try:
        report = report_for_drug(drug, rows)
    except Exception as e:
        logger.exception("[LITIGATION] Report failed for %r", drug)
        return {"error": str(e), "gcs_uri": None, "archive_uri": None}
    logger.info("[LITIGATION] Report -> %s", report["gcs_uri"])
    return report


async def litigation_analysis(drug_name: str, reset: bool = False) -> dict:
    """
    Litigation pipeline for ONE drug, plus a Gemini usage row per model in config.GEMINI_STATS_TABLE
    (written even when the run fails).

    Args:
        drug_name: Drug name; resolved to the drug list's spelling and used for every output.
        reset:     Accepted so the runner can call every dimension alike. Patents already in the litigation
                   table are never searched again; delete a patent's rows from the table to search it again.

    Returns:
        {"drug_name", "patents", "patents_searched", "patents_skipped", "total_cases", "cases_by_type",
         "unique_challengers", "failed_patents", "processing_time_seconds", "output_tables": [...],
         "report": {"gcs_uri", "archive_uri"} or {"error"}}
        (plus "innovator", "brand_names" and "summary" when patents were searched)
        or {"drug_name", "error", ...} if the drug has no patent_master rows above the confidence threshold.
    """
    reset_gemini_call_stats()
    t0 = time.time()
    drug = resolve_drug_name(drug_name)
    run_status, error = "success", None
    try:
        result = await _run(drug, t0)
        if result.get("error"):
            run_status, error = "failed", result["error"]
        return result
    except Exception as e:
        run_status, error = "failed", str(e)
        raise
    finally:
        save_gemini_stats(drug, config.DIMENSION_LITIGATION, run_status, error, time.time() - t0)


async def _run(drug: str, t0: float) -> dict:
    logger.info("%s\n[LITIGATION] Starting for: %s\n%s", "=" * 60, drug, "=" * 60)

    # Step 1: patent_master -> the drug's patents that are not in the litigation table yet
    patents, filing_dates = await asyncio.to_thread(get_patents, drug)
    if not patents:
        return {"drug_name": drug,
                "error": f"No patents for '{drug}' in {config.PATENT_MASTER_TABLE} "
                         f"with confidence > {config.PATENT_CONFIDENCE_THRESHOLD}."}
    existing = await asyncio.to_thread(load_existing_rows, drug)
    searched_before = {normalize_patent_number(row["patent_number"]) for row in existing if row.get("patent_number")}
    pending = [p for p in patents if normalize_patent_number(p) not in searched_before]
    logger.info("[LITIGATION] %s | %d patent(s): %d already in the table, %d to search",
                drug, len(patents), len(patents) - len(pending), len(pending))

    base = {"drug_name": drug, "patents": len(patents), "patents_searched": 0,
            "patents_skipped": len(patents) - len(pending), "output_tables": []}
    if not pending:
        report = await asyncio.to_thread(_generate_report, drug, existing)
        return {**base, "total_cases": sum(1 for row in existing if row.get("case_type")),
                "processing_time_seconds": round(time.time() - t0, 1), "report": report}

    # Step 2: innovator, and brand names for a Marketed drug
    marketed = await asyncio.to_thread(is_marketed_drug, drug)
    brand_names, innovator = await get_brand_and_innovator(drug, marketed)

    # Step 3: litigation search
    raw, failed_patents = await search_litigations(pending, drug, innovator)
    logger.info("[LITIGATION] %s | %d raw case(s) across %d patent(s)", drug, len(raw), len(pending))

    # Step 4: de-duplicate, group, summarise (over the earlier cases too, as the totals cover the drug)
    new_cases = deduplicate_litigations(raw)
    searched = [p for p in pending if p not in failed_patents]    # a patent whose reply failed is searched again next run
    case_rows = merge_case_rows(existing, new_cases, searched)
    all_cases = [row for row in case_rows if row.get("case_type")]
    cases_by_type = group_by_type(all_cases)
    challengers = unique_challengers(all_cases)

    # Step 5: BigQuery
    elapsed = round(time.time() - t0, 1)
    summary = build_summary(all_cases, cases_by_type)
    rows = build_rows(drug, case_rows, summary, innovator, brand_names,
                      filing_dates, date.today().isoformat(), elapsed)
    rows_written = await asyncio.to_thread(write_litigation, drug, rows)

    # Step 6: the dimension's PDF report, from the drug's rows. A failed report is recorded in the result but
    # does not fail the run.
    report = await asyncio.to_thread(_generate_report, drug, rows)

    logger.info("[LITIGATION] Done in %.1fs - %d new case(s), %d case(s) in all, %d row(s) written",
                elapsed, len(new_cases), len(all_cases), rows_written)
    return {
        **base,
        "innovator":               innovator,
        "brand_names":             brand_names,
        "patents_searched":        len(searched),
        "total_cases":             len(all_cases),
        "cases_by_type":           {case_type: len(cases) for case_type, cases in cases_by_type.items()},
        "unique_challengers":      len(challengers),
        "failed_patents":          failed_patents,
        "summary":                 summary,
        "processing_time_seconds": elapsed,
        "output_tables":           [config.BQ_LITIGATION_TABLE],
        "report":                  report,
    }
