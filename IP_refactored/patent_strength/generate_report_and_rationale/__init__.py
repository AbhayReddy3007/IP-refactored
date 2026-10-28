"""
generate_report_and_rationale
─────────────────────────────
    generate_rationale.py  — the drug's short summary (<= 200 words), stored in BQ_COUNTRY_SCORE_TABLE.rationale
    generate_report.py     — the PDF report built from the two patent strength tables

Only the rationale helper is re-exported here; generate_report pulls in reportlab, which is
only imported when a report is built.
"""

from .generate_rationale import RATIONALE_MAX_WORDS, rationale_for_drug

__all__ = ["RATIONALE_MAX_WORDS", "rationale_for_drug"]
