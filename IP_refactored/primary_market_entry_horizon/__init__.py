"""
primary_market_entry_horizon
─────────────────────────────
Score-calculation sub-package. Derives, from a drug's analysed patent list,
how many years remain before a generic/biosimilar can realistically enter
the market (years_to_entry), and the corresponding 1-5 scores.

    from IP_refactored.primary_market_entry_horizon import primary_market_entry_horizon

    patents = primary_market_entry_horizon(patents)

Also packages the supporting modules that feed / consume the score
calculation (ported from cog/):
    phase_fetcher.py         — supplies phase_at_filing (BigQuery + Excel fallback)
    approval_date_fetcher.py — supplies real-world approval_date_us / approval_date_eu
    excel_exporter.py        — writes the final scored patents to Excel in GCS
(the original callers — tools.py's two-pass orchestration, and agent.py,
which has been removed — are superseded by dimension_1.py at the top of
the IP_refactored package.)
"""

from .primary_market_entry_horizon import (
    apply_pediatric_exclusivity,
    assign_controlling_patent_expiry_year,
    assign_estimated_approval_year,
    assign_exclusivity_year,
    assign_score,
    assign_us_ep_score,
    assign_years_to_entry,
    effective_filing_date,
    effective_filing_year,
    patent_expiry_date,
    primary_market_entry_horizon,
    run_calculations,
)
from .phase_fetcher import (
    assign_patent_phases,
    canonicalise_drug_name,
    fetch_clinical_timeline,
)
from .approval_date_fetcher import fetch_approval_dates
from .excel_exporter import export_combined_excel, export_to_excel

__all__ = [
    "apply_pediatric_exclusivity",
    "assign_controlling_patent_expiry_year",
    "assign_estimated_approval_year",
    "assign_exclusivity_year",
    "assign_patent_phases",
    "assign_score",
    "assign_us_ep_score",
    "assign_years_to_entry",
    "canonicalise_drug_name",
    "effective_filing_date",
    "effective_filing_year",
    "export_combined_excel",
    "export_to_excel",
    "fetch_approval_dates",
    "fetch_clinical_timeline",
    "patent_expiry_date",
    "primary_market_entry_horizon",
    "run_calculations",
]
