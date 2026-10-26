#!/usr/bin/env python3
"""
run_ip_dimension.py
───────────────────
Command-line entry point for the "IP Dimension" pipeline, on a Linux VM or as
a Cloud Run Job. It decides WHICH drugs to run (a --drug, DRUG_NAME, or every
GLP-1 drug with patents in GCS, sharded across Cloud Run tasks) and runs each
through primary_market_entry_horizon(), which owns the per-drug pipeline:
patents -> blocking analysis -> scores -> glp_patent_loe / glp_master_loe.

Usage:
    python -m IP_refactored.run_ip_dimension --drug Semaglutide
    python -m IP_refactored.run_ip_dimension --drug Semaglutide --reindex
    python -m IP_refactored.run_ip_dimension        # DRUG_NAME if set, else every GLP-1 drug (sharded on Cloud Run)

On Cloud Run Jobs, set DRUG_NAME (one name or a comma-separated list) on the
job instead of --drug; CLOUD_RUN_TASK_INDEX / CLOUD_RUN_TASK_COUNT split the
drug list across tasks automatically.
"""

import argparse
import asyncio
import logging
import time
from typing import List

from . import config
from .drug_list import list_glp1_patent_files
from .primary_market_entry_horizon import primary_market_entry_horizon
from .utils import setup_logging

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Drug discovery + Cloud Run Jobs sharding
# ─────────────────────────────────────────────

def list_glp1_drug_folders() -> List[str]:
    """Every GLP-1 drug (BigQuery drug list) that has approved patent PDFs in GCS."""
    if not config.GCS_BUCKET:
        logger.error("[DISCOVERY] GCS_BUCKET not set — cannot list patent files")
        return []
    drugs = sorted(list_glp1_patent_files())
    logger.info("[DISCOVERY] %d GLP-1 drug(s) with patents: %s", len(drugs), drugs)
    return drugs


def get_my_shard(drugs: List[str]) -> List[str]:
    """This Cloud Run task's share of *drugs* (all of them when run locally)."""
    idx, count = config.CLOUD_RUN_TASK_INDEX, config.CLOUD_RUN_TASK_COUNT
    if count <= 1:
        return drugs
    shard = [d for i, d in enumerate(drugs) if i % count == idx]
    logger.info("[SHARD] Task %d/%d -> %d drug(s): %s", idx + 1, count, len(shard), shard)
    return shard


def parse_drug_name_list(raw: str) -> List[str]:
    """'Semaglutide, Tirzepatide' -> ['Semaglutide', 'Tirzepatide'] (de-duplicated, ordered)."""
    return list(dict.fromkeys(name.strip() for name in (raw or "").split(",") if name.strip()))


def get_target_drugs() -> List[str]:
    """DRUG_NAME (one or more, comma-separated) if set, otherwise every GLP-1
    drug with patent PDFs in GCS."""
    explicit = parse_drug_name_list(config.DRUG_NAME)
    if explicit:
        logger.info("[DISCOVERY] DRUG_NAME set -> %d drug(s): %s", len(explicit), explicit)
        return explicit
    return list_glp1_drug_folders()


# ─────────────────────────────────────────────
# Shard runner (Cloud Run Jobs / multi-drug)
# ─────────────────────────────────────────────

async def run_shard(reindex: bool = False) -> dict:
    """Run primary_market_entry_horizon() for every drug in this task's shard of the target list."""
    drugs = get_target_drugs()
    if not drugs:
        return {"status": "error", "message": "No drugs to process — DRUG_NAME is unset and none were discovered.",
                "results": []}

    shard = get_my_shard(drugs)
    results = []
    for i, drug_name in enumerate(shard, 1):
        logger.info("[PROGRESS] %d/%d: %s", i, len(shard), drug_name)
        try:
            results.append(await primary_market_entry_horizon(drug_name, reindex=reindex))
        except Exception as e:
            logger.exception("[ERROR] %s failed", drug_name)
            results.append({"drug_name": drug_name, "error": str(e), "patents": []})

    failed = [r for r in results if r.get("error")]
    logger.info("[IP DIMENSION] Shard complete — %d succeeded | %d failed", len(results) - len(failed), len(failed))
    return {"status": "complete", "total_drugs": len(shard), "succeeded": len(results) - len(failed),
            "failed": len(failed), "results": results}


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="IP Dimension pipeline (patents -> scores -> BigQuery / JSON)")
    parser.add_argument("--drug", default=None,
                        help="Run one drug, overriding DRUG_NAME and discovery. On Cloud Run, set DRUG_NAME instead.")
    parser.add_argument("--reindex", action="store_true", help="Force re-indexing and re-analysis, ignoring caches.")
    args = parser.parse_args()

    setup_logging()
    t0 = time.time()
    logger.info("IP DIMENSION PIPELINE — task %d/%d", config.CLOUD_RUN_TASK_INDEX + 1, config.CLOUD_RUN_TASK_COUNT)

    if args.drug:
        result = asyncio.run(primary_market_entry_horizon(args.drug, reindex=args.reindex))
        failed = bool(result.get("error"))
        if failed:
            logger.error("[IP DIMENSION] %s", result["error"])
    else:
        result = asyncio.run(run_shard(reindex=args.reindex))
        failed = result["status"] == "error" or result.get("failed", 0) > 0

    elapsed = time.time() - t0
    logger.info("[DONE] Total wall time: %.1fs (%.1f min)", elapsed, elapsed / 60)
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
