"""Whether a drug is Marketed (decides if its brand names are looked up)."""

from ...primary_market_entry_horizon.bq_utils import fetch_drug_details_df
from ...primary_market_entry_horizon.score_calculation.merge_forecasted_loe import is_marketed
from ...primary_market_entry_horizon.score_calculation.phase_fetcher import canonicalise_drug_name


def is_marketed_drug(drug: str) -> bool:
    """True if the drug is Marketed in any geography (from the drug details view)."""
    return is_marketed(fetch_drug_details_df(canonicalise_drug_name(drug)))
