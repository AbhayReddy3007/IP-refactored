"""
align_drug_names.py
───────────────────
One-off fix: rewrite the `Drug Name` column of the LOE output tables
(glp_patent_loe / glp_master_loe) so each drug uses the drug list's spelling
(cleaned_generic_name, e.g. "Bgm-0504" rather than "bgm-0504").

Only that column is updated — nothing is re-indexed or re-analysed. Names are
matched ignoring case and all whitespace, like delete_rows.delete_drug_rows().

    python -m IP_refactored.align_drug_names             # every drug in the drug list
    python -m IP_refactored.align_drug_names --drug Bgm-0504
"""

import argparse
import logging

from google.cloud import bigquery

from . import config, gcp_utils
from .drug_list import drug_list
from .utils import bq_safe_name, setup_logging

logger = logging.getLogger(__name__)

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


def main() -> None:
    parser = argparse.ArgumentParser(description="Align Drug Name spelling in the LOE tables with the drug list")
    parser.add_argument("--drug", default=None, help="Only this drug (default: every drug in the drug list).")
    args = parser.parse_args()
    setup_logging()

    drugs = drug_list()
    if args.drug:
        wanted = args.drug.strip().lower()
        drugs = [d for d in drugs if d.lower() == wanted] or [args.drug.strip()]
    if not drugs:
        logger.error("[ALIGN] Drug list is empty — nothing to align")
        return

    for table_id in (config.BQ_PATENT_LOE_TABLE, config.BQ_MASTER_LOE_TABLE):
        total = sum(align_drug_name(table_id, drug) for drug in drugs)
        logger.info("[ALIGN] %s: %d row(s) updated in total", table_id, total)


if __name__ == "__main__":
    main()
