"""
runner.py
─────────
Forecast + score one drug; run the pipeline for a list of drugs.
"""

from typing import Dict, List, Optional, Tuple
import pandas as pd
from .bq_loaders import _load_all_step4_from_bq, _load_all_step5_from_bq, _load_drug_company_map, _load_step23_from_bq
from .bq_writer import _write_to_bq
from .constants import BQ_FORECAST_S3_TABLE, BQ_OUTPUT_TABLE, _CURRENT_DATE
from .forecast_generation import _generate_drug_forecast
from .scoring import _score_forecast
from .web_search import _search_company_analysis, _search_company_for_drug, _search_innovator_patterns


# ─────────────────────────────────────────────
# Main runner
# ─────────────────────────────────────────────

async def forecast_and_score_drug(
    drug_name: str,
    company_name: str,
    patent_df: Optional[pd.DataFrame],
    innovator_patterns: str,
    company_analysis: str,
) -> Tuple[List[Dict], Optional[str]]:
    """Generate one drug's patent forecast with Gemini and score it.
    Returns (scored forecasted_loe rows, None), or ([], the error) if the forecast or scoring fails."""
    print(f"\n  Generating patent forecast...")
    try:
        forecast = await _generate_drug_forecast(
            drug_name=drug_name,
            company_name=company_name,
            patent_data=patent_df,
            innovator_patterns=innovator_patterns,
            company_analysis=company_analysis,
        )
        if not forecast or "error" in forecast:
            err = (forecast or {}).get("error", "No data")
            print(f"  Forecast failed: {err}")
            return [], err

        print(f"\n  Global Phase: {forecast.get('current_phase', '?')}")
        print(f"  Class: {forecast.get('drug_class', '?')}")
        print(f"  Forecasted patents: {len(forecast.get('forecast', []))}")
        for entry in forecast.get("forecast", [])[:5]:
            print(f"    • [{entry.get('likelihood','?')}] {entry.get('patent_type','?')} "
                  f"[{entry.get('jurisdiction','')}] ({entry.get('filing_window','')}) "
                  f"~{entry.get('estimated_patents', '?')} patents")
        if len(forecast.get("forecast", [])) > 5:
            print(f"    ... and {len(forecast['forecast']) - 5} more entries")

        print(f"\n  Scoring forecast...")
        scored = _score_forecast(drug_name, forecast)
        if not scored:
            print(f"  No scoreable entries")
            return [], "No scoreable entries"
        return scored, None

    except Exception as e:
        print(f"  [ERROR] {drug_name}: {e}")
        return [], str(e)


