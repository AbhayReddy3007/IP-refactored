"""
blocking_analysis
──────────────────
Multi-step patent blocking analysis, split into one module per step plus an
orchestrator. The public API is re-exported here so callers can do:

    from IP_refactored.blocking_analysis import run_blocking_analysis, load_formulation_excel

Shared GCP calls (Gemini client, GCS cache) and config live in the parent
IP_refactored package (gcp_utils.py / config.py); jurisdiction extraction is
shared with IP_refactored.chunking.
"""

from ..chunking.utils import extract_jurisdiction
from .step1_claim_classification import CLAIM_CATEGORIES
from .step2_claim_elements import get_drug_rows, load_formulation_excel
from .orchestrator import (
    error_result,
    invalidate_drug_cache,
    invalidate_patent_cache,
    is_non_analysable_patent,
    load_cached_patent_analysis,
    load_cached_patents_bulk,
    run_blocking_analysis,
    skipped_result,
    store_patent_analysis,
)

__all__ = [
    "CLAIM_CATEGORIES",
    "error_result",
    "extract_jurisdiction",
    "get_drug_rows",
    "invalidate_drug_cache",
    "invalidate_patent_cache",
    "is_non_analysable_patent",
    "load_cached_patent_analysis",
    "load_cached_patents_bulk",
    "load_formulation_excel",
    "run_blocking_analysis",
    "skipped_result",
    "store_patent_analysis",
]
