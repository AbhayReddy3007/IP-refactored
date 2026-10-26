"""
delete_rows.py
──────────────
Deleting operations on the LOE output tables (glp_patent_loe / glp_master_loe).

    delete_drug_rows(table_id, drug_name)   delete every row of one drug from a table
    delete_low_confidence_rows(table_id, drug_name=None)
                                            delete patent rows whose patent_master confidence is
                                            <= config.PATENT_CONFIDENCE_THRESHOLD (0.3)

primary_market_entry_horizon.bq_utils.write_table_to_bq() calls this before appending a drug's new rows, so a
re-run replaces the drug's rows instead of duplicating them, and other drugs'
rows are never touched. The drug name is matched ignoring case and all spaces,
so rows written under an older spelling ('Semaglutide', 'semaglutide') are
cleared too.

The LOE tables carry no confidence column, so delete_low_confidence_rows()
looks it up in config.PATENT_MASTER_TABLE (read only — nothing is deleted
there) and removes the output rows for those patents, matching patent numbers
ignoring punctuation and case. Run it as a one-off sweep:

    python -m IP_refactored.delete_rows [--drug Semaglutide]

The BigQuery DELETE itself goes through gcp_helpers.delete_rows.
"""

import argparse
import logging
from typing import Optional

from . import config, gcp_helpers
from .utils import bq_safe_name, setup_logging

logger = logging.getLogger(__name__)

DRUG_NAME_BQ_COLUMN = bq_safe_name("Drug Name")
PATENT_NUMBER_BQ_COLUMN = bq_safe_name("Patent Number")


def delete_drug_rows(table_id: str, drug_name: str) -> int:
    """Delete *drug_name*'s rows from the BigQuery table *table_id*.
    Returns the number of rows deleted (0 if the table doesn't exist yet)."""
    deleted = gcp_helpers.delete_rows(
        table_id,
        f"REGEXP_REPLACE(LOWER({DRUG_NAME_BQ_COLUMN}), r'\\s+', '') = REGEXP_REPLACE(LOWER(@drug_name), r'\\s+', '')",
        {"drug_name": drug_name},
    )
    logger.info("[DELETE] %s: deleted %d row(s) for %r", table_id, deleted, drug_name)
    return deleted


def delete_low_confidence_rows(table_id: str, drug_name: Optional[str] = None) -> int:
    """Delete rows from *table_id* whose patent has confidence <=
    config.PATENT_CONFIDENCE_THRESHOLD in config.PATENT_MASTER_TABLE — one
    drug's rows, or every drug's when *drug_name* is None.
    Returns the number of rows deleted (0 if the table doesn't exist yet)."""
    def clean_number(col: str) -> str:
        return f"REGEXP_REPLACE(UPPER(CAST({col} AS STRING)), r'[^A-Z0-9]', '')"

    def same_drug(col: str) -> str:
        return f"REGEXP_REPLACE(LOWER({col}), r'\\s+', '') = REGEXP_REPLACE(LOWER(@drug_name), r'\\s+', '')"

    molecule_filter = f" AND {same_drug('molecule_name')}" if drug_name else ""
    low_confidence = (
        f"SELECT {clean_number('patent_number')} FROM `{config.PATENT_MASTER_TABLE}` "
        f"WHERE confidence <= {config.PATENT_CONFIDENCE_THRESHOLD}{molecule_filter}"
    )
    where = f"{clean_number(PATENT_NUMBER_BQ_COLUMN)} IN ({low_confidence})"
    params = None
    if drug_name:
        where += f" AND {same_drug(DRUG_NAME_BQ_COLUMN)}"
        params = {"drug_name": drug_name}
    deleted = gcp_helpers.delete_rows(table_id, where, params)
    logger.info("[DELETE] %s: deleted %d low-confidence row(s)%s", table_id, deleted,
                f" for {drug_name!r}" if drug_name else "")
    return deleted


def main() -> None:
    parser = argparse.ArgumentParser(description="Delete low-confidence patent rows from the LOE output tables")
    parser.add_argument("--drug", default=None, help="Only this drug's rows (default: every drug).")
    args = parser.parse_args()
    setup_logging()
    for table_id in (config.BQ_PATENT_LOE_TABLE, config.BQ_MASTER_LOE_TABLE):
        delete_low_confidence_rows(table_id, args.drug)


if __name__ == "__main__":
    main()
