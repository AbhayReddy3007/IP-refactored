"""
checkpoint.py
GCS-backed scoring checkpoint (one file per drug, via gcp_utils) with throttled writes.
"""

import logging
import time

from ... import gcp_utils
from ..scoring.constants import CHECKPOINT_GCS_FILE, CHECKPOINT_SUBFOLDER, MIN_GCS_WRITE_INTERVAL

logger = logging.getLogger(__name__)

_last_gcs_write_time = 0.0
_checkpoint_dirty = False  # True if the in-memory cache has unsaved GCS changes


def checkpoint_key(drug: str, patent_number: str) -> str:
    """Cache key identifying one drug/patent pair."""
    return f"{drug.strip()}|{patent_number.strip()}"


def load_checkpoint(drug: str) -> dict:
    """Load the drug's checkpoint from GCS ({} if there is none)."""
    try:
        data = gcp_utils.read_json(CHECKPOINT_SUBFOLDER, CHECKPOINT_GCS_FILE, drug_name=drug)
        if data is not None:
            logger.info("Loaded GCS checkpoint for '%s': %d patents already scored", drug, len(data))
            return data
    except Exception as exc:
        logger.warning("GCS checkpoint read failed for '%s' (%s) - starting without one", drug, exc)
    return {}


def _gcs_write_with_retry(cache: dict, drug: str, max_retries: int = 4, base_backoff: float = 2.0) -> bool:
    """Write the checkpoint to GCS with exponential backoff (handles 429s)."""
    for attempt in range(1, max_retries + 1):
        try:
            gcp_utils.write_json(CHECKPOINT_SUBFOLDER, CHECKPOINT_GCS_FILE, cache, drug_name=drug)
            return True
        except Exception as exc:
            if attempt >= max_retries:
                logger.error("GCS checkpoint write failed after %d attempts: %s", max_retries, exc)
                return False
            wait = base_backoff * (2 ** (attempt - 1))
            if "429" in str(exc):
                logger.warning(
                    "GCS 429 rate limit, retrying in %ss (attempt %d/%d)", wait, attempt, max_retries
                )
            else:
                logger.warning("GCS checkpoint write failed (%s), retrying in %ss", exc, wait)
            time.sleep(wait)
    return False


def save_checkpoint(cache: dict, drug: str) -> None:
    """Save the checkpoint, throttling GCS writes to avoid 429 rate limits (at most
    once every MIN_GCS_WRITE_INTERVAL seconds). ``flush_checkpoint_to_gcs`` at the
    end of scoring ensures GCS receives the complete state."""
    global _last_gcs_write_time, _checkpoint_dirty
    _checkpoint_dirty = True

    now = time.time()
    if now - _last_gcs_write_time >= MIN_GCS_WRITE_INTERVAL and _gcs_write_with_retry(cache, drug):
        _last_gcs_write_time = now
        _checkpoint_dirty = False


def flush_checkpoint_to_gcs(cache: dict, drug: str) -> None:
    """Force-write the final checkpoint state to GCS. Call at end of scoring."""
    global _checkpoint_dirty, _last_gcs_write_time
    if _checkpoint_dirty and _gcs_write_with_retry(cache, drug):
        _checkpoint_dirty = False
        _last_gcs_write_time = time.time()
        logger.info("Final checkpoint state flushed to GCS")
