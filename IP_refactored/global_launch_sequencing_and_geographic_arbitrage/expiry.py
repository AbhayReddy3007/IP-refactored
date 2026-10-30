"""Patent expiry calculations."""
import logging
from datetime import date

import pandas as pd
from dateutil.relativedelta import relativedelta

from .. import config
from ..utils import year_from_value
from .constants import PTE_RULES

logger = logging.getLogger(__name__)


def compute_expiry(filing_date, approval_date_str: str, pte_months: int, jurisdiction: str) -> dict:
    result = {"base_expiry": "Not found", "adjusted_expiry": "Not found", "pte_capped": False}

    if pd.isna(filing_date):
        return result

    base = filing_date.date() + relativedelta(years=20)
    result["base_expiry"] = base

    if pte_months == 0:
        result["adjusted_expiry"] = base
        return result

    rules = PTE_RULES.get(jurisdiction, {})
    adjusted = base + relativedelta(months=pte_months)

    max_eff = rules.get("max_effective_from_approval_years")
    if max_eff and approval_date_str and approval_date_str != "Not found":
        approval_dt = pd.to_datetime(approval_date_str, errors="coerce")
        if pd.notna(approval_dt):
            cap = approval_dt.date() + relativedelta(years=max_eff)
            if adjusted > cap:
                adjusted = cap
                result["pte_capped"] = True

    result["adjusted_expiry"] = adjusted
    return result


def compute_expiry_gap(adjusted_expiry) -> int | str:
    """Fallback calculation for non-US rows."""
    exp = pd.to_datetime(adjusted_expiry, errors="coerce")
    if pd.isna(exp):
        return "N/A"
    return exp.year - date.today().year


def normalize_year_only(val):
    """A parseable date string; bare years like 2025 / 2025.0 become YYYY-01-01.
    Used for the Grant Date and Filing Date columns."""
    if pd.isna(val):
        return val
    text = str(val).strip()
    try:
        number = float(text)
        if number == int(number):
            return f"{int(number)}-01-01"
    except (ValueError, TypeError):
        pass
    return text


def compute_expiries(shortlisted: pd.DataFrame) -> None:
    """Add the base (filing + 20 years) and PTE-adjusted expiry columns, and the cap flag, in place."""
    shortlisted["Filing Date"] = shortlisted["Filing Date"].apply(normalize_year_only)
    filing_dates = pd.to_datetime(shortlisted["Filing Date"], errors="coerce")
    base_expiries, adjusted_expiries, capped_flags = [], [], []

    for idx, row in shortlisted.iterrows():
        expiry = compute_expiry(
            filing_dates.iloc[idx],
            row.get("Approval Date", "Not found"),
            int(row.get("PTE Months (Granted)", 0) or 0),
            row["Jurisdiction"],
        )
        base_expiries.append(expiry["base_expiry"])
        adjusted_expiries.append(expiry["adjusted_expiry"])
        capped_flags.append(expiry["pte_capped"])

    shortlisted["Base Patent Term (Filing+20y)"] = base_expiries
    shortlisted["Adjusted Expiry (with PTE)"] = adjusted_expiries
    shortlisted["PTE Capped (Post-Approval Cap)"] = capped_flags


def apply_type_overrides(shortlisted: pd.DataFrame, filtered_df: pd.DataFrame) -> None:
    """A jurisdiction with a non-forecasted patent expires at the base term; one with only forecasted
    patents expires in its filing year + config.PATENT_TERM_YEARS (20), the same rule the master table uses
    for forecasted rows (the current year if there is no usable filing year)."""
    if "Type" not in filtered_df.columns:
        logger.warning("[LAUNCH] 'Type' not found in the master rows — skipping the Type-based expiry override")
        return

    types = filtered_df["Type"].astype(str).str.strip()
    real = filtered_df[(types.str.lower() != "forecasted") & (types != "")]
    has_real_patent = set(zip(real["Drug Name"], real["Jurisdiction"]))

    current_year = date(date.today().year, 1, 1)
    to_base = to_forecast_term = to_current_year = 0
    for idx, row in shortlisted.iterrows():
        if (row["Drug Name"], row["Jurisdiction"]) in has_real_patent:
            shortlisted.at[idx, "Adjusted Expiry (with PTE)"] = row["Base Patent Term (Filing+20y)"]
            shortlisted.at[idx, "PTE Status"] = "Overridden (non-forecasted patent exists)"
            to_base += 1
        else:
            filing_year = year_from_value(row["Filing Date"])
            if filing_year is None:
                shortlisted.at[idx, "Adjusted Expiry (with PTE)"] = current_year
                to_current_year += 1
            else:
                shortlisted.at[idx, "Adjusted Expiry (with PTE)"] = date(int(filing_year) + config.PATENT_TERM_YEARS, 1, 1)
                to_forecast_term += 1
            shortlisted.at[idx, "PTE Status"] = "Overridden (all forecasted)"
        shortlisted.at[idx, "PTE Months (Granted)"] = 0
        shortlisted.at[idx, "PTE Capped (Post-Approval Cap)"] = False
    logger.info("[LAUNCH] Type-based expiry overrides: %d to the base term, %d to filing year + %d (forecasted), "
                "%d to the current year (no filing year)", to_base, to_forecast_term, config.PATENT_TERM_YEARS, to_current_year)


def longest_expiry_per_jurisdiction(shortlisted: pd.DataFrame) -> pd.DataFrame:
    """One row per drug and jurisdiction: the patent that expires last."""
    shortlisted["Adjusted Expiry (with PTE)"] = pd.to_datetime(shortlisted["Adjusted Expiry (with PTE)"], errors="coerce")
    deduped = (
        shortlisted
        .sort_values("Adjusted Expiry (with PTE)", ascending=False, na_position="last")
        .drop_duplicates(subset=["Drug Name", "Jurisdiction"], keep="first")
        .sort_values(["Drug Name", "Jurisdiction"])
        .reset_index(drop=True)
    )
    logger.info("[LAUNCH] After de-duplication: %d row(s) (removed %d)", len(deduped), len(shortlisted) - len(deduped))
    return deduped


def add_expiry_gap(shortlisted: pd.DataFrame, years_to_entry: dict) -> None:
    """Add 'Expiry Gap (Years)' in place: the master table's Years to Entry for the US patent, else
    the years from now to the adjusted expiry."""
    def gap(row):
        known = years_to_entry.get((row["Drug Name"], row["Patent Number"])) if row["Jurisdiction"] == "US" else None
        return known if known is not None else compute_expiry_gap(row["Adjusted Expiry (with PTE)"])

    shortlisted["Expiry Gap (Years)"] = shortlisted.apply(gap, axis=1)