async def run_pipeline(drug_names: List[str]):
    """Runs the full forecast + scoring pipeline for all specified drugs."""

    print(f"\n{'═'*80}")
    print(f"  STEP 6 FORECAST + SCORING PIPELINE")
    print(f"  Date: {_CURRENT_DATE}")
    print(f"{'═'*80}\n")

    # ── Load all inputs from BQ ──────────────────────────────────────────────
    print("[PIPELINE] Loading inputs from BigQuery...\n")

    drug_company_map = _load_drug_company_map()
    forecasting_data = _load_step23_from_bq()
    step4_data       = _load_all_step4_from_bq()
    step5_data       = _load_all_step5_from_bq()

    if not forecasting_data:
        print(f"[PIPELINE] No Step 2/3 data found in {BQ_FORECAST_S3_TABLE}")
        print(f"[PIPELINE] Proceeding with web search fallback for patent data")

    # If no drug list provided, use drugs from Step 2/3
    if not drug_names:
        drug_names = sorted(forecasting_data.keys())
        if not drug_names:
            # Try drug-company map
            drug_names = sorted(set(
                info.get("company", k).title() if k else k
                for k, info in drug_company_map.items()
            ))

    if not drug_names:
        print("[PIPELINE] No drugs found. Provide --drug or populate the BQ tables.")
        return

    print(f"\n[PIPELINE] Drugs to process: {len(drug_names)}")
    for i, d in enumerate(drug_names, 1):
        print(f"  {i}. {d}")

    # ── Process each drug ────────────────────────────────────────────────────
    total = len(drug_names)
    all_scored = []
    succeeded = 0
    failed = []

    for i, drug_name in enumerate(drug_names, 1):
        print(f"\n{'█'*80}")
        print(f"  [{i}/{total}] Forecasting & Scoring: {drug_name}")
        print(f"{'█'*80}")

        # Find company and geographies
        drug_info = drug_company_map.get(drug_name.lower(), {})
        if not drug_info:
            drug_norm = drug_name.lower().replace(" ", "").replace("-", "")
            for k, v in drug_company_map.items():
                if drug_norm in k.replace(" ", "").replace("-", ""):
                    drug_info = v
                    break
        company_name = drug_info.get("company", "") if isinstance(drug_info, dict) else ""
        geographies  = drug_info.get("geographies", []) if isinstance(drug_info, dict) else []

        # Web search fallback for company
        if not company_name:
            print(f"  Company not found in BQ — searching web...")
            company_name = await _search_company_for_drug(drug_name)

        print(f"  Company: {company_name or 'Unknown'}")
        if geographies:
            print(f"  Geographies: {', '.join(geographies)}")

        # Load patent data (Step 2/3 from BQ)
        patent_df = None
        for sheet_name, df in forecasting_data.items():
            if sheet_name.lower().strip() == drug_name.lower().strip():
                patent_df = df
                break
        if patent_df is None:
            drug_norm = drug_name.lower().replace(" ", "").replace("-", "")
            for sheet_name, df in forecasting_data.items():
                if drug_norm in sheet_name.lower().replace(" ", "").replace("-", ""):
                    patent_df = df
                    break
        if patent_df is not None:
            print(f"  Step 2/3 (BQ): {len(patent_df)} rows")
        else:
            print(f"  Step 2/3: not found in BQ")

        # Load Step 4 (innovator patterns from BQ)
        innovator_patterns = ""
        drug_lower = drug_name.lower()
        if drug_lower in step4_data:
            innovator_patterns = step4_data[drug_lower]
            print(f"  Step 4 (BQ): {len(innovator_patterns)} chars")
        else:
            # Try fuzzy match
            for k, v in step4_data.items():
                if drug_lower.replace(" ", "").replace("-", "") in k.replace(" ", "").replace("-", ""):
                    innovator_patterns = v
                    print(f"  Step 4 (BQ fuzzy): {len(innovator_patterns)} chars")
                    break

        # Load Step 5 (company analysis from BQ)
        company_analysis = ""
        if company_name:
            comp_lower = company_name.lower()
            if comp_lower in step5_data:
                company_analysis = step5_data[comp_lower]
                print(f"  Step 5 (BQ): {len(company_analysis)} chars")
            else:
                for k, v in step5_data.items():
                    if comp_lower in k or k in comp_lower:
                        company_analysis = v
                        print(f"  Step 5 (BQ fuzzy): {len(company_analysis)} chars")
                        break

        # Web search fallbacks for missing data
        if not innovator_patterns:
            print(f"  Step 4 missing — searching web...")
            innovator_patterns = await _search_innovator_patterns(drug_name, company_name)

        if not company_analysis:
            print(f"  Step 5 missing — searching web...")
            company_analysis = await _search_company_analysis(company_name, drug_name)

        # ── Generate forecast and score it ───────────────────────────────────
        scored, error = await forecast_and_score_drug(
            drug_name, company_name, patent_df, innovator_patterns, company_analysis)
        if error:
            failed.append((drug_name, error))
        else:
            all_scored.extend(scored)
            succeeded += 1

    # ── Write all scored results to BigQuery ─────────────────────────────────
    if all_scored:
        print(f"\n{'═'*80}")
        print(f"  WRITING SCORED RESULTS TO BIGQUERY")
        print(f"{'═'*80}")
        _write_to_bq(all_scored)

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n{'═'*80}")
    print(f"  PIPELINE COMPLETE — Forecast + Scoring")
    print(f"{'═'*80}")
    print(f"  Drugs:     {total}")
    print(f"  Succeeded: {succeeded}")
    print(f"  Failed:    {len(failed)}")
    print(f"  Output:    {BQ_OUTPUT_TABLE}")

    if succeeded:
        print(f"\n  {'Drug Name':<30} {'Score':<8} {'Avg YTE':<12} {'Patents'}")
        print(f"  {'─'*65}")
        seen = set()
        for p in all_scored:
            drug = p["drug_name"]
            if drug in seen:
                continue
            seen.add(drug)
            print(f"  {drug:<30} {p['ip_dimension_1_score']:<8} "
                  f"{p['avg_years_to_entry_us_ep']:<12} "
                  f"{len([x for x in all_scored if x['drug_name'] == drug])}")

    if failed:
        print(f"\n  Failed:")
        for drug, err in failed:
            print(f"    - {drug}: {err}")

    print(f"{'═'*80}\n")

    return all_scored
