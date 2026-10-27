"""
drug_list.py
────────────
Resolves the target drug list for a run from BigQuery instead of GCS folder
discovery: queries `vw_drug_details_full` for every GLP-1-class drug (by
target / mechanism of action), then lists each drug's patent PDFs in GCS via
patent_filter() — so a run only ever touches files for drugs in this list.

Drug names are matched case-insensitively (patent_filter() already does
this), but every name this module returns or prints keeps the spelling of
`cleaned_generic_name` (e.g. "Bgm-0504").

All BigQuery / GCS access goes through IP_refactored.gcp_utils; all
configuration (project, dataset) comes from IP_refactored.config.

Usage:
    from IP_refactored.drug_list import drug_list, list_glp1_patent_files

    drugs         = drug_list()        # ["liraglutide", "semaglutide", ...]
    files_by_drug = list_glp1_patent_files()      # {"semaglutide": [{"filename": ..., "blob_name": ...}, ...], ...}

CLI:
    python -m IP_refactored.drug_list
"""

import logging
from typing import Dict, List

from . import config
from . import gcp_utils
from .utils import normalize, normalize_drug_str
from .patent_filter import patent_filter

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# BigQuery — GLP-1 drug discovery query
# ─────────────────────────────────────────────
# The view is addressed by its fully-qualified ID, config.DRUG_LIST_VIEW.

GLP1_DRUG_QUERY = """
SELECT DISTINCT cleaned_generic_name
FROM `{view}`
WHERE (
    UPPER(cleaned_Target) LIKE '%GLUCAGON LIKE PEPTIDE 1%'
    OR UPPER(cleaned_Target) LIKE '%GLP-1%'
    OR UPPER(cleaned_Target) LIKE '%GLUCAGON LIKE PEPTIDE-1%'
    OR (data_source = 'IPD' AND Mechanism_of_Action = 'Glucagon-like peptide-1 (GLP-1) agonist')
)
AND Mechanism_of_Action IS NOT NULL
AND LOWER(Mechanism_of_Action) NOT LIKE '%antagonist%'
"""


def drug_list() -> List[str]:
    """
    Runs the GLP-1 drug-discovery query against BigQuery and returns every
    distinct `cleaned_generic_name` as spelled there, de-duplicated
    case-insensitively, sorted.

    Returns:
        Sorted list of drug names, e.g. ["Bgm-0504", "liraglutide", ...]
        Empty list if the query fails or returns nothing.
    """
    query = GLP1_DRUG_QUERY.format(view=config.DRUG_LIST_VIEW)
    logger.info(
        "[DRUG_LIST] Querying GLP-1 drug list from %s", config.DRUG_LIST_VIEW,
    )

    try:
        client = gcp_utils.get_bq_client()
        rows = client.query(query).result()
    except Exception as e:
        logger.error("[DRUG_LIST] BigQuery query failed: %s", e)
        return []

    # Keep each name's original spelling; de-duplicate case-insensitively
    # (first spelling wins) so "Bgm-0504" and "bgm-0504" don't both appear.
    by_key: Dict[str, str] = {}
    for row in sorted(rows, key=lambda r: str(r["cleaned_generic_name"] or "")):
        raw = row["cleaned_generic_name"]
        if raw and str(raw).strip():
            name = str(raw).strip()
            by_key.setdefault(name.lower(), name)

    drugs = sorted(by_key.values(), key=str.lower)
    logger.info("[DRUG_LIST] %d GLP-1 drug name(s) found: %s", len(drugs), drugs)
    return drugs


# ─────────────────────────────────────────────
# GCS — filter patent_filter() output down to only GLP-1 drugs
# ─────────────────────────────────────────────

_drug_names_cache: tuple = ()


def _drug_list_names() -> tuple:
    """The drug list, fetched once per process. An empty result (failed query)
    is not cached, so a transient error can't leave every name unresolved."""
    global _drug_names_cache
    if not _drug_names_cache:
        _drug_names_cache = tuple(drug_list())
    return _drug_names_cache


def resolve_drug_name(name: str) -> str:
    """The drug's name exactly as the drug list spells it (cleaned_generic_name).

    Every output (BigQuery tables, JSON, reports, GCS folders) is written
    under this one spelling, whatever case / spacing the caller typed. A name
    that is not in the drug list falls back to its stripped form."""
    cleaned = str(name or "").strip()
    for candidate in _drug_list_names():
        if normalize_drug_str(candidate) == normalize_drug_str(cleaned):
            return candidate
    for candidate in _drug_list_names():
        if normalize(candidate) == normalize(cleaned):
            return candidate
    return cleaned


def list_glp1_patent_files() -> Dict[str, List[Dict]]:
    """
    For every GLP-1 drug returned by drug_list(), lists its patent
    PDFs in GCS via patent_filter() (same fuzzy folder-name matching used
    everywhere else). Drugs with no matching GCS folder (no patents indexed
    yet for that drug) are skipped, not errored.

    Returns:
        {drug_name_lowercase: [{"filename": str, "blob_name": str}, ...]}
        Only drugs with at least one PDF are included.
    """
    drugs = drug_list()
    if not drugs:
        logger.warning("[DRUG_LIST] GLP-1 query returned no drug names — nothing to list")
        return {}

    files_by_drug: Dict[str, List[Dict]] = {}
    for drug in drugs:
        refs = patent_filter(drug)
        if refs:
            files_by_drug[drug] = refs
        else:
            logger.info("[DRUG_LIST] No GCS patent folder for '%s' — skipped", drug)

    total_files = sum(len(v) for v in files_by_drug.values())
    logger.info(
        "[DRUG_LIST] %d/%d GLP-1 drug(s) have patents in GCS | %d file(s) total",
        len(files_by_drug), len(drugs), total_files,
    )
    return files_by_drug


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = list_glp1_patent_files()
    if not result:
        print("No GLP-1 drugs with patent files found.")
    for drug, refs in result.items():
        print(f"\n{drug} ({len(refs)} file(s)):")
        for r in refs:
            print(f"  {r['filename']}")