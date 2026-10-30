"""
IP_pillar
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
    forecast/                     — forecast_analysis: patent filing forecast per drug -> forecasted_loe (run by the horizon)
    patent_strength/              — dimension 2: legal robustness of the patents
    patent_thicket_and_circumvention/ — dimension 3: thicket density and design-around feasibility
    global_launch_sequencing_and_geographic_arbitrage/ — dimension 4: expiry per jurisdiction, launch order, arbitrage
    litigations/                  — dimension 5: patent litigation and court cases per patent
    run_ip_dimension.py           — entry point: python -m IP_pillar.run_ip_dimension --drug <name>
"""
