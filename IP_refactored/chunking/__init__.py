"""
chunking
────────
Patent fetching + chunking + AlloyDB storage sub-package.

    patent_filter.py   — list a drug's patent PDFs in GCS   (patent_filter(drug_name))
    indexer.py         — fetch -> extract -> chunk -> embed -> store in AlloyDB
                          (indexer(drug_name))
    alloydb_client.py  — AlloyDB (pgvector) storage layer   (alloydb_client())
    utils.py           — shared helpers (normalize, safe_name, chunk_text, clean_date, ...)

Usage:
    from IP_refactored.chunking import patent_filter, indexer, alloydb_client

    refs   = patent_filter("Semaglutide")
    client = alloydb_client()
    result = await indexer("Semaglutide")
"""

from .patent_filter import patent_filter
from .indexer import indexer
from .alloydb_client import alloydb_client

__all__ = ["patent_filter", "indexer", "alloydb_client"]
