#!/usr/bin/env python3
"""
check_phase.py
───────────────
Local diagnostic script — lives next to dimension_1.py, in the same
IP_refactored/ package. Shows exactly what phase_fetcher.fetch_clinical_timeline()
does for a given drug (or every GLP-1 drug, via --all): the raw rows pulled
from clinical_efficacy and the drug-list/drug_details table, the jurisdiction
tokens each row resolved to, which phase won per jurisdiction and from which
source, the fallback-Excel contribution, and the final merged timeline that
dimension_1.py / assign_patent_phases() would use. Nothing is downloaded or
written anywhere; this only queries and prints.

SETUP — fill in SERVICE_ACCOUNT_JSON_PATH below, then run locally:

    python -m IP_refactored.check_phase "Semaglutide"
    python -m IP_refactored.check_phase --all

Must be run with `-m` from the directory that CONTAINS the IP_refactored/
folder (the same place you'd run `python -m IP_refactored.dimension_1`
from) — not as `python check_phase.py`, because this file uses the
package's relative imports.

Local Python environment needs: google-cloud-bigquery, google-auth, pandas
(the same packages dimension_1.py needs for BigQuery — no GCS/AlloyDB/Gemini
dependency here).
"""

import os

# ─────────────────────────────────────────────
# Hardcoded local settings — EDIT THESE before running
# ─────────────────────────────────────────────

# Path to your service-account JSON key file (used for BigQuery).
SERVICE_ACCOUNT_JSON_PATH = r"/path/to/your/service-account.json"

# Optional overrides — leave as "" to use config.py's built-in defaults /
# whatever is already set in your shell's environment variables.
PROJECT_ID_OVERRIDE               = ""   # e.g. "cognito-prod-394707"
CLINICAL_EFFICACY_TABLE_OVERRIDE  = ""   # e.g. "cognito-dev-380506.data_mart.clinical_efficacy_glp1"
DRUG_DETAILS_TABLE_OVERRIDE       = ""   # e.g. "vw_drug_details_full"

# These MUST be set before importing the package — config.py reads env vars
# exactly once, at import time.
if SERVICE_ACCOUNT_JSON_PATH and SERVICE_ACCOUNT_JSON_PATH != r"/path/to/your/service-account.json":
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = SERVICE_ACCOUNT_JSON_PATH
if PROJECT_ID_OVERRIDE:
    os.environ["PROJECT_ID"] = PROJECT_ID_OVERRIDE
if CLINICAL_EFFICACY_TABLE_OVERRIDE:
    os.environ["CLINICAL_EFFICACY_TABLE"] = CLINICAL_EFFICACY_TABLE_OVERRIDE
if DRUG_DETAILS_TABLE_OVERRIDE:
    os.environ["DRUG_DETAILS_TABLE"] = DRUG_DETAILS_TABLE_OVERRIDE

import asyncio
import sys
from pathlib import Path

from . import config
from .chunking.drug_list import get_glp1_drug_names
from .primary_market_entry_horizon import phase_fetcher as pf


def _print_header():
    print("=" * 78)
    print("PHASE FETCH DIAGNOSTIC")
    print("=" * 78)
    creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or "(none set — will try ADC)"
    print(f"  Service account file      : {creds}")
    print(f"  BigQuery project (default): {config.BQ_PROJECT_ID}")
    print(f"  clinical_efficacy table   : {pf.BQ_TABLE_NAME}")
    clinical_project, clinical_dataset = pf._clinical_project_dataset()
    print(f"  drug_details table        : {pf.BQ_DRUG_DETAILS_TABLE}  "
          f"(looked up in {clinical_project}.{clinical_dataset})")
    print(f"  fallback Excel            : {pf.PHASE_FALLBACK_EXCEL}"
          f"{'  (found)' if pf.PHASE_FALLBACK_EXCEL.exists() else '  (NOT FOUND)'}")
    print("=" * 78)
    if creds != "(none set — will try ADC)" and not Path(creds).exists():
        print(f"  !! WARNING: service account file not found at: {creds}")
        print("=" * 78)


