"""
generate_report_and_rationale
─────────────────────────────
    generate_rationale.py  — the drug's summary (no length limit), generated on every run; not stored in BigQuery
    generate_report.py     — the PDF report built from the run's own results

Only the rationale helper is re-exported here; generate_report pulls in reportlab, which is
only imported when a report is built.
"""

from .generate_rationale import rationale_for_drug

__all__ = ["rationale_for_drug"]
