"""Per-drug cache of finished circumvention results (GCS, via gcp_utils)."""

import logging

from ... import gcp_utils
from .constants import CHECKPOINT_FILE, CHECKPOINT_SUBFOLDER

logger = logging.getLogger(__name__)


def load_circumvention_cache(drug: str) -> dict:
    """{claim category: circumvention result} saved by an earlier run of the drug ({} if none)."""
    try:
        data = gcp_utils.read_json(CHECKPOINT_SUBFOLDER, CHECKPOINT_FILE, drug_name=drug)
    except Exception as exc:
        logger.warning("[CHECKPOINT] Could not read the cache for '%s' (%s) — starting without one", drug, exc)
        return {}
    cache = (data or {}).get("results_by_category", {})
    if cache:
        logger.info("[CHECKPOINT] %d claim categor(ies) already analysed for '%s'", len(cache), drug)
    return cache


def save_circumvention_cache(drug: str, results_by_category: dict) -> None:
    try:
        gcp_utils.write_json(CHECKPOINT_SUBFOLDER, CHECKPOINT_FILE,
                             {"results_by_category": results_by_category}, drug_name=drug)
    except Exception as exc:
        logger.warning("[CHECKPOINT] GCS write failed for '%s': %s", drug, exc)
