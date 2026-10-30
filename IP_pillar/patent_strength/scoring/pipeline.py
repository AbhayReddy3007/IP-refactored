"""
pipeline.py
Orchestration helpers: score-cache loading and concurrent patent scoring.
"""

import asyncio
import logging

import pandas as pd

from ..storage.bq_utils import load_existing_score_cache
from ..storage.checkpoint import checkpoint_key, flush_checkpoint_to_gcs, load_checkpoint
from .constants import ACTIVE_SFS, PATENT_CONCURRENCY
from .scoring import score_patent_async

logger = logging.getLogger(__name__)


def load_score_cache(drug: str, reset: bool = False) -> dict:
    """Load the drug's already-scored results so finished sub-factors are not re-run.

    The GCS checkpoint is the primary cache. Any gaps are filled from
    patent_strength_table in BigQuery, so a lost or corrupted checkpoint does
    not force a full re-score of patents already written out previously.
    """
    if reset:
        return {}

    cache = load_checkpoint(drug)

    added = 0
    for key, entry in load_existing_score_cache(drug).items():
        existing = cache.setdefault(key, {})
        for sf_key, sf_value in entry.items():
            if sf_key not in existing:
                existing[sf_key] = sf_value
                added += 1
    if added:
        logger.info("[BQ FALLBACK] Filled %d sub-factor result(s) missing from the GCS checkpoint", added)
    return cache


async def _process_one(
    idx: int,
    total: int,
    row,
    cache: dict,
    semaphore: asyncio.Semaphore,
    lock: asyncio.Lock,
) -> dict:
    patent_number = str(row.get("Patent Number", "N/A")).strip()
    drug = str(row.get("Drug Name", "N/A")).strip()
    source_file = str(row.get("Source File", "")).strip()
    cached_entry = cache.get(checkpoint_key(drug, patent_number), {})

    logger.info("[%d/%d] %s (%s) - source: '%s'", idx + 1, total, patent_number, drug, source_file)

    if all(sf_key in cached_entry for sf_key in ACTIVE_SFS):
        logger.info("%s | all sub-factors cached - skipping Gemini calls", patent_number)
        return cached_entry

    return await score_patent_async(semaphore, row, cached_entry, ACTIVE_SFS, cache, lock)


async def score_all_patents(df_shortlist: pd.DataFrame, cache: dict, drug: str) -> list[dict]:
    """Score every shortlisted patent concurrently; results follow row order."""
    # The semaphore throttles concurrent Gemini calls only; AlloyDB fetches are
    # I/O-bound and not rate-limited here.
    semaphore = asyncio.Semaphore(PATENT_CONCURRENCY)
    lock = asyncio.Lock()
    total = len(df_shortlist)

    tasks = [
        _process_one(idx, total, row, cache, semaphore, lock)
        for idx, (_, row) in enumerate(df_shortlist.iterrows())
    ]
    try:
        # gather() returns results in task order, matching df_shortlist row order.
        return list(await asyncio.gather(*tasks))
    finally:
        # Persist any remaining checkpoint state, even if scoring was interrupted.
        flush_checkpoint_to_gcs(cache, drug)
