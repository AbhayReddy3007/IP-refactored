"""
patent_layering.py
──────────────────
Command-line entry point: imports the functions from the other files in this folder and runs them.
Run: python -m ip_pillar.primary_market_entry_horizon.forecast.patent_layering.patent_layering
"""

import asyncio
from typing import Optional
import pandas as pd
from .display import print_forecast_s3, print_step1, print_step3
from .filing_analysis import step3_filing_analysis
from .ip_landscape import step1_ip_landscape
from .layering import step2_patent_layering
from .output import _save_excel, merge_to_forecast_s3, upload_to_bigquery


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Forecasting pipeline (BigQuery edition).")
    parser.add_argument(
        "--upload", action="store_true", default=False,
        help="Upload the final merged table to BigQuery after processing."
    )
    parser.add_argument(
        "--drugs", default=None,
        help=(
            "Comma-separated drug names. When provided, step3 only processes "
            "these drugs (case-insensitive). When absent, processes all drugs "
            "in loe_table. Used by the orchestrator to shard step3 across "
            "Cloud Run tasks the same way step5 is sharded."
        ),
    )
    args = parser.parse_args()

    # Parse --drugs into a set (None means 'all')
    drug_filter: Optional[set] = None
    if args.drugs:
        drug_filter = {d.strip() for d in args.drugs.split(",") if d.strip()}
        print(f"[CLI] --drugs filter: {sorted(drug_filter)}")

    def _run(coro):
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                raise RuntimeError
            return loop.run_until_complete(coro)
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(coro)

    # ══════════════════════════════════════════════════════════════════════
    # STEP 1: IP Landscape
    # ══════════════════════════════════════════════════════════════════════
    print("\n── STEP 1 — IP Landscape of Marketed Drugs ──\n")
    step1_result = _run(step1_ip_landscape(drug_filter=drug_filter))
    print_step1(step1_result)

    # ══════════════════════════════════════════════════════════════════════
    # STEP 2: Patent Layering Pattern
    # ══════════════════════════════════════════════════════════════════════
    print("\n── STEP 2 — Patent Layering Pattern ──\n")
    step2_result = _run(step2_patent_layering(drug_filter=drug_filter))
    print_forecast_s3(step2_result)

    # ══════════════════════════════════════════════════════════════════════
    # STEP 3: Filing Timing vs. Development Milestones
    # ══════════════════════════════════════════════════════════════════════
    step3_result = pd.DataFrame()
    if not step2_result.empty:
        print("\n── STEP 3 — Filing Timing vs. Development Milestones ──\n")
        step3_result = _run(step3_filing_analysis(step2_result))
        print_step3(step3_result)

    # ══════════════════════════════════════════════════════════════════════
    # MERGE → Single flat table (forecast_s3)
    # ══════════════════════════════════════════════════════════════════════
    print("\n── MERGING — All steps into forecast_s3 ──\n")
    forecast_s3 = merge_to_forecast_s3(step1_result, step2_result, step3_result)

    if not forecast_s3.empty:
        # Save to GCS
        _save_excel(forecast_s3, "forecast_s3.xlsx", "forecast_s3")

        # Upload to BigQuery if requested
        if args.upload:
            upload_to_bigquery(forecast_s3)
        else:
            print(f"[INFO] To upload to BigQuery, re-run with --upload")

    print("\n── DONE ──")
