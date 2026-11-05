"""
generate_report_and_rationale
─────────────────────────────
    generate_rationale.py  — the drug's summary (no length limit), stored in BQ_ARBITRAGE_TABLE.rationale
    generate_report/       — the PDF reports built from the run's own results:
                             generate_dim4_report.py (launch sequencing and arbitrage), generate_pte_report.py (PTE analysis)

Only the rationale helper is re-exported here; generate_report pulls in reportlab, which is
only imported when a report is built.
"""

from .generate_rationale import rationale_for_drug

__all__ = ["rationale_for_drug"]