async def check_drug(drug_name: str) -> None:
    """Full trace of fetch_clinical_timeline()'s decision for one drug."""
    print(f"\n{'#' * 78}")
    print(f"# DRUG: {drug_name}")
    print(f"{'#' * 78}")

    canonical = pf.canonicalise_drug_name(drug_name)
    if canonical != drug_name:
        print(f"\n[0] Alias resolved: '{drug_name}' -> '{canonical}'")

    # ── 1. Raw clinical_efficacy rows ───────────────────────────────────────
    print(f"\n[1] clinical_efficacy lookup")
    print(f"    Table: {pf.BQ_TABLE_NAME}")
    print(f"    Match: lower(regexp_replace(molecule_name)) == lower(regexp_replace('{canonical}'))")
    try:
        clinical_df = pf.import_from_gbq(
            drug_name=canonical,
            table_name=pf.BQ_TABLE_NAME,
            project_id=pf.BQ_PROJECT_ID,
            dataset_id=pf.BQ_DATASET_ID,
            service_account_path=pf.BQ_SERVICE_ACCOUNT,
        )
    except Exception as e:
        print(f"    !! Query failed: {e}")
        clinical_df = None

    if clinical_df is None or clinical_df.empty:
        print("    No clinical_efficacy rows found for this drug.")
    else:
        print(f"    {len(clinical_df)} raw row(s):")
        for _, row in clinical_df.iterrows():
            tokens = pf._location_tokens(row.get("trial_location"))
            norm_phase = pf._normalize_phase(row.get("phase"))
            print(f"      molecule_name={row.get('molecule_name')!r}  "
                  f"trial_location={row.get('trial_location')!r} -> tokens={sorted(tokens) or '(none)'}  "
                  f"phase={row.get('phase')!r} -> normalised={norm_phase!r}  "
                  f"phase_status={row.get('phase_status')!r}")

    clin_phases_by_jur, clin_status_by_jur = pf._phases_by_jurisdiction_clinical(
        clinical_df if clinical_df is not None else __import__("pandas").DataFrame()
    )
    clin_overall = pf._overall_phase_clinical(clinical_df) if clinical_df is not None else None
    print(f"\n    Per-jurisdiction phases from clinical_efficacy: {clin_phases_by_jur or '(none)'}")
    print(f"    Overall (location-agnostic) phase from clinical_efficacy: {clin_overall!r}")

    # ── 2. Raw drug_details ("drug list table") rows ───────────────────────
    vwd_project, vwd_dataset = pf._clinical_project_dataset()
    print(f"\n[2] drug_details ('drug list table') lookup")
    print(f"    Table: {pf.BQ_DRUG_DETAILS_TABLE}  (in {vwd_project}.{vwd_dataset})")
    print(f"    Match: lower(regexp_replace(cleaned_generic_name)) == lower(regexp_replace('{canonical}'))")
    try:
        vwd_df = pf._fetch_drug_details_df(
            drug_name=canonical, project_id=vwd_project, dataset_id=vwd_dataset,
            sa_path=pf.BQ_SERVICE_ACCOUNT,
        )
    except Exception as e:
        print(f"    !! Query failed: {e}")
        vwd_df = None

    if vwd_df is None or vwd_df.empty:
        print("    No drug_details rows found for this drug.")
    else:
        phase_col = pf._find_col(vwd_df, ["highest", "development", "stage"]) or pf._find_col(vwd_df, ["development", "stage"])
        geo_col = pf._find_col(vwd_df, ["drug", "geo", "new"]) or pf._find_col(vwd_df, ["drug", "geo"]) or pf._find_col(vwd_df, ["geo"])
        geo_fallback_col = pf._find_col(vwd_df, ["drug", "geography"])
        print(f"    Columns detected: phase={phase_col!r}  geo={geo_col!r}  geo_fallback={geo_fallback_col!r}")
        print(f"    {len(vwd_df)} raw row(s):")
        for _, row in vwd_df.iterrows():
            geo_new = row.get(geo_col) if geo_col else None
            geo_fb = row.get(geo_fallback_col) if geo_fallback_col else None
            tokens = pf._vwd_geo_tokens_combined(geo_new, geo_fb)
            norm_phase = pf._normalize_phase(row.get(phase_col)) if phase_col else None
            print(f"      {geo_col}={geo_new!r}"
                  f"{f'  (fallback {geo_fallback_col}={geo_fb!r})' if geo_fallback_col else ''} "
                  f"-> tokens={sorted(tokens) or '(none)'}  "
                  f"{phase_col}={row.get(phase_col) if phase_col else None!r} -> normalised={norm_phase!r}")

    vwd_phases_by_jur = pf._phases_by_jurisdiction_vwd(vwd_df if vwd_df is not None else __import__("pandas").DataFrame())
    vwd_overall = pf._overall_phase_vwd(vwd_df) if vwd_df is not None else None
    print(f"\n    Per-jurisdiction phases from drug_details: {vwd_phases_by_jur or '(none)'}")
    print(f"    Overall (geography-agnostic) phase from drug_details: {vwd_overall!r}")

    # ── 3. Per-jurisdiction pooled winner (both sources) ────────────────────
    print(f"\n[3] Pooled winner per jurisdiction (highest-priority phase across BOTH sources)")
    all_jurisdictions = sorted(set(clin_phases_by_jur.keys()) | set(vwd_phases_by_jur.keys()))
    if not all_jurisdictions:
        overall_best = pf._highest_phase(clin_overall, vwd_overall)
        print(f"    No jurisdiction resolved from either source.")
        print(f"    Overall fallback -> US/EP = {overall_best!r}")
    else:
        for jur in all_jurisdictions:
            best_phase, source = pf._pick_best_phase_multi(
                clin_phases_by_jur.get(jur, set()), vwd_phases_by_jur.get(jur, set())
            )
            print(f"      {jur}: clinical={sorted(clin_phases_by_jur.get(jur, set())) or '(none)'}  "
                  f"vwd={sorted(vwd_phases_by_jur.get(jur, set())) or '(none)'}  "
                  f"-> {best_phase!r}  (source={source})")

    # ── 4. Fallback Excel contribution ───────────────────────────────────────
    print(f"\n[4] Fallback Excel")
    fallback_df = pf.load_fallback_phase_excel()
    if fallback_df is None:
        print("    Not available.")
    else:
        fb = pf.lookup_fallback_phase(canonical, fallback_df)
        print(f"    US={fb.get('US')!r}  EP={fb.get('EP')!r}")

    # ── 5. Final result — exactly what fetch_clinical_timeline() returns ────
    print(f"\n[5] Final result — what fetch_clinical_timeline('{drug_name}') returns and the "
          f"rest of the pipeline (assign_patent_phases, Excel output) will use")
    timeline = await pf.fetch_clinical_timeline(drug_name=drug_name)
    print(f"    current_stage    : {timeline.get('current_stage')}")
    print(f"    source           : {timeline.get('source')}")
    print(f"    geography_stages : {timeline.get('geography_stages')}")
    print(f"    completed_stages : {timeline.get('completed_stages')}")


async def check_all() -> None:
    """Runs check_drug() for every drug in the GLP-1 drug list (chunking.drug_list)."""
    print("\n[DRUG LIST] Resolving every drug via chunking.drug_list.get_glp1_drug_names() ...")
    drugs = get_glp1_drug_names()
    if not drugs:
        print("No drugs returned by the drug-list query. Nothing to check.")
        return
    print(f"[DRUG LIST] {len(drugs)} drug(s) to check: {drugs}")
    for drug in drugs:
        await check_drug(drug)


if __name__ == "__main__":
    _print_header()

    if len(sys.argv) < 2:
        print("\nUsage:")
        print('  python -m IP_refactored.check_phase "<drug_name>"')
        print("  python -m IP_refactored.check_phase --all")
        sys.exit(1)

    if sys.argv[1] == "--all":
        asyncio.run(check_all())
    else:
        asyncio.run(check_drug(sys.argv[1]))