"""
strength_helpers.py
───────────────────
The helpers patent_strength() calls (kept out of patent_strength_analysis.py, which holds only that function):
the run summaries written to the log and the PDF report.
"""

import logging

from .. import config
from .scoring.constants import ACTIVE_SFS, SF_DISPLAY_NAMES, SF_SCORE_LABELS

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Run summaries (written to the log) and the report
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


def generate_report(drug_name: str) -> dict:
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
