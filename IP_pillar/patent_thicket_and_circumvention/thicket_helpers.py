"""
thicket_helpers.py
──────────────────
The helper patent_thicket_and_circumvention() calls (kept out of patent_thicket_circumvention_analysis.py,
which holds only that function): the PDF report.
"""

import logging

from .. import config

logger = logging.getLogger(__name__)


def generate_report(drug: str, drug_score: dict, circumvention: dict, rationale: str) -> dict:
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
