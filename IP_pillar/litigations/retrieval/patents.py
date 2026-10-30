"""The drug's patents (glp_master_loe, approved patents only) and their filing dates (glp_patent_loe)."""

import logging
from typing import Dict, List

import pandas as pd

from ...utils import normalize_patent_number

logger = logging.getLogger(__name__)

_BLANK = {"", "nan", "none", "n/a", "nat"}


def get_patent_numbers(master: pd.DataFrame) -> List[str]:
    """Distinct patent numbers of the real (non-forecasted) patents, sorted. Two spellings of one number
    ('US-1,234,567-B2' and 'US1234567B2') count once; the first spelling is kept."""
    patents = master[master["Type"].str.lower() != "forecasted"]["Patent Number"]
    by_number: dict = {}
    for value in patents.dropna().astype(str).str.strip():
        if value.lower() not in _BLANK:
            by_number.setdefault(normalize_patent_number(value), value)
    numbers = sorted(by_number.values())
    logger.info("[LITIGATION] %d distinct patent number(s) in glp_master_loe", len(numbers))
    return numbers


def get_filing_dates(patent_loe: pd.DataFrame) -> Dict[str, str]:
    """{normalised patent number: filing date} from the drug's glp_patent_loe rows (blank dates left out)."""
    if patent_loe.empty or not {"Patent Number", "Filing Date"} <= set(patent_loe.columns):
        return {}
    dates: Dict[str, str] = {}
    for number, filing_date in zip(patent_loe["Patent Number"], patent_loe["Filing Date"]):
        date_text = str(filing_date).strip()
        if pd.notna(number) and pd.notna(filing_date) and date_text.lower() not in _BLANK:
            dates.setdefault(normalize_patent_number(number), date_text)
    return dates
