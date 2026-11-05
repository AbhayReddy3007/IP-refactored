"""
generate_report_and_rationale
─────────────────────────────
LOE (Primary Market) rationale + PDF report.

    generate_rationale.py  — per-drug LOE metrics and the "Rationale" text stored in glp_master_loe
    generate_report.py     — 2-page PDF report built from glp_master_loe (python -m ...generate_report)

Only the rationale helpers are re-exported here; generate_report pulls in
reportlab, so import it explicitly when you need it.
"""

from .generate_rationale import extract_drug_data, generate_rationale, rationale_for_drug

__all__ = ["extract_drug_data", "generate_rationale", "rationale_for_drug"]
