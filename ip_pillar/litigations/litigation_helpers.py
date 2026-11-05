"""
litigation_helpers.py
─────────────────────
The helper litigation_analysis() calls (kept out of litigation_analysis.py, which holds only that function):
the PDF report.
"""

import logging

from .. import config

logger = logging.getLogger(__name__)


def generate_report(drug: str, rows: list) -> dict:
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
