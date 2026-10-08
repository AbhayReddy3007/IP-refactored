#!/usr/bin/env python3
"""
dimension_1.py
───────────────
Cloud Run Jobs entry point for the "IP Dimension 1" (patent blocking /
market-entry-horizon) pipeline.

This is the single orchestrator for the whole IP_refactored package —
it supersedes cog/tools.py's and cog/agent.py's duplicated pipeline logic
(agent.py has been removed entirely; this file is its replacement, without
the Google ADK dependency).

Per-drug pipeline (dimension_1()):
    1. List patent PDFs for the drug                 (chunking.patent_filter)
    2. Fetch, chunk, embed, store them in AlloyDB     (chunking.indexer)
    3. Fetch the drug's clinical development timeline (primary_market_entry_horizon.phase_fetcher)
    4. Run the 5-step blocking analysis                (blocking_analysis)
    5. Assign phase_at_filing to each patent           (primary_market_entry_horizon.phase_fetcher)
    6. First-pass score calculation                    (primary_market_entry_horizon)
    7. Fetch real-world approval dates (Marketed only)  (primary_market_entry_horizon.approval_date_fetcher)
    8. Second-pass score calculation (uses real dates)  (primary_market_entry_horizon)
    9. Export per-drug Excel + regenerate combined Excel to GCS_EXCEL
                                                         (primary_market_entry_horizon.excel_exporter)

Cloud Run Jobs usage:
    Deploy this file's image as a Cloud Run Job. Set env vars (GCS_BUCKET,
    ALLOYDB_*, GOOGLE_API_KEY, etc. — see config.py) in the job's
    configuration, set --tasks to however many parallel workers you want,
    then click Execute. Each task:
      - resolves the target drug list: DRUG_NAME if set (one name, or
        several separated by commas), otherwise every drug folder under
        GCS_PATENTS_PREFIX (full discovery)
      - takes its shard of that list via CLOUD_RUN_TASK_INDEX / CLOUD_RUN_TASK_COUNT
        (Cloud Run Jobs sets these automatically per task — no config needed)
      - runs dimension_1() for each drug in its shard
      - writes each drug's Excel, and the up-to-date combined Excel, to
        gs://{GCS_BUCKET}/{GCS_CACHE_PREFIX}/{GCS_EXCEL}/

    To restrict a run to specific drug(s) on Cloud Run, set the DRUG_NAME
    env var on the job (e.g. "Semaglutide" or "Semaglutide, Tirzepatide")
    instead of passing a CLI flag — Cloud Run Jobs doesn't take CLI args
    per execution the way a local run does.

Local / single-drug usage:
    python -m IP_refactored.dimension_1 --drug Semaglutide
    python -m IP_refactored.dimension_1 --drug Semaglutide --reindex
    python -m IP_refactored.dimension_1              # DRUG_NAME if set, else full sharded run (local: 1 task = all drugs)
"""

import argparse
import asyncio
import time
from datetime import datetime
from typing import Dict, List, Optional

from . import config
from . import gcp_utils
from .chunking.patent_filter import patent_filter
from .chunking.drug_list import list_glp1_patent_files
from .chunking.indexer import indexer as run_indexer, get_or_create_collection
from .blocking_analysis import run_blocking_analysis
from .primary_market_entry_horizon import (
    primary_market_entry_horizon,
    fetch_clinical_timeline,
    assign_patent_phases,
    fetch_approval_dates,
    export_to_excel,
    export_combined_excel,
)

# ─────────────────────────────────────────────
# Drug discovery + Cloud Run Jobs sharding
# ─────────────────────────────────────────────

def list_all_drug_folders() -> List[str]:
    """List every drug folder name under gs://{GCS_BUCKET}/{GCS_PATENTS_PREFIX}/."""
    if not config.GCS_BUCKET:
        print("[DISCOVERY] GCS_BUCKET not set — cannot discover drugs")
        return []

    prefix = config.GCS_PATENTS_PREFIX.rstrip("/") + "/"
    blobs = gcp_utils.list_blobs_with_prefix(prefix)
    prefix_depth = len(prefix.split("/")) - 1

    folders: Dict[str, bool] = {}
    for blob in blobs:
        parts = blob.name.split("/")
        if len(parts) > prefix_depth + 1:
            folders[parts[prefix_depth]] = True

    drugs = sorted(folders.keys())
    print(f"[DISCOVERY] {len(drugs)} drug(s) in gs://{config.GCS_BUCKET}/{prefix}")
    return drugs


def list_glp1_drug_folders() -> List[str]:
    """List every GLP-1 drug (from the BigQuery drug-list query in
    chunking.drug_list) that has at least one patent PDF under
    gs://{GCS_BUCKET}/{GCS_PATENTS_PREFIX}/. Drugs returned by the query with
    no matching GCS folder are excluded here (nothing to index for them)."""
    if not config.GCS_BUCKET:
        print("[DISCOVERY] GCS_BUCKET not set — cannot list patent files")
        return []

    files_by_drug = list_glp1_patent_files()
    drugs = sorted(files_by_drug.keys())
    print(f"[DISCOVERY] {len(drugs)} GLP-1 drug(s) with patents in gs://{config.GCS_BUCKET}/{config.GCS_PATENTS_PREFIX}/: {drugs}")
    return drugs


