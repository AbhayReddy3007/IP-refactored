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

All BigQuery / GCS access goes through ip_pillar.gcp_utils; all
configuration (project, dataset) comes from ip_pillar.config.

Usage:
    from ip_pillar.drug_list import drug_list, list_glp1_patent_files

    drugs         = drug_list()        # ["liraglutide", "semaglutide", ...]
    files_by_drug = list_glp1_patent_files()      # {"semaglutide": [{"filename": ..., "blob_name": ...}, ...], ...}

CLI:
    python -m ip_pillar.drug_list                       # list every GLP-1 drug's patent files in GCS
    python -m ip_pillar.drug_list --align               # rewrite Drug Name in the LOE tables to the drug list's spelling
    python -m ip_pillar.drug_list --align --drug Bgm-0504
"""

import argparse
import logging
from typing import Dict, List, Optional

from google.cloud import bigquery

from . import config
from . import gcp_utils
from .utils import bq_safe_name, normalize, normalize_drug_str, setup_logging
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


def list_glp1_patent_files(drugs: Optional[List[str]] = None) -> Dict[str, List[Dict]]:
    """
    For every GLP-1 drug returned by drug_list() (or only for *drugs*, a subset of that list
    such as one Cloud Run task's share), lists its patent
    PDFs in GCS via patent_filter() (same fuzzy folder-name matching used
    everywhere else). Drugs with no matching GCS folder (no patents indexed
    yet for that drug) are skipped, not errored.

    Returns:
        {drug_name_lowercase: [{"filename": str, "blob_name": str}, ...]}
        Only drugs with at least one PDF are included.
    """
    if drugs is None:
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


def list_glp1_drugs_with_patent_master_rows() -> List[str]:
    """
    Every GLP-1 drug (drug_list()) that has at least one patent in patent_master with
    confidence above config.PATENT_CONFIDENCE_THRESHOLD, spelled as the drug list spells it, sorted.
    One BigQuery query, no GCS listing: the discovery for dimensions that read patent_master only.
    Names are matched on lower(molecule_name), as litigations.retrieval.get_patents does.

    A failed patent_master query raises, so a transient error never looks like "no drugs".
    """
    drugs = drug_list()
    if not drugs:
        return []
    query = (f"SELECT DISTINCT LOWER(TRIM(molecule_name)) AS molecule_name FROM `{config.PATENT_MASTER_TABLE}` "
             f"WHERE confidence > {config.PATENT_CONFIDENCE_THRESHOLD} AND molecule_name IS NOT NULL")
    with_patents = {str(row["molecule_name"]) for row in gcp_utils.get_bq_client().query(query).result()}
    found = [drug for drug in drugs if drug.strip().lower() in with_patents]
    logger.info("[DRUG_LIST] %d/%d GLP-1 drug(s) have patents in %s with confidence > %s: %s",
                len(found), len(drugs), config.PATENT_MASTER_TABLE, config.PATENT_CONFIDENCE_THRESHOLD, found)
    return found


# ─────────────────────────────────────────────
# One-off fix — align the spelling of Drug Name in the LOE tables
# ─────────────────────────────────────────────
# Rewrites the `Drug Name` column of glp_patent_loe / glp_master_loe so each drug uses the drug list's spelling
# (cleaned_generic_name, e.g. "Bgm-0504" rather than "bgm-0504"). Only that column is updated — nothing is
# re-indexed or re-analysed. Names are matched ignoring case and all whitespace, like delete_rows.delete_drug_rows().

DRUG_NAME_BQ_COLUMN = bq_safe_name("Drug Name")


def align_drug_name(table_id: str, drug_name: str) -> int:
    """Set Drug Name to *drug_name* on every row of that drug whose spelling
    differs. Returns the number of rows updated."""
    sql = (
        f"UPDATE `{table_id}` SET {DRUG_NAME_BQ_COLUMN} = @drug_name "
        f"WHERE REGEXP_REPLACE(LOWER({DRUG_NAME_BQ_COLUMN}), r'\\s+', '') = REGEXP_REPLACE(LOWER(@drug_name), r'\\s+', '') "
        f"AND {DRUG_NAME_BQ_COLUMN} != @drug_name"
    )
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("drug_name", "STRING", drug_name)]
    )
    job = gcp_utils.get_bq_client().query(sql, job_config=job_config)
    job.result()
    updated = job.num_dml_affected_rows or 0
    if updated:
        logger.info("[ALIGN] %s: %d row(s) renamed to %r", table_id, updated, drug_name)
    return updated


def align_drug_names(only_drug: str = None) -> None:
    """align_drug_name() for every drug in the drug list (or just *only_drug*), on both LOE tables."""
    drugs = drug_list()
    if only_drug:
        wanted = only_drug.strip().lower()
        drugs = [d for d in drugs if d.lower() == wanted] or [only_drug.strip()]
    if not drugs:
        logger.error("[ALIGN] Drug list is empty — nothing to align")
        return

    for table_id in (config.BQ_PATENT_LOE_TABLE, config.BQ_MASTER_LOE_TABLE):
        total = sum(align_drug_name(table_id, drug) for drug in drugs)
        logger.info("[ALIGN] %s: %d row(s) updated in total", table_id, total)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GLP-1 drug list: patent files in GCS, or align Drug Name spelling")
    parser.add_argument("--align", action="store_true",
                        help="Align Drug Name in glp_patent_loe / glp_master_loe with the drug list's spelling.")
    parser.add_argument("--drug", default=None, help="With --align: only this drug (default: every drug in the drug list).")
    args = parser.parse_args()
    setup_logging()

    if args.align:
        align_drug_names(args.drug)
    else:
        result = list_glp1_patent_files()
        if not result:
            print("No GLP-1 drugs with patent files found.")
        for drug, refs in result.items():
            print(f"\n{drug} ({len(refs)} file(s)):")
            for r in refs:
                print(f"  {r['filename']}")
