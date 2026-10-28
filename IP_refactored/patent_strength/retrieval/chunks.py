"""
chunks.py
AlloyDB chunk retrieval (vector-search, fetch-once architecture) and
in-memory, sub-factor-specific chunk re-ranking.

fetch_all_chunks_for_patent searches the drug's collection first, then looks
for the file in any other drug's collection (the indexer shares patents
across drugs). Collections and the AlloyDB client come from chunking.
"""

import logging
from functools import lru_cache

from ... import config
from ...chunking.alloydb_client import alloydb_client
from ...chunking.indexer import find_in_any_collection, get_or_create_collection
from ...gemini_utils import gemini_embed
from ..scoring.constants import TOP_K_CHUNKS

logger = logging.getLogger(__name__)

# Broad query so the vector search returns a representative mix across all patent sections.
_COMBINED_QUERY = (
    "independent claim novelty prior art background "
    "prosecution amendment experimental data"
)

# Only real chunks (the index-complete marker row has chunk_index -1).
_CHUNKS_ONLY = {"chunk_index": {"$gte": 0}}


@lru_cache(maxsize=1)
def _query_embedding() -> tuple:
    """Embedding of the combined query, computed once per process (the chunks
    were embedded with the same task type)."""
    vector = gemini_embed([_COMBINED_QUERY], config.GEMINI_EMBEDDING_MODEL, task_type="SEMANTIC_SIMILARITY")[0]
    return tuple(vector)


def _vector_query(collection, filename: str, top_k: int) -> list:
    """Top ``top_k`` chunks of ``filename`` closest to the combined query, in one DB round-trip."""
    results = collection.query(
        query_embeddings=[list(_query_embedding())],
        n_results=top_k,
        where={"$and": [{"filename": {"$eq": filename}}, _CHUNKS_ONLY]},
        include=["documents"],
    )
    seen: set = set()
    docs: list = []
    # collection.query returns list-of-lists (one per query); flatten + dedupe
    for doc_list in results.get("documents", []):
        for doc in doc_list or []:
            if doc and doc not in seen:
                seen.add(doc)
                docs.append(doc)
    return docs[:top_k]


def _filtered_get(collection, filename: str, top_k: int) -> list:
    """Fallback without vector ranking: the first ``top_k`` chunks of ``filename``."""
    try:
        results = collection.get(
            where={"$and": [{"filename": {"$eq": filename}}, _CHUNKS_ONLY]},
            include=["documents", "metadatas"],
        )
        return (results.get("documents", []) or [])[:top_k]
    except Exception:
        logger.debug("Metadata filter failed for '%s'", filename, exc_info=True)
        return []


def _query_collection(collection, filename: str) -> list:
    try:
        docs = _vector_query(collection, filename, TOP_K_CHUNKS)
        if docs:
            return docs
    except Exception:
        logger.debug("Vector query failed for '%s'; falling back to metadata filter", filename, exc_info=True)
    return _filtered_get(collection, filename, TOP_K_CHUNKS)


def fetch_all_chunks_for_patent(drug: str, patent_number: str, source_file: str) -> list:
    """
    Fetch up to TOP_K_CHUNKS chunks for a patent — ONCE per patent, not once per SF.

    The vector search in AlloyDB does the ranking and limiting, so the full
    document is never pulled over the network. Each of the 4 sub-factors then
    re-ranks these chunks in memory using keyword scoring (zero extra DB calls).

    Strategy:
      1. The drug's own collection
      2. Fallback: whichever collection the indexer stored this file in
    """
    filename = source_file.strip()
    if not filename:
        logger.warning("No source file specified for '%s'", patent_number)
        return []

    # 1. Primary: the drug's collection
    try:
        collection = get_or_create_collection(drug)
        docs = _query_collection(collection, filename)
        if docs:
            logger.info("Vector search -> %d chunks from '%s' for '%s'", len(docs), collection.name, filename)
            return docs
    except Exception:
        logger.debug("Primary collection lookup failed for '%s'", drug, exc_info=True)

    logger.info("'%s' not in the collection for '%s' - looking in other collections", filename, drug)

    # 2. Fallback: any other collection holding this file
    try:
        target = find_in_any_collection(filename)
        if target:
            docs = _query_collection(alloydb_client().get_collection(target), filename)
            if docs:
                logger.info("Vector search -> %d chunks from collection '%s' for '%s'", len(docs), target, filename)
                return docs
    except Exception as exc:
        logger.warning("Could not search other collections for '%s': %s", filename, exc)

    logger.warning("No chunks found for '%s' in any collection", filename)
    return []


def rank_chunks_for_sf(all_chunks: list, sections: list, top_k: int = TOP_K_CHUNKS) -> list:
    """
    Re-rank an already-fetched chunk list by sub-factor-specific section keywords.
    Pure in-memory — zero DB calls.
    """

    def relevance_score(text: str) -> int:
        lowered = text.lower()
        return sum(1 for keyword in sections if keyword in lowered)

    return sorted(all_chunks, key=relevance_score, reverse=True)[:top_k]