def get_my_shard(drugs: List[str]) -> List[str]:
    """Split *drugs* across Cloud Run Job tasks via CLOUD_RUN_TASK_INDEX / _COUNT."""
    idx, count = config.CLOUD_RUN_TASK_INDEX, config.CLOUD_RUN_TASK_COUNT
    if count <= 1:
        return drugs
    shard = [d for i, d in enumerate(drugs) if i % count == idx]
    print(f"[SHARD] Task {idx + 1}/{count} -> {len(shard)} drug(s): {shard}")
    return shard


def parse_drug_name_list(raw: str) -> List[str]:
    """Parse DRUG_NAME ("Semaglutide" or "Semaglutide, Tirzepatide, ...")
    into a clean, de-duplicated list of drug names, preserving order."""
    if not raw:
        return []
    seen: Dict[str, bool] = {}
    names: List[str] = []
    for part in raw.split(","):
        name = part.strip()
        if name and name not in seen:
            seen[name] = True
            names.append(name)
    return names


def get_target_drugs() -> List[str]:
    """Resolve the full (pre-shard) list of drugs this run should cover:
    DRUG_NAME (one or more, comma-separated) if set, otherwise every GLP-1
    drug returned by the BigQuery drug-list query (chunking.drug_list) that
    has patent PDFs in GCS."""
    explicit = parse_drug_name_list(config.DRUG_NAME)
    if explicit:
        print(f"[DISCOVERY] DRUG_NAME set -> restricting run to {len(explicit)} drug(s): {explicit}")
        return explicit
    return list_glp1_drug_folders()


# ─────────────────────────────────────────────
# Per-drug pipeline
# ─────────────────────────────────────────────

async def dimension_1(drug_name: str, reindex: bool = False) -> dict:
    """
    Full IP Dimension 1 pipeline for ONE drug: index -> blocking analysis ->
    phase assignment -> score calculation -> approval dates -> recalculation
    -> Excel export to GCS.

    Args:
        drug_name: Drug name (must match a GCS patent folder, fuzzy-matched).
        reindex:   If True, force re-indexing and re-analysis, ignoring caches.

    Returns:
        {
          "drug_name": str, "analysis_date": str, "patents": [...],
          "source_files": [...], "processing_time_seconds": float,
          "excel_path": str|None, "combined_excel_path": str|None,
          "error": str (only present on failure),
        }
    """
    t0 = time.time()
    print(f"\n{'=' * 60}\n[DIMENSION 1] Starting for: {drug_name}\n{'=' * 60}")

    analysis_date = datetime.now().strftime("%Y-%m-%d")

    # ── Step 1: list PDFs ──────────────────────────────────────────────────
    pdf_refs = patent_filter(drug_name)
    if not pdf_refs:
        return {
            "drug_name":     drug_name,
            "error":         f"No PDFs found for '{drug_name}' in GCS bucket '{config.GCS_BUCKET}'.",
            "patents":       [],
            "source_files":  [],
            "analysis_date": analysis_date,
        }

    # ── Step 2: fetch + chunk + embed + store in AlloyDB ───────────────────
    print(f"\n[DIMENSION 1] Indexing {len(pdf_refs)} patent(s)...")
    await run_indexer(drug_name, reindex=reindex)
    collection = get_or_create_collection(drug_name)

    # ── Step 3: clinical timeline (needed for blocking-analysis phase routing) ──
    print(f"\n[DIMENSION 1] Fetching clinical timeline...")
    timeline = await fetch_clinical_timeline(drug_name)
    geography_stages = timeline.get("geography_stages", {})
    drug_phase = {"US": geography_stages.get("United States"), "EP": geography_stages.get("EU")}
    print(f"[DIMENSION 1] Drug phase -> US: {drug_phase['US']} | EP: {drug_phase['EP']}")

    # ── Step 4: blocking analysis (Steps 1-5) ───────────────────────────────
    print(f"\n[DIMENSION 1] Running blocking analysis...")
    patents = await run_blocking_analysis(
        drug_name, pdf_refs, collection, drug_phase=drug_phase, force_reanalyse=reindex,
    )
    print(f"[DIMENSION 1] {len(patents)} patent(s) analysed")

    # ── Step 5: assign phase_at_filing per patent ───────────────────────────
    patents = assign_patent_phases(patents, timeline)

    # ── Step 6: first-pass score calculation ────────────────────────────────
    print(f"\n[DIMENSION 1] Running first-pass score calculation...")
    patents = primary_market_entry_horizon(patents)

    # ── Step 7: real-world approval dates (Marketed jurisdictions only) ────
    bq_companies = [c.strip() for c in str(timeline.get("company_name", "")).split(",") if c.strip()]
    bq_brands    = [b.strip() for b in str(timeline.get("brand_name", "")).split(",") if b.strip()]

    us_marketed = any(
        p.get("phase_at_filing") == "Marketed" and (p.get("jurisdiction") or "").upper() == "US"
        for p in patents
    )
    eu_marketed = any(
        p.get("phase_at_filing") == "Marketed" and (p.get("jurisdiction") or "").upper() == "EP"
        for p in patents
    )
    print(f"[DIMENSION 1] US Marketed: {us_marketed} | EU Marketed: {eu_marketed}")

    approval = await fetch_approval_dates(
        drug_name=drug_name, bq_companies=bq_companies, bq_brands=bq_brands,
        fetch_us=us_marketed, fetch_eu=eu_marketed,
    )
    for p in patents:
        p["approval_date_us"]        = approval["US"]["date"]
        p["approval_date_eu"]        = approval["EU"]["date"]
        p["approval_date_us_source"] = approval["US"]["source"]
        p["approval_date_eu_source"] = approval["EU"]["source"]

    # ── Step 8: recalculate with real approval dates ────────────────────────
    print(f"\n[DIMENSION 1] Recalculating with real approval dates...")
    patents = primary_market_entry_horizon(patents)

    # ── Step 9: export to GCS_EXCEL ─────────────────────────────────────────
    print(f"\n[DIMENSION 1] Exporting Excel to gs://{config.GCS_BUCKET}/.../{config.GCS_EXCEL}/ ...")
    excel_path = export_to_excel(drug_name, patents, analysis_date)
    combined_excel_path = export_combined_excel(analysis_date)

    elapsed = time.time() - t0
    print(f"\n[DIMENSION 1] Done in {elapsed:.1f}s - {len(patents)} patent(s)")
    print(f"[DIMENSION 1] Excel: {excel_path}")
    print(f"[DIMENSION 1] Combined Excel: {combined_excel_path}")

    return {
        "drug_name":               drug_name,
        "analysis_date":           analysis_date,
        "patents":                 patents,
        "source_files":            [ref["filename"] for ref in pdf_refs],
        "processing_time_seconds": round(elapsed, 1),
        "phase_data_source":       timeline.get("source", "unavailable"),
        "excel_path":              excel_path,
        "combined_excel_path":     combined_excel_path,
    }


