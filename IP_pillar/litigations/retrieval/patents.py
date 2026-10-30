"""The drug's patents and their filing dates, from patent_master (confidence above the threshold)."""

import logging
from typing import Dict, List, Tuple

from ... import config, gcp_helpers
from ...utils import normalize_patent_number

logger = logging.getLogger(__name__)

_BLANK = {"", "nan", "none", "n/a", "nat"}

_QUERY = """
SELECT patent_number, CAST(filing_date AS STRING) AS filing_date
FROM `{table}`
WHERE lower(molecule_name) = @molecule_name
AND confidence > {threshold}
"""


def get_patents(drug: str) -> Tuple[List[str], Dict[str, str]]:
    """(patent numbers, {normalised patent number: filing date}) of the drug's rows in config.PATENT_MASTER_TABLE
    with confidence above config.PATENT_CONFIDENCE_THRESHOLD. Numbers are distinct and sorted; two spellings of
    one number ('US-1,234,567-B2' and 'US1234567B2') count once and the first spelling is kept. Blank filing
    dates are left out. A failed read raises, so a transient error can never look like "no patents"."""
    query = _QUERY.format(table=config.PATENT_MASTER_TABLE, threshold=config.PATENT_CONFIDENCE_THRESHOLD)
    df = gcp_helpers.run_query(query, {"molecule_name": drug.strip().lower()})
    by_number: Dict[str, str] = {}
    dates: Dict[str, str] = {}
    for number, filing_date in zip(df["patent_number"], df["filing_date"]):
        value = str(number).strip()
        if value.lower() in _BLANK:
            continue
        key = normalize_patent_number(value)
        by_number.setdefault(key, value)
        date_text = str(filing_date).strip()
        if date_text.lower() not in _BLANK:
            dates.setdefault(key, date_text)
    numbers = sorted(by_number.values())
    logger.info("[LITIGATION] %d distinct patent number(s) in %s with confidence > %s for %r",
                len(numbers), config.PATENT_MASTER_TABLE, config.PATENT_CONFIDENCE_THRESHOLD, drug)
    return numbers, dates
