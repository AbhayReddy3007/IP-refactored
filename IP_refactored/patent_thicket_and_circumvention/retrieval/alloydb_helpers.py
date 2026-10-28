"""Patent-chunk retrieval from AlloyDB (collections and client come from chunking)."""

import logging

from ...chunking.indexer import get_or_create_collection

logger = logging.getLogger(__name__)


def _query_collection(collection, filename: str) -> list:
    """All real chunks of *filename* in one collection."""
    # Compound filter: this file's chunks, excluding the index-complete marker.
    try:
        results = collection.get(
            where={"$and": [{"filename": {"$eq": filename}}, {"chunk_index": {"$gte": 0}}]},
            include=["documents", "metadatas"],
        )
        docs = results.get("documents", [])
        if docs:
            return docs
    except Exception:
        logger.debug("Compound filter failed for '%s'", filename, exc_info=True)

    # Filename-only filter (fallback within the same collection).
    try:
        results = collection.get(where={"filename": {"$eq": filename}}, include=["documents", "metadatas"])
        return [
            d for d, m in zip(results.get("documents", []), results.get("metadatas", []))
            if m.get("chunk_index", -1) >= 0
        ]
    except Exception as e:
        logger.warning("Query failed on '%s': %s", collection.name, e)
        return []


def fetch_relevant_chunks(drug: str, source_file: str, sections: list, top_k: int = 12) -> list:
    """
    Chunks of one patent from the drug's AlloyDB collection, ranked by how many of the
    *sections* keywords they contain (the 'Source File' value is the stored 'filename').

    Only the drug's own collection is searched; if the patent is not there an empty list
    is returned (no scan of other collections).
    """
    filename = (source_file or "").strip()
    if not filename:
        logger.warning("Empty source_file passed to fetch_relevant_chunks")
        return []

    try:
        collection = get_or_create_collection(drug)
        docs = _query_collection(collection, filename)
    except Exception as e:
        logger.warning("Could not query the collection of '%s': %s", drug, e)
        return []

    if not docs:
        logger.info("'%s' not found in the collection of '%s' — skipping patent.", filename, drug)
        return []

    logger.info("Found %d chunks in the collection of '%s' for '%s'", len(docs), drug, filename)
    return sorted(docs, key=lambda text: sum(1 for kw in sections if kw in text.lower()), reverse=True)[:top_k]
