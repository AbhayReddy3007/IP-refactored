"""
primary_market_entry_horizon
─────────────────────────────
Runs the whole IP Dimension process for a drug and derives, from its analysed
patent list, how many years remain before a generic/biosimilar can realistically
enter the market (years_to_entry), and the corresponding 1-5 scores.

    from IP_refactored.primary_market_entry_horizon import calculate_scores, primary_market_entry_horizon

    result  = await primary_market_entry_horizon("semaglutide")   # the full pipeline for one drug
    patents = calculate_scores(patents)                           # just the score calculations

Also packages the supporting modules that feed / consume the score
calculation (ported from cog/):
    primary_market_entry_horizon.py — orchestrates the whole per-drug process
    score_calculation/       — patent_scores (calculate_scores), phase_fetcher (phase_at_filing),
                               approval_date_fetcher (real approval dates), scoring (master-table scoring),
                               merge_forecasted_loe (glp_patent_loe rows; merge with forecasted_loe -> glp_master_loe rows)
    bq_utils.py              — every BigQuery read, and the LOE table writes (BigQuery + a JSON snapshot in GCS)
    generate_report_and_rationale/ — the "Rationale" column of glp_master_loe + the PDF report built from it
(the original callers — tools.py's two-pass orchestration, and agent.py,
which has been removed — are superseded by primary_market_entry_horizon();
run_ip_dimension.py at the top of the IP_refactored package picks the drugs.)
"""

from .primary_market_entry_horizon import primary_market_entry_horizon
from .score_calculation import (
    apply_pediatric_exclusivity,
    assign_controlling_patent_expiry_year,
    assign_estimated_approval_year,
    assign_exclusivity_year,
    assign_patent_phases,
    assign_score,
    assign_us_ep_score,
    assign_years_to_entry,
    calculate_scores,
    canonicalise_drug_name,
    effective_filing_date,
    effective_filing_year,
    fetch_approval_dates,
    fetch_clinical_timeline,
    ip_dimension_score,
    patent_expiry_date,
    patents_to_dataframe,
    run_calculations,
    run_merge,
)
from .bq_utils import publish_table

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
    "primary_market_entry_horizon",
    "publish_table",
    "run_calculations",
    "run_merge",
]