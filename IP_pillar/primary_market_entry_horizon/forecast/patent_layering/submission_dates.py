"""
submission_dates.py
───────────────────
Regulatory submission dates via Gemini Search, cached in GCS.
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional
import pandas as pd
from .... import gcp_utils
from ....gemini_utils import gemini_generate_async
from .constants import _GEMINI_MODEL


# ─────────────────────────────────────────────
# Regulatory submission dates — via Gemini Search
# ─────────────────────────────────────────────
#
# These dates change rarely (an NDA/BLA/MAA filing date is a one-time
# historical event) but are expensive to fetch — each lookup is a
# Gemini-with-Google-Search call per drug. With Cloud Run sharding the
# pipeline across multiple tasks, every task was independently fetching
# the same dates for the same drugs, multiplying cost and time by the
# task count. We cache results in GCS so the first task that resolves
# a drug's date persists it, and every subsequent task (this run or any
# future run) reads it back instead of re-querying Gemini.
#
# Cache layout:
#   <GCS_CACHE_PREFIX>/regulatory_submissions/<drug_slug>/submission.json
# Contents:
#   {"drug": "<original>", "date": "YYYY-MM-DD" or null, "fetched_at": "..."}
#
# A null `date` is also cached — that's a real lookup result ("Gemini
# couldn't find a submission") and we don't want to retry it forever.
# If you ever need to force a refresh, delete the cache subfolder via
# gcs_cache.delete_prefix("regulatory_submissions").

_SUBMISSION_CACHE_SUBFOLDER = "regulatory_submissions"
_SUBMISSION_CACHE_FILENAME  = "submission.json"

_submission_cache = gcp_utils


def _submission_cache_get(drug_name: str):
    """Return (hit: bool, date_or_none).

    hit=True means we have a cached answer for this drug (which may
    itself be None — meaning "we asked and found nothing").
    """
    if _submission_cache is None:
        return False, None
    try:
        cached = _submission_cache.read_json(
            _SUBMISSION_CACHE_SUBFOLDER,
            _SUBMISSION_CACHE_FILENAME,
            drug_name=drug_name,
        )
    except Exception as e:
        print(f"[SUBMISSION CACHE] read failed for '{drug_name}': {e}")
        return False, None
    if cached is None:
        return False, None
    return True, cached.get("date")


def _submission_cache_put(drug_name: str, date_str):
    if _submission_cache is None:
        return
    payload = {
        "drug": drug_name,
        "date": date_str,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        _submission_cache.write_json(
            _SUBMISSION_CACHE_SUBFOLDER,
            _SUBMISSION_CACHE_FILENAME,
            payload,
            drug_name=drug_name,
        )
    except Exception as e:
        print(f"[SUBMISSION CACHE] write failed for '{drug_name}': {e}")


async def _fetch_regulatory_submission_date(drug_name: str) -> Optional[str]:
    """Uses Gemini web search to find the regulatory submission (NDA/BLA/MAA) date for a drug.

    Cached in GCS so the same drug isn't looked up more than once across
    parallel tasks or repeated runs. Returns the cached value (even if it
    is None) when present; otherwise fetches, caches, and returns.
    """
    hit, cached_date = _submission_cache_get(drug_name)
    if hit:
        print(f"[SUBMISSION] '{drug_name}': {cached_date or 'None'} (cache hit)")
        return cached_date

    prompt = f"""What is the earliest regulatory submission date (NDA, BLA, or MAA filing date) for the drug "{drug_name}"?

Search the web and return ONLY the date in YYYY-MM-DD format. If you cannot find an exact date, return your best estimate.
If no submission has been made, return "None".

Examples of expected output: "2016-12-05", "2021-09-15", "None"
Return ONLY the date string, nothing else.
"""
    try:
        response = await gemini_generate_async(
            prompt, _GEMINI_MODEL, google_search=True, temperature=0.1,
        )
        raw = (response.text or "").strip().strip(".")
        if raw.lower() in ("none", "n/a", "unknown", ""):
            _submission_cache_put(drug_name, None)
            return None
        # Validate it looks like a date
        pd.to_datetime(raw, errors="raise")
        print(f"[SUBMISSION] '{drug_name}': {raw} (fresh)")
        _submission_cache_put(drug_name, raw)
        return raw
    except Exception as e:
        print(f"[SUBMISSION] Failed for '{drug_name}': {e}")
        # Don't cache transient failures — let the next task retry.
        return None


async def _fetch_all_submission_dates(drug_names: List[str]) -> Dict[str, str]:
    """Fetches regulatory submission dates for all marketed drugs via Gemini Search."""
    results = {}
    for drug in drug_names:
        date_str = await _fetch_regulatory_submission_date(drug)
        if date_str:
            results[drug.lower()] = date_str
    return results
