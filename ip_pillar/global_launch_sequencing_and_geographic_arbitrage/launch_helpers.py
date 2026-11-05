"""
launch_helpers.py
─────────────────
The helper global_launch_sequencing_and_geographic_arbitrage() calls (kept out of
global_launch_sequencing_and_geographic_arbitrage.py, which holds only that function): the two PDF reports.
"""

import logging

from .. import config

logger = logging.getLogger(__name__)


def generate_reports(drug: str, shortlisted, arbitrage, rationale: str) -> dict:
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
