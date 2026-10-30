"""
generate_report_and_rationale
─────────────────────────────
    generate_rationale.py  — the drug's summary (no length limit), stored in BQ_COUNTRY_SCORE_TABLE.rationale
    generate_report.py     — the PDF report built from the two patent strength tables

Only the rationale helper is re-exported here; generate_report pulls in reportlab, which is
only imported when a report is built.
"""

from .generate_rationale import rationale_for_drug

__all__ = ["rationale_for_drug"]