# ─────────────────────────────────────────────
# Cloud Run Job entry point — processes this task's shard of drugs
# ─────────────────────────────────────────────

async def run_shard(reindex: bool = False) -> dict:
    """Resolves the target drug list (DRUG_NAME if set, else full GCS
    discovery), takes this task's shard, and runs dimension_1() for each —
    the Cloud Run Jobs worker entry point."""
    drugs = get_target_drugs()
    if not drugs:
        return {
            "status": "error",
            "message": "No drugs to process — DRUG_NAME is unset and no drug folders were found under GCS_PATENTS_PREFIX.",
            "results": [],
        }

    shard = get_my_shard(drugs)
    if not shard:
        print("[DIMENSION 1] No drugs in this task's shard. Nothing to do.")
        return {"status": "complete", "total_drugs": 0, "results": []}

    results = []
    for i, drug_name in enumerate(shard, 1):
        print(f"\n[PROGRESS] {i}/{len(shard)}: {drug_name}")
        try:
            result = await dimension_1(drug_name, reindex=reindex)
            results.append(result)
        except Exception as e:
            import traceback
            print(f"[ERROR] {drug_name}: {e}\n{traceback.format_exc()}")
            results.append({"drug_name": drug_name, "error": str(e), "patents": []})

    ok     = [r for r in results if not r.get("error")]
    failed = [r for r in results if r.get("error")]
    print(f"\n[DIMENSION 1] Shard complete — {len(ok)} succeeded | {len(failed)} failed")

    return {"status": "complete", "total_drugs": len(shard), "succeeded": len(ok), "failed": len(failed), "results": results}


# ─────────────────────────────────────────────
# CLI / Cloud Run Jobs entrypoint
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="IP Dimension 1 pipeline (patents -> score -> Excel in GCS)")
    parser.add_argument(
        "--drug", default=None,
        help="Run a single drug, overriding DRUG_NAME and GCS discovery (local/testing use). "
             "On Cloud Run, set the DRUG_NAME env var instead.",
    )
    parser.add_argument("--reindex", action="store_true", help="Force re-indexing and re-analysis, ignoring caches.")
    args = parser.parse_args()

    t0 = time.time()
    idx, count = config.CLOUD_RUN_TASK_INDEX, config.CLOUD_RUN_TASK_COUNT
    print(f"\n{'=' * 60}\n  DIMENSION 1 PIPELINE — task {idx + 1}/{count}\n{'=' * 60}\n")

    if args.drug:
        result = asyncio.run(dimension_1(args.drug, reindex=args.reindex))
        print(f"\nExcel: {result.get('excel_path')}")
        print(f"Combined Excel: {result.get('combined_excel_path')}")
    else:
        result = asyncio.run(run_shard(reindex=args.reindex))

    elapsed = time.time() - t0
    print(f"\n[DONE] Total wall time: {elapsed:.1f}s ({elapsed / 60:.1f} min)")


if __name__ == "__main__":
    main()