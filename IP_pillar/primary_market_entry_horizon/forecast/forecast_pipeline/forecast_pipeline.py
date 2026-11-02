"""
forecast_pipeline.py
────────────────────
Command-line entry point: imports the functions from the other files in this folder and runs them.
Run: python -m ip_pillar.primary_market_entry_horizon.forecast.forecast_pipeline.forecast_pipeline
"""

import asyncio
import sys
import argparse
from datetime import datetime
from .bq_loaders import _load_step23_from_bq
from .constants import BQ_FORECAST_S3_TABLE
from . import bq_writer, runner
from .runner import run_pipeline


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Step 6 + Scoring: Generate and score patent forecasts. "
                    "Reads from BQ, writes scored output to BQ."
    )
    parser.add_argument("--drug",         nargs="+", default=None,
                        help="One or more drug names — space-separated "
                             "(--drug Semaglutide Tirzepatide) or comma-separated "
                             "(--drug \"Semaglutide,Tirzepatide\"), or a mix of both")
    parser.add_argument("--limit",        type=int, default=None, help="Limit number of drugs")
    parser.add_argument("--dry-run",      action="store_true", help="List drugs only")
    parser.add_argument("--output-table", default=None,  help="Override output BQ table")
    args = parser.parse_args()

    if args.output_table:
        # the writer and the runner's summary read the table name from their own module
        bq_writer.BQ_OUTPUT_TABLE = runner.BQ_OUTPUT_TABLE = args.output_table

    # Get drug list
    if args.drug:
        # Each token can itself be comma-separated, so this handles
        # `--drug A B C`, `--drug "A,B,C"`, and any mix of the two.
        drugs = []
        for token in args.drug:
            for name in token.split(","):
                name = name.strip()
                if name and name not in drugs:
                    drugs.append(name)
        print(f"\n[DRUGS] {len(drugs)} drug(s) requested via --drug:")
        for i, d in enumerate(drugs, 1):
            print(f"  {i}. {d}")
    else:
        # Load drugs from Step 2/3 BQ table
        try:
            data = _load_step23_from_bq()
            drugs = sorted(data.keys()) if data else []
        except Exception:
            drugs = []

        if not drugs:
            print(f"No drugs found in {BQ_FORECAST_S3_TABLE}. Use --drug to specify one.")
            sys.exit(1)

    if args.limit:
        drugs = drugs[:args.limit]
        print(f"\n[LIMIT] First {args.limit} drug(s) only")

    if args.dry_run:
        print(f"\n[DRY RUN] Would process {len(drugs)} drug(s):")
        for i, d in enumerate(drugs, 1):
            print(f"  {i}. {d}")
        sys.exit(0)

    # Run
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

    start = datetime.now()
    _run(run_pipeline(drugs))
    print(f"Total time: {(datetime.now() - start).total_seconds():.1f}s")
