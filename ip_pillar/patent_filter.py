"""
patent_filter.py
─────────────────
Lists patent PDF filenames from GCS for a given drug name, restricted to
patents BigQuery's patent_master table (config.PATENT_MASTER_TABLE) marks
as approved: patent_status = 'PDF Downloaded' and eval_refined_confidence > 0.3. A PDF
sitting in GCS that isn't approved for this molecule in patent_master is
excluded — so indexing, blocking analysis, and the output tables downstream
all only ever see this approved set. No downloading here — metadata only.

All GCS/BigQuery access goes through ip_pillar.gcp_utils; all
configuration (bucket name, patents prefix, patent_master table) comes
from ip_pillar.config.

Usage:
    from ip_pillar.patent_filter import patent_filter, get_approved_patent_numbers

    refs = patent_filter("BGM-0504")
    # -> [{"filename": "US1234567.pdf", "blob_name": "patents/BGM-0504/US1234567.pdf"}, ...]

    get_approved_patent_numbers("BGM-0504")   # just this molecule
    get_approved_patent_numbers()              # every molecule in patent_master
"""

import logging
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from google.cloud import bigquery

from . import config
from . import gcp_utils
from .utils import normalize, normalize_patent_number

logger = logging.getLogger(__name__)

if config.GCS_BUCKET:
    logger.debug("[PATENT_FILTER] Config loaded: gs://%s/%s/", config.GCS_BUCKET, config.GCS_PATENTS_PREFIX)
else:
    logger.warning("[PATENT_FILTER] GCS_BUCKET not set — patent files cannot be loaded")

# ─────────────────────────────────────────────
# BigQuery — approved patent numbers (patent_master)
# ─────────────────────────────────────────────

PATENT_MASTER_QUERY_BY_MOLECULE = """
SELECT patent_number
FROM `{table}`
WHERE lower(molecule_name) = @molecule_name
AND patent_status = '{status}'
AND {confidence} > {threshold}
"""

PATENT_MASTER_QUERY_ALL = """
SELECT patent_number
FROM `{table}`
WHERE patent_status = '{status}'
AND {confidence} > {threshold}
"""


def get_approved_patent_numbers(molecule_name: Optional[str] = None) -> List[str]:
    """
    Queries config.PATENT_MASTER_TABLE for approved patent numbers:
    patent_status = 'PDF Downloaded' and eval_refined_confidence > 0.3.

    Args:
        molecule_name: If given, restricts to lower(molecule_name) ==
                        molecule_name.lower() (matches the query's own
                        `lower(molecule_name) = ...` filter). If None,
                        returns approved patent numbers for every molecule
                        in the table.

    Returns:
        Sorted list of raw patent_number strings as stored in BigQuery.
        Empty list on query failure or no matches.
    """
    if not config.PATENT_MASTER_TABLE:
        logger.warning("[PATENT_FILTER] PATENT_MASTER_TABLE not set — cannot filter by patent_master")
        return []

    try:
        client = gcp_utils.get_bq_client()
        _q_kwargs = dict(status=config.PATENT_STATUS_FILTER, threshold=config.PATENT_CONFIDENCE_THRESHOLD,
                         confidence=config.PATENT_CONFIDENCE_SQL)
        if molecule_name:
            query = PATENT_MASTER_QUERY_BY_MOLECULE.format(table=config.PATENT_MASTER_TABLE, **_q_kwargs)
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("molecule_name", "STRING", molecule_name.strip().lower()),
                ]
            )
            logger.info("[PATENT_FILTER] Querying %s for molecule '%s'...", config.PATENT_MASTER_TABLE, molecule_name)
            rows = client.query(query, job_config=job_config).result()
        else:
            query = PATENT_MASTER_QUERY_ALL.format(table=config.PATENT_MASTER_TABLE, **_q_kwargs)
            logger.info("[PATENT_FILTER] Querying %s for ALL molecules...", config.PATENT_MASTER_TABLE)
            rows = client.query(query).result()

        patent_numbers = sorted({
            str(row.patent_number).strip()
            for row in rows
            if row.patent_number and str(row.patent_number).strip()
        })
    except Exception as e:
        logger.error("[PATENT_FILTER] patent_master query failed against %s: %s", config.PATENT_MASTER_TABLE, e)
        return []

    logger.info(
        "[PATENT_FILTER] %d approved patent number(s)%s",
        len(patent_numbers), f" for '{molecule_name}'" if molecule_name else " (all molecules)",
    )
    return patent_numbers


# ─────────────────────────────────────────────
# GCS — PDFs for a drug, filtered down to approved patent numbers
# ─────────────────────────────────────────────

_folders_lock = threading.Lock()
_folders_cache: Dict[str, tuple] = {}   # prefix -> (monotonic time, exact map, fuzzy map)


def _drug_folders(prefix: str):
    """({lowercased folder name: folder prefix}, {normalised folder name: folder prefix}) for the drug folders
    directly under *prefix*. One cheap delimiter listing (folder names only, no objects), reused for
    config.PATENT_FOLDERS_CACHE_SECONDS, instead of listing every object under the prefix on every call."""
    with _folders_lock:
        cached = _folders_cache.get(prefix)
        if cached and time.monotonic() - cached[0] < config.PATENT_FOLDERS_CACHE_SECONDS:
            return cached[1], cached[2]

        exact: Dict[str, str] = {}
        fuzzy: Dict[str, str] = {}
        for folder_prefix in sorted(gcp_utils.list_subfolders(prefix)):
            folder_name = folder_prefix[len(prefix):].strip("/")
            if not folder_name:
                continue
            exact.setdefault(folder_name.strip().lower(), folder_prefix)
            fuzzy.setdefault(normalize(folder_name), folder_prefix)
        logger.info("[PATENT_FILTER] %d drug folder(s) under %s", len(exact), prefix)
        _folders_cache[prefix] = (time.monotonic(), exact, fuzzy)
        return exact, fuzzy


