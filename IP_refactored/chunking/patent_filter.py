"""
patent_filter.py
─────────────────
Lists patent PDF filenames from GCS for a given drug name. No downloading —
metadata only. (Renamed from gcs_lister.py.)

All GCS access goes through IP_refactored.gcp_utils; all configuration
(bucket name, patents prefix) comes from IP_refactored.config.

Usage:
    from IP_refactored.chunking.patent_filter import patent_filter

    refs = patent_filter("Semaglutide")
    # -> [{"filename": "US1234567.pdf", "blob_name": "patents/Semaglutide/US1234567.pdf"}, ...]
"""

import logging
from pathlib import Path
from typing import Dict, List

from .. import config
from .. import gcp_utils
from .utils import normalize

logger = logging.getLogger(__name__)

if config.GCS_BUCKET:
    logger.debug("[PATENT_FILTER] Config loaded: gs://%s/%s/", config.GCS_BUCKET, config.GCS_PATENTS_PREFIX)
else:
    logger.warning("[PATENT_FILTER] GCS_BUCKET not set — patent files cannot be loaded")


def patent_filter(drug_name: str) -> List[Dict]:
    """
    Lists PDF filenames for *drug_name* from GCS, performing fuzzy folder-name
    matching on the drug name (case / space / hyphen / underscore insensitive).

    Args:
        drug_name: Drug name to look up (must fuzzy-match a GCS folder name
                   under gs://{GCS_BUCKET}/{GCS_PATENTS_PREFIX}/).

    Returns:
        List of {"filename": str, "blob_name": str} dicts, sorted by blob name.
    """
    if not config.GCS_BUCKET:
        logger.warning("[PATENT_FILTER] GCS_BUCKET not set — cannot list patent files")
        return []

    prefix = config.GCS_PATENTS_PREFIX.rstrip("/") + "/"
    drug_norm = normalize(drug_name)

    logger.info("[PATENT_FILTER] Listing PDFs for '%s' under gs://%s/%s", drug_name, config.GCS_BUCKET, prefix)

    all_blobs = gcp_utils.list_blobs_with_prefix(prefix)
    logger.info("[PATENT_FILTER] Found %d total object(s) under prefix", len(all_blobs))

    prefix_depth = len(prefix.split("/")) - 1
    drug_folders: Dict[str, str] = {}
    for blob in all_blobs:
        parts = blob.name.split("/")
        if len(parts) > prefix_depth + 1:
            folder_name = parts[prefix_depth]
            norm = normalize(folder_name)
            if norm not in drug_folders:
                drug_folders[norm] = "/".join(parts[: prefix_depth + 1]) + "/"

    logger.debug("[PATENT_FILTER] Drug folders found: %s", list(drug_folders.keys()))

    if drug_norm not in drug_folders:
        logger.warning(
            "[PATENT_FILTER] No folder matching '%s' (normalised: '%s'). Available: %s",
            drug_name, drug_norm, list(drug_folders.keys()),
        )
        return []

    matched_prefix = drug_folders[drug_norm]
    logger.info("[PATENT_FILTER] Matched folder prefix: %s", matched_prefix)

    pdf_blobs = [
        b for b in all_blobs
        if b.name.startswith(matched_prefix)
        and b.name.lower().endswith(".pdf")
        and not b.name.endswith("/")
    ]

    if not pdf_blobs:
        logger.warning("[PATENT_FILTER] No PDFs found in gs://%s/%s", config.GCS_BUCKET, matched_prefix)
        return []

    result = [
        {"filename": Path(b.name).name, "blob_name": b.name}
        for b in sorted(pdf_blobs, key=lambda b: b.name)
    ]
    logger.info("[PATENT_FILTER] Found %d PDF(s): %s", len(result), [r["filename"] for r in result])
    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    drug = sys.argv[1] if len(sys.argv) > 1 else None
    if not drug:
        print("Usage: python -m IP_refactored.chunking.patent_filter <drug_name>")
        sys.exit(1)
    for ref in patent_filter(drug):
        print(ref)
