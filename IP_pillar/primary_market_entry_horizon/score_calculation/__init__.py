"""
score_calculation
─────────────────
Everything that produces or feeds the years-to-entry / score numbers.

    patent_scores.py         — per-patent score calculations (calculate_scores)
    phase_fetcher.py         — supplies phase_at_filing (BigQuery only: clinical_efficacy
                               + the drug-list/drug_details view — no local Excel fallback)
    approval_date_fetcher.py — supplies real-world approval_date_us / approval_date_eu
    scoring.py               — master-table Years to Entry / Avg Years to Entry (US & EP) / IP Dimension 1 Score
    merge_forecasted_loe.py  — patents -> glp_patent_loe rows; merge with forecasted_loe and re-score -> glp_master_loe rows

    from IP_pillar.primary_market_entry_horizon.score_calculation import calculate_scores
"""

from .approval_date_fetcher import fetch_approval_dates
from .merge_forecasted_loe import patents_to_dataframe, run_merge
from .patent_scores import (
    apply_pediatric_exclusivity,
    assign_controlling_patent_expiry_year,
    assign_estimated_approval_year,
    assign_exclusivity_year,
    assign_score,
    assign_us_ep_score,
    assign_years_to_entry,
    calculate_scores,
    effective_filing_date,
    effective_filing_year,
    patent_expiry_date,
    run_calculations,
)
from .phase_fetcher import (
    assign_patent_phases,
    canonicalise_drug_name,
    fetch_clinical_timeline,
)
from .scoring import ip_dimension_score

__all__ = [
    "apply_pediatric_exclusivity",
    "assign_controlling_patent_expiry_year",
    "assign_estimated_approval_year",
    "assign_exclusivity_year",
    "assign_patent_phases",
    "assign_score",
    "assign_us_ep_score",
    "assign_years_to_entry",
    "calculate_scores",
    "canonicalise_drug_name",
    "effective_filing_date",
    "effective_filing_year",
    "fetch_approval_dates",
    "fetch_clinical_timeline",
    "ip_dimension_score",
    "patent_expiry_date",
    "patents_to_dataframe",
    "run_calculations",
    "run_merge",
]
