"""
patern_analysis.py
──────────────────
Command-line entry point: imports the functions from the other files in this folder and runs them.
Run: python -m ip_pillar.primary_market_entry_horizon.forecast.patern_analysis.patern_analysis
"""

import asyncio
import argparse
from datetime import datetime
from .batch import list_all_gcs_drugs, run_all


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int)
    parser.add_argument("--drug")

    args = parser.parse_args()

    if args.drug:
        drugs = [args.drug]
    else:
        drugs = list_all_gcs_drugs()

    if args.limit:
        drugs = drugs[:args.limit]

    start = datetime.now()

    asyncio.run(run_all(drugs))

    print(f"\nTime taken: {(datetime.now() - start).total_seconds():.2f}s")
