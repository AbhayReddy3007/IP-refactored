"""
global_launch_sequencing_and_geographic_arbitrage
─────────────────────────────────────────────────
IP dimension: in which order a drug's patents let it launch across jurisdictions, and how much
geographic arbitrage the differences in expiry allow. Reads glp_master_loe (the output of
primary_market_entry_horizon) and writes shortlisted_secondary_patents_table + arbitrage_summary_table.

    global_launch_sequencing_and_geographic_arbitrage.py   the pipeline for one drug
    bq_utils.py     BigQuery reads / writes specific to this dimension (shared ones: IP_refactored/bq_utils.py)
    filters.py      shortlist: target jurisdictions, granted, BLOCKING, key claim categories
    approval.py     earliest approval date per jurisdiction (Gemini, two-pass)
    pte.py          patent term extension per patent (Gemini)
    expiry.py       patent expiry: filing + 20 years, PTE, post-approval cap
    arbitrage.py    launch order, LOE gaps, protection gap, 1-5 arbitrage score, Dimension IV score
    constants.py    filters, jurisdictions, PTE rules
    generate_report_and_rationale/   written on every run
        generate_rationale.py   the drug's summary of the launch order and arbitrage (stored in the arbitrage table)
        generate_report/        the PDF reports (Dimension IV report, PTE analysis), uploaded to GCS
"""

from .global_launch_sequencing_and_geographic_arbitrage import global_launch_sequencing_and_geographic_arbitrage

__all__ = ["global_launch_sequencing_and_geographic_arbitrage"]
