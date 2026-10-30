"""
forecast
────────
IP dimension: how many more patents of each type the innovator is likely to file in each jurisdiction,
scored like the real patents. Reads glp_patent_loe (the output of primary_market_entry_horizon) and writes
forecasted_loe, which the next primary_market_entry_horizon run merges into glp_master_loe.

    forecast_analysis.py   forecast_analysis(drug) — the whole pipeline for one drug
    bq_utils.py            BigQuery reads and writes specific to this dimension (shared ones: IP_pillar/bq_utils.py)

    step3.py   IP landscape, patent layering (AlloyDB patent text + Gemini), phase at filing, typical filing stage -> forecast_s3
    step4.py   the innovator's filing pattern across its portfolio (Gemini + Google Search) -> filing_pattern_table
    step5.py   the company's business strategy and incentive to expand protection (SEC EDGAR + Gemini) -> company_analysis_table
    step6.py   the patent filing forecast per patent type and jurisdiction (Gemini) and its scoring -> forecasted_loe

    generate_report/   written on every run
        generate_report.py  the PDF report (Gemini text from the forecast and the existing patents), uploaded to GCS

Each step file can still be run on its own with `python -m IP_pillar.forecast.stepN`.
"""

from .forecast_analysis import forecast_analysis

__all__ = ["forecast_analysis"]
