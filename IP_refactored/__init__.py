"""
IP_refactored
─────────────
IP Dimension 1 (patent blocking / market-entry-horizon) pipeline.

Layout:
    config.py                     — every setting / env var (loads .env)
    utils.py                      — pure shared helpers (normalisation, jurisdictions, dates, BQ naming)
    gcp_utils.py                  — every Google Cloud / Gemini call (clients, GCS, BigQuery)
    chunking/                     — patent listing, text extraction, chunking, AlloyDB storage
    blocking_analysis/            — 5-step patent blocking analysis
    primary_market_entry_horizon/ — phase / approval-date lookup, score calculation,
                                    glp_patent_loe / glp_master_loe tables (merge, scoring, BQ + JSON export)
    run_ip_dimension.py           — entry point: python -m IP_refactored.run_ip_dimension --drug <name>
"""