def patent_filter(drug_name: str) -> List[Dict]:
    """
    Lists PDF filenames for *drug_name* from GCS, then restricts the result
    to only files whose patent number is approved in patent_master for this
    molecule: patent_status = 'PDF Downloaded' and eval_refined_confidence > 0.3.

    GCS folder matching tries, in order:
      1. Exact match: lower(drug_name) == lower(folder_name).
      2. Only if no exact match exists: fuzzy match — both sides run
         through utils.normalize() (lowercase, spaces/hyphens/underscores
         removed), so folder-naming variants like "BGM 0504" / "BGM_0504"
         / "bgm0504" still resolve to the same folder.

    Matching a BigQuery patent_number to a GCS filename is punctuation/case
    insensitive (e.g. patent_number "US-1,234,567-B2" matches filename
    "US1234567B2.pdf") via utils.normalize_patent_number().

    Args:
        drug_name: Drug name to look up — must match a GCS folder name
                   under gs://{GCS_BUCKET}/{GCS_PATENTS_PREFIX}/ (exact,
                   case-insensitive, or failing that fuzzy), and is also
                   used as molecule_name against patent_master.

    Returns:
        List of {"filename": str, "blob_name": str} dicts, sorted by blob
        name. Empty list if the GCS folder is missing OR patent_master has
        no approved rows for this molecule OR none of the GCS PDFs match an
        approved patent number.
    """
    if not config.GCS_BUCKET:
        logger.warning("[PATENT_FILTER] GCS_BUCKET not set — cannot list patent files")
        return []

    prefix = config.GCS_PATENTS_PREFIX.rstrip("/") + "/"
    drug_lower = drug_name.strip().lower()
    drug_norm = normalize(drug_name)

    logger.info("[PATENT_FILTER] Listing PDFs for '%s' under gs://%s/%s", drug_name, config.GCS_BUCKET, prefix)

    drug_folders_exact, drug_folders_fuzzy = _drug_folders(prefix)
    logger.debug("[PATENT_FILTER] Drug folders found: %s", list(drug_folders_exact.keys()))

    if drug_lower in drug_folders_exact:
        matched_prefix = drug_folders_exact[drug_lower]
        logger.info("[PATENT_FILTER] Exact match on lower(folder_name): %s", matched_prefix)
    elif drug_norm in drug_folders_fuzzy:
        matched_prefix = drug_folders_fuzzy[drug_norm]
        logger.info(
            "[PATENT_FILTER] No exact match for '%s' — fell back to fuzzy match (normalised '%s'): %s",
            drug_name, drug_norm, matched_prefix,
        )
    else:
        logger.warning(
            "[PATENT_FILTER] No folder matching '%s' (exact or fuzzy '%s'). Available: %s",
            drug_name, drug_norm, list(drug_folders_exact.keys()),
        )
        return []

    # Only this drug's folder is listed - not the whole patents prefix (17k+ objects, ~35 s a call).
    folder_blobs = gcp_utils.list_blobs_with_prefix(matched_prefix)
    logger.info("[PATENT_FILTER] Found %d object(s) in %s", len(folder_blobs), matched_prefix)
    pdf_blobs = [
        b for b in folder_blobs
        if b.name.lower().endswith(".pdf")
        and not b.name.endswith("/")
    ]

    if not pdf_blobs:
        logger.warning("[PATENT_FILTER] No PDFs found in gs://%s/%s", config.GCS_BUCKET, matched_prefix)
        return []

    # Restrict to patents approved in patent_master for this molecule.
    approved_numbers = get_approved_patent_numbers(drug_name)
    if not approved_numbers:
        logger.warning(
            "[PATENT_FILTER] No approved patent_master row(s) for '%s' — 0 of %d GCS PDF(s) will be used",
            drug_name, len(pdf_blobs),
        )
        return []
    approved_norm = {normalize_patent_number(p) for p in approved_numbers}

    result: List[Dict] = []
    skipped: List[str] = []
    for b in sorted(pdf_blobs, key=lambda b: b.name):
        filename = Path(b.name).name
        stem_norm = normalize_patent_number(Path(filename).stem)
        if stem_norm in approved_norm:
            result.append({"filename": filename, "blob_name": b.name})
        else:
            skipped.append(filename)

    if skipped:
        logger.info(
            "[PATENT_FILTER] %d GCS PDF(s) skipped (not approved in patent_master for '%s'): %s",
            len(skipped), drug_name, skipped,
        )

    if not result:
        logger.warning(
            "[PATENT_FILTER] 0 of %d GCS PDF(s) matched an approved patent number for '%s'",
            len(pdf_blobs), drug_name,
        )
        return []

    logger.info("[PATENT_FILTER] %d approved PDF(s) for '%s': %s", len(result), drug_name, [r["filename"] for r in result])
    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("Usage: python -m ip_pillar.patent_filter <drug_name>")
        print("       python -m ip_pillar.patent_filter --all   (approved patent numbers, every molecule)")
        sys.exit(1)
    if sys.argv[1] == "--all":
        for pn in get_approved_patent_numbers():
            print(pn)
    else:
        for ref in patent_filter(sys.argv[1]):
            print(ref)
