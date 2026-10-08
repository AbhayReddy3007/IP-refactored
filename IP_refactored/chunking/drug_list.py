"""
drug_list.py
────────────
Resolves the target drug list for a run from BigQuery instead of GCS folder
discovery: queries `vw_drug_details_full` for every GLP-1-class drug (by
target / mechanism of action), then lists each drug's patent PDFs in GCS via
patent_filter() — so a run only ever touches files for drugs in this list.

Drug names are matched case-insensitively (patent_filter() already does
this), but every name this module returns or prints is lower-cased, per
`lower(cleaned_generic_name)`.

All BigQuery / GCS access goes through IP_refactored.gcp_utils; all
configuration (project, dataset) comes from IP_refactored.config.

Usage:
    from IP_refactored.chunking.drug_list import get_glp1_drug_names, list_glp1_patent_files

    drugs         = get_glp1_drug_names()        # ["liraglutide", "semaglutide", ...]
    files_by_drug = list_glp1_patent_files()      # {"semaglutide": [{"filename": ..., "blob_name": ...}, ...], ...}

CLI:
    python -m IP_refactored.chunking.drug_list
"""

import logging
from typing import Dict, List

from .. import config
from .. import gcp_utils
from .patent_filter import patent_filter

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# BigQuery — GLP-1 drug discovery query
# ─────────────────────────────────────────────
# Table is addressed via config.BQ_PROJECT_ID / config.BQ_DATASET_ID (same
# source of truth as every other BigQuery call in this package) rather than
# hardcoding the project/dataset, so it follows PROJECT_ID / BQ_PROJECT_ID
# env var overrides like everything else in config.py.

GLP1_DRUG_VIEW = "vw_drug_details_full"

GLP1_DRUG_QUERY = """
SELECT DISTINCT cleaned_generic_name
FROM `{project}.{dataset}.{view}`
WHERE (
    UPPER(cleaned_Target) LIKE '%GLUCAGON LIKE PEPTIDE 1%'
    OR UPPER(cleaned_Target) LIKE '%GLP-1%'
    OR UPPER(cleaned_Target) LIKE '%GLUCAGON LIKE PEPTIDE-1%'
    OR (data_source = 'IPD' AND Mechanism_of_Action = 'Glucagon-like peptide-1 (GLP-1) agonist')
)
AND Mechanism_of_Action IS NOT NULL
AND LOWER(Mechanism_of_Action) NOT LIKE '%antagonist%'
"""


def get_glp1_drug_names() -> List[str]:
    """
    Runs the GLP-1 drug-discovery query against BigQuery and returns every
    distinct `cleaned_generic_name`, lower-cased and de-duplicated, sorted.

    Returns:
        Sorted list of lower-case drug names, e.g. ["liraglutide", "semaglutide", ...]
        Empty list if the query fails or returns nothing.
    """
    query = GLP1_DRUG_QUERY.format(
        project=config.BQ_PROJECT_ID, dataset=config.BQ_DATASET_ID, view=GLP1_DRUG_VIEW,
    )
    logger.info(
        "[DRUG_LIST] Querying GLP-1 drug list from %s.%s.%s",
        config.BQ_PROJECT_ID, config.BQ_DATASET_ID, GLP1_DRUG_VIEW,
    )

    try:
        client = gcp_utils.get_bq_client()
        rows = client.query(query).result()
    except Exception as e:
        logger.error("[DRUG_LIST] BigQuery query failed: %s", e)
        return []

    names = set()
    for row in rows:
        raw = row["cleaned_generic_name"]
        if raw and str(raw).strip():
            names.add(str(raw).strip().lower())

    drugs = sorted(names)
    logger.info("[DRUG_LIST] %d GLP-1 drug name(s) found: %s", len(drugs), drugs)
    return drugs


# ─────────────────────────────────────────────
# GCS — filter patent_filter() output down to only GLP-1 drugs
# ─────────────────────────────────────────────

def list_glp1_patent_files() -> Dict[str, List[Dict]]:
    """
    For every GLP-1 drug returned by get_glp1_drug_names(), lists its patent
    PDFs in GCS via patent_filter() (same fuzzy folder-name matching used
    everywhere else). Drugs with no matching GCS folder (no patents indexed
    yet for that drug) are skipped, not errored.

    Returns:
        {drug_name_lowercase: [{"filename": str, "blob_name": str}, ...]}
        Only drugs with at least one PDF are included.
    """
    drugs = get_glp1_drug_names()
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