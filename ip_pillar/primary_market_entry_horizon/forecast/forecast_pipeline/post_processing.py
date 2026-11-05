"""
post_processing.py
──────────────────
Post-processing of Gemini forecasts.
"""

import re as _re
from typing import Dict
from .constants import _CURRENT_YEAR


# ─────────────────────────────────────────────
# Post-processing helpers
# ─────────────────────────────────────────────

def _fix_filing_window(window: str) -> str:
    """Ensure filing_window starts at or after the current year."""
    if not window or not isinstance(window, str):
        return f"{_CURRENT_YEAR}-{_CURRENT_YEAR + 3}"

    years = [int(y) for y in _re.findall(r"\d{4}", window)]
    if not years:
        return f"{_CURRENT_YEAR}-{_CURRENT_YEAR + 3}"

    if len(years) == 1:
        start = max(years[0], _CURRENT_YEAR)
        return f"{start}-{start + 2}"

    start, end = years[0], years[-1]

    if start < _CURRENT_YEAR:
        span = max(end - start, 1)
        start = _CURRENT_YEAR
        end = start + span

    if end < _CURRENT_YEAR:
        end = _CURRENT_YEAR + 2
    if end <= start:
        end = start + 2

    return f"{start}-{end}"


def _normalize_phase(phase_str: str) -> str:
    """Normalize phase strings to simple format."""
    if not phase_str or not isinstance(phase_str, str):
        return "N/A"

    s = phase_str.strip()
    s_upper = s.upper()

    if "APPROVED" in s_upper or "MARKETED" in s_upper or "LAUNCHED" in s_upper:
        return "Approved"
    if "SUBMIT" in s_upper or "BLA" in s_upper or "NDA" in s_upper or "MAA" in s_upper:
        return "Submitted"
    if "PRECLINICAL" in s_upper or "PRE-CLINICAL" in s_upper or "DISCOVERY" in s_upper:
        return "Preclinical"
    if "DISCONTINUE" in s_upper or "WITHDRAW" in s_upper or "TERMINAT" in s_upper:
        return "Discontinued"
    if "NOT APPLICABLE" in s_upper or "N/A" in s_upper or "UNKNOWN" in s_upper:
        return "N/A"
    if "NOT FILED" in s_upper or "NO FILING" in s_upper:
        return "Not filed"

    m = _re.search(r"(?:phase\s*)(IV|III|II|I|[1-4])(?:\s*/\s*(IV|III|II|I|[1-4]))?",
                    s, _re.IGNORECASE)
    if m:
        roman = {"I": "1", "II": "2", "III": "3", "IV": "4"}
        p1 = m.group(1).upper()
        p1 = roman.get(p1, p1)
        if m.group(2):
            p2 = m.group(2).upper()
            p2 = roman.get(p2, p2)
            return f"Phase {p1}/{p2}"
        sub = _re.search(r"(?:phase\s*(?:IV|III|II|I|[1-4])\s*)([ab])",
                          s, _re.IGNORECASE)
        if sub:
            return f"Phase {p1}{sub.group(1).lower()}"
        return f"Phase {p1}"

    return s[:20]


def _validate_forecast(forecast: Dict) -> Dict:
    """Post-process a forecast to fix common issues."""
    if not forecast or "forecast" not in forecast:
        return forecast

    for entry in forecast["forecast"]:
        entry["filing_window"] = _fix_filing_window(entry.get("filing_window", ""))
        if "drug_phase_in_jurisdiction" in entry:
            entry["drug_phase_in_jurisdiction"] = _normalize_phase(
                entry.get("drug_phase_in_jurisdiction", "")
            )
        try:
            ep = int(entry.get("estimated_patents", 1))
            entry["estimated_patents"] = max(ep, 1)
        except (ValueError, TypeError):
            entry["estimated_patents"] = 1

    if "current_phase" in forecast:
        forecast["current_phase"] = _normalize_phase(forecast.get("current_phase", ""))

    return forecast
