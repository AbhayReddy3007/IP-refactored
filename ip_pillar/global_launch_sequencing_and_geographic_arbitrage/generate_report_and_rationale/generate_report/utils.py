"""
utils.py
─────────
What the two launch-sequencing reports (generate_dim4_report, generate_pte_report) share: the colour
maps, the jurisdiction names, and the preparation of the shortlisted patents for display.

    prepare_shortlist(df)   the shortlist with the display columns the reports use
    expiry_year(value)      a calendar year as text ('N/A' if there is none)
    score_colour(score)     the fill colour of a 1-5 arbitrage score
    cell(value)             a table cell's text ('—' when blank)
"""

from datetime import date, datetime

import pandas as pd
from reportlab.lib import colors

from ...constants import JURISDICTION_NAMES

GREEN = colors.HexColor("#2ECC71")
AMBER = colors.HexColor("#F39C12")
RED = colors.HexColor("#E74C3C")
NEUTRAL = colors.HexColor("#BDC3C7")

SCORE_COLOUR = {5: GREEN, 4: colors.HexColor("#27AE60"), 3: AMBER, 2: colors.HexColor("#E67E22"), 1: RED}
SIGNAL_HEX = {
    "Immediate opportunity": "2ECC71",
    "Strong arbitrage":      "27AE60",
    "Meaningful arbitrage":  "F39C12",
    "Limited arbitrage":     "E67E22",
    "No arbitrage":          "E74C3C",
    "Reference market":      "3498DB",
}
RATING_HEX = {
    "Very Strong Geographic Arbitrage Opportunity": "2ECC71",
    "Strong Geographic Arbitrage Opportunity":      "27AE60",
    "Moderate Geographic Arbitrage Opportunity":    "F39C12",
    "Limited Geographic Arbitrage Opportunity":     "E67E22",
    "FAIL — No viable geographic arbitrage":        "E74C3C",
}


def jurisdiction_name(code) -> str:
    """'CN' -> 'China'."""
    return JURISDICTION_NAMES.get(str(code), str(code))


def score_colour(score) -> colors.Color:
    try:
        return SCORE_COLOUR.get(int(float(score)), NEUTRAL)
    except (ValueError, TypeError):
        return NEUTRAL


def cell(value) -> str:
    """A table cell's text; '—' for a blank value."""
    return "—" if value is None or str(value) in ("", "nan", "None", "<NA>", "NaT") else str(value)


def expiry_year(value) -> str:
    """The calendar year of a date or a bare year ('2031', 2031.0); 'N/A' if there is none."""
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return str(value.year) if pd.notna(value) else "N/A"
    year = pd.to_numeric(value, errors="coerce")
    if pd.notna(year):
        return str(int(year))
    parsed = pd.to_datetime(value, errors="coerce")
    return str(parsed.year) if pd.notna(parsed) else "N/A"


def _pte_months(row) -> str:
    """PTE months as shown: NA when no PTE applies (not allowed, or overridden by the expiry rules)."""
    status = str(row.get("PTE Status", ""))
    if status.startswith("Overridden") or status == "Not applicable":
        return "NA"
    return cell(row.get("PTE Months (Granted)"))


def prepare_shortlist(shortlisted: pd.DataFrame) -> pd.DataFrame:
    """A copy of the shortlisted patents with the display columns 'PTE (months)' and
    'Controlling Patent Expiry Year' (the year of the jurisdiction's adjusted expiry)."""
    df = shortlisted.copy()
    df["PTE (months)"] = df.apply(_pte_months, axis=1)
    df["Controlling Patent Expiry Year"] = df["Adjusted Expiry (with PTE)"].map(expiry_year)
    df["Filing + 20y Expiry Year"] = df["Base Patent Term (Filing+20y)"].map(expiry_year)
    return df
