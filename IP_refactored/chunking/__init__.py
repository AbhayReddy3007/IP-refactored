"""
chunking
────────
Patent fetching + chunking + AlloyDB storage sub-package.

    indexer.py         — fetch -> extract -> chunk -> embed -> store in AlloyDB
                          (indexer(drug_name))
    dates.py           — filing/grant date extraction (Gemini) + stored-date lookup (AlloyDB)
    alloydb_client.py  — AlloyDB (pgvector) storage layer   (alloydb_client())
    utils.py           — shared helpers (normalize, safe_name, chunk_text, clean_date, fmt_elapsed, ...)

Usage:
    from IP_refactored.chunking import indexer, alloydb_client

    client = alloydb_client()
    result = await indexer("Semaglutide")
"""

from .indexer import indexer
from .alloydb_client import alloydb_client

__all__ = ["indexer", "alloydb_client"]
