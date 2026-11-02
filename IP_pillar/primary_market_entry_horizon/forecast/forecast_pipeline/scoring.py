"""
scoring.py
──────────
Forecast scoring (controlling expiry, years to entry, 1-5 score).
"""

import re as _re
from typing import Dict, List, Optional
import pandas as pd
from .constants import _CURRENT_DATE, _CURRENT_YEAR


# ─────────────────────────────────────────────
# Forecast scoring helpers (from forecast_scorer)
# ─────────────────────────────────────────────

def _extract_lower_year(filing_window: str) -> Optional[int]:
    if not filing_window or str(filing_window).strip().lower() in ("n/a", "nan", "none", ""):
        return None
    years = _re.findall(r'(20\d{2})', str(filing_window))
    return min(int(y) for y in years) if years else None


def _calc_score(avg_yte: float) -> int:
    if avg_yte <= 3:
        return 5
    elif avg_yte <= 5:
        return 4
    elif avg_yte <= 8:
        return 3
    elif avg_yte <= 10:
        return 2
    else:
        return 1


def _score_forecast(drug_name: str, forecast: Dict) -> List[Dict]:
    """
    Scores a drug's forecast entries.
    For each forecasted patent:
      controlling_patent_expiry = lower_filing_year + 20
      years_to_entry = controlling_patent_expiry - current_year
    Then:
      avg_years_to_entry = mean of max(US), max(EP/EU)
      score = 1-5
    """
    current_phase = forecast.get("current_phase", "")

    # Est. Approval Year based on phase
    phase_lower = current_phase.strip().lower()
    if "phase 3" in phase_lower:
        est_approval_year = _CURRENT_YEAR + 3
    elif "phase 2" in phase_lower:
        est_approval_year = _CURRENT_YEAR + 5
    elif "approved" in phase_lower:
        est_approval_year = _CURRENT_YEAR  # already approved
    else:
        est_approval_year = None

    patents = []

    for entry in forecast.get("forecast", []):
        patent_type = entry.get("patent_type", "").strip()
        filing_win  = entry.get("filing_window", "").strip()
        jur         = entry.get("jurisdiction", "").strip()
        likelihood  = entry.get("likelihood", "").strip()

        if not jur or jur.lower() in ("nan", "none", ""):
            continue
        if not likelihood or likelihood.lower() in ("nan", "none", ""):
            continue

        lower_year = _extract_lower_year(filing_win)
        if not lower_year:
            continue

        expiry = lower_year + 20
        yte    = expiry - _CURRENT_YEAR

        # Exclusivity Year
        jur_upper = jur.upper()
        if est_approval_year is not None:
            if jur_upper == "US":
                exclusivity_year = est_approval_year + 5
            elif jur_upper in ("EU", "EP"):
                exclusivity_year = est_approval_year + 10
            else:
                exclusivity_year = None
        else:
            exclusivity_year = None

        est_patents_raw = entry.get("estimated_patents")
        est_patents = None if pd.isna(est_patents_raw) else est_patents_raw

        patents.append({
            "drug_name":                        drug_name,
            "company":                          forecast.get("company", ""),
            "global_phase":                     current_phase,
            "drug_class":                       forecast.get("drug_class", ""),
            "patent_number":                    f"{jur}+Forecasted+{patent_type}",
            "tag":                              "BLOCKING",
            "type":                             "Forecasted",
            "step1_claim_category":             patent_type,
            "jurisdiction":                     jur,
            "phase_in_jurisdiction":            entry.get("drug_phase_in_jurisdiction", ""),
            "likelihood":                       likelihood,
            "filing_window":                    filing_win,
            "filing_date_lower":                lower_year,
            "rationale":                        entry.get("rationale", ""),
            "strategic_purpose":                entry.get("strategic_purpose", ""),
            "controlling_patent_expiry_year":   expiry,
            "years_to_entry":                   yte,
            "est_approval_year":                est_approval_year,
            "exclusivity_year":                 exclusivity_year,
            "no_of_forecasted_patents":         est_patents,
            "overall_forecast":                 forecast.get("overall_forecast", ""),
            "portfolio_gaps":                   " | ".join(forecast.get("portfolio_gaps", [])),
            "risk_assessment":                  forecast.get("risk_assessment", ""),
            "existing_patent_summary":          forecast.get("existing_patent_summary", ""),
        })

    if not patents:
        return []

    # Keep highest YTE row per jurisdiction — blank YTE on all other rows
    best_per_jur = {}
    for p in patents:
        jur = p["jurisdiction"].upper()
        if jur not in best_per_jur or p["years_to_entry"] > best_per_jur[jur]["years_to_entry"]:
            best_per_jur[jur] = p

    for p in patents:
        if p is not best_per_jur.get(p["jurisdiction"].upper()):
            p["years_to_entry"] = None

    # Drug-level score: max YTE per jurisdiction, then avg, then score
    us_yte = best_per_jur.get("US", {}).get("years_to_entry")
    ep_yte = next((best_per_jur[j].get("years_to_entry")
                   for j in ("EP", "EU") if j in best_per_jur), None)

    yte_values = [v for v in [us_yte, ep_yte] if v is not None]
    avg_yte = round(sum(yte_values) / len(yte_values), 2) if yte_values else None
    score   = _calc_score(avg_yte) if avg_yte is not None else None

    # Avg across all jurisdictions
    all_yte = [p["years_to_entry"] for p in patents if p["years_to_entry"] is not None]
    avg_yte_all = round(sum(all_yte) / len(all_yte), 2) if all_yte else None

    for p in patents:
        p["avg_years_to_entry_us_ep"] = avg_yte
        p["avg_years_to_entry"]       = avg_yte_all
        p["ip_dimension_1_score"]     = score
        p["scored_at"]                = _CURRENT_DATE

    print(f"  {drug_name}: {len(forecast.get('forecast', []))} forecasts | "
          f"US max YTE={us_yte} | EP max YTE={ep_yte} | "
          f"Avg={avg_yte} | Score={score}")

    return patents
