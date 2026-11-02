"""
strategy_analysis.py
────────────────────
Command-line entry point: imports the functions from the other files in this folder and runs them.
Run: python -m ip_pillar.primary_market_entry_horizon.forecast.strategy_analysis.strategy_analysis
"""

import asyncio
import sys
import argparse
from .batch import _fetch_all_companies, _run_batch
from .reporting import export_to_excel, print_assessment
from .review import review_business_strategy


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Review business strategy and assess patent expansion incentive."
    )
    parser.add_argument("--company",  default=None, help="Single company name")
    parser.add_argument("--therapy",  default=None, help="Therapy area (e.g. 'GLP-1')")
    parser.add_argument("--drugs",    default=None, help="Comma-separated drug names")
    parser.add_argument("--export",   action="store_true", help="Export to Excel")
    parser.add_argument("--output",   default=None, help="Excel output path")
    parser.add_argument("--limit",    type=int, default=None, help="Batch: max companies")
    parser.add_argument("--dry-run",  action="store_true", help="Batch: list companies only")
    parser.add_argument("--no-bq",    action="store_true", help="Skip BigQuery write")
    args = parser.parse_args()

    drugs = [d.strip() for d in args.drugs.split(",")] if args.drugs else []

    if args.company:
        # ── Single company ────────────────────────────────────────────────────
        assessment = review_business_strategy(
            company      = args.company,
            therapy_area = args.therapy,
            drugs        = drugs,
            write_bq     = not args.no_bq,
        )
        print_assessment(assessment)
        if args.export:
            safe = args.company.lower().replace(" ", "_")
            export_to_excel(assessment, args.output or f"{safe}.xlsx")

    else:
        # ── Batch ─────────────────────────────────────────────────────────────
        companies = _fetch_all_companies(args.therapy or "GLP-1")
        if not companies:
            print("No companies found. Use --company or check BQ_SOURCE_TABLE config.")
            sys.exit(1)
        if args.limit:
            companies = companies[: args.limit]
        if args.dry_run:
            print(f"\n[DRY RUN] {len(companies)} companies:")
            for i, c in enumerate(companies, 1):
                print(f"  {i}. {c}")
            sys.exit(0)

        asyncio.run(_run_batch(companies, args.therapy or "GLP-1", args.export))
