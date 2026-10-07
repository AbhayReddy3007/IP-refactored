"""
primary_market_entry_horizon.py
────────────────────────────────
All derived score/metric calculations applied to a drug's patent list.
(Renamed and consolidated from calculators.py.)

  1. Estimated approval date      — current date + phase offset
  2. Exclusivity date             — approval date + jurisdiction offset (US +5yr, EP +10yr)
  3. Controlling patent expiry    — filing date + 20 years (+ PTE if applicable)
  4. Years to entry               — max(expiry_date, exclusivity_date) - today
  5. Pediatric exclusivity        — +6 months to US exclusivity if flag set
  6. Score                        — 1-5 based on avg years to entry across ALL jurisdictions
  7. IP Dimension 1 Score         — 1-5 based on avg years to entry across US + EP only

Pure logic — no I/O, no GCS/BigQuery/Gemini calls. Operates entirely on
patent dicts already held in memory (produced by blocking_analysis).

All dates are stored as YYYY-MM-DD strings. Year-only values are still
supported for backward compatibility (stored alongside the date fields).

Usage:
    from IP_refactored.primary_market_entry_horizon import primary_market_entry_horizon

    patents = primary_market_entry_horizon(patents)
"""

import re
from datetime import datetime
from typing import Dict, List, Optional

from dateutil.relativedelta import relativedelta

# ─────────────────────────────────────────────
# Shared constants
# ─────────────────────────────────────────────

_TIMELINE_STAGES = [
    "Preclinical", "Phase 1", "Phase 2", "Phase 3", "Pre-registration", "Marketed"
]
_STAGE_RANK: Dict[str, int] = {s: i for i, s in enumerate(_TIMELINE_STAGES)}


# ─────────────────────────────────────────────
# Date parsing helpers
# ─────────────────────────────────────────────

def _parse_date(date_str) -> Optional[datetime]:
    """Parse various date formats into a datetime object.
    Handles: YYYY-MM-DD, DD-MM-YYYY, DD/MM/YYYY, DD-Mon-YYYY, YYYY, etc."""
    if date_str is None:
        return None
    s = str(date_str).strip()
    if not s or s.lower() in ("unknown", "n/a", "nan", "none", "nat", "<na>"):
        return None

    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%Y/%m/%d",
                "%m-%d-%Y", "%m/%d/%Y", "%d %b %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue

    # Year-only (e.g. "2026")
    match = re.search(r'((?:19|20)\d{2})', s)
    if match:
        return datetime(int(match.group(1)), 1, 1)

    return None


def _format_date(dt: Optional[datetime]) -> Optional[str]:
    """Format datetime as YYYY-MM-DD string."""
    return dt.strftime("%Y-%m-%d") if dt else None


# ─────────────────────────────────────────────
# PTE-adjusted effective filing year
# ─────────────────────────────────────────────

def effective_filing_year(patent: Dict) -> Optional[float]:
    """Returns the PTE-adjusted effective filing year for a patent."""
    filing_date = patent.get("filing_date")
    if not filing_date:
        return None
    dt = _parse_date(filing_date)
    if not dt:
        return None

    filing_year = dt.year + (dt.month - 1) / 12 + (dt.day - 1) / 365.25

    pte_months = patent.get("pte")
    if pte_months:
        try:
            filing_year += float(pte_months) / 12
        except (ValueError, TypeError):
            pass

    return filing_year


def effective_filing_date(patent: Dict) -> Optional[datetime]:
    """Returns the actual filing date as a datetime (no PTE adjustment)."""
    return _parse_date(patent.get("filing_date"))


def patent_expiry_date(patent: Dict) -> Optional[datetime]:
    """Returns filing date + 20 years + PTE months as exact date."""
    fd = _parse_date(patent.get("filing_date"))
    if not fd:
        return None
    expiry = fd + relativedelta(years=20)
    pte_months = patent.get("pte")
    if pte_months:
        try:
            expiry += relativedelta(months=int(float(pte_months)))
        except (ValueError, TypeError):
            pass
    return expiry


# ─────────────────────────────────────────────
# 1. Estimated approval year
# ─────────────────────────────────────────────

def assign_estimated_approval_year(patents: List[Dict]) -> List[Dict]:
    """
    Sets estimated_approval_year AND estimated_approval_date on the single
    latest BLOCKING patent per jurisdiction.
    """
    print("[APPROVAL YEAR] Calculating estimated approval date...")

    # Phase + Trial Status → years to approval (from clinical development timeline)
    _PHASE_TRIAL_OFFSET = {
        ("Marketed", ""):                        0,
        ("Pre-registration", ""):                0.5,
        ("Phase 3", "Recruiting"):               3,
        ("Phase 3", "Active, Not Recruiting"):   2,
        ("Phase 3", "Primary Completion Reached"): 1,
        ("Phase 3", "Study Completed"):          0.75,
        ("Phase 2", "Recruiting"):               5,
        ("Phase 2", "Active, Not Recruiting"):   4,
        ("Phase 2", "Primary Completion Reached"): 3,
        ("Phase 2", "Study Completed"):          2.5,
    }
    # Fallback: phase-only offsets (when trial_status is unknown)
    _PHASE_OFFSET = {"Phase 2": 5, "Phase 3": 3, "Pre-registration": 0.5, "Marketed": 0}
    today = datetime.now()

    for p in patents:
        p["estimated_approval_year"] = None
        p["estimated_approval_date"] = None

    all_jurisdictions = sorted(set(
        (p.get("jurisdiction") or "").upper() for p in patents
        if p.get("jurisdiction")
    ))

    for jurisdiction in all_jurisdictions:
        candidates = [
            p for p in patents
            if p.get("tag") == "BLOCKING"
            and p.get("filing_date")
            and (p.get("jurisdiction") or "").upper() == jurisdiction
            and effective_filing_year(p) is not None
        ]

        if not candidates:
            print(f"[APPROVAL YEAR] No BLOCKING {jurisdiction} patents with filing dates.")
            continue

        latest = max(candidates, key=effective_filing_year)
        phase  = latest.get("phase_at_filing")
        trial_status = latest.get("trial_status", "")

        # Try phase+trial_status combo first, then fall back to phase-only
        offset = _PHASE_TRIAL_OFFSET.get((phase, trial_status))
        source = f"{phase} + {trial_status}" if trial_status else phase
        if offset is None:
            offset = _PHASE_OFFSET.get(phase)
            source = f"{phase} (no trial status)"

        if offset is not None:
            approval_date = today + relativedelta(years=int(offset), months=int((offset % 1) * 12))
            latest["estimated_approval_year"] = approval_date.year
            latest["estimated_approval_date"] = _format_date(approval_date)
            print(
                f"[APPROVAL YEAR] {latest.get('patent_number')} | {jurisdiction} | "
                f"{source} | +{offset}yr → {latest['estimated_approval_date']}"
            )
        else:
            print(
                f"[APPROVAL YEAR] {latest.get('patent_number')} | {jurisdiction} | "
                f"Phase: {phase} — no offset defined"
            )

    return patents


# ─────────────────────────────────────────────
# 2. Exclusivity year
# ─────────────────────────────────────────────

def assign_exclusivity_year(patents: List[Dict]) -> List[Dict]:
    """
    Sets exclusivity_year AND exclusivity_date on the single latest BLOCKING
    patent per jurisdiction.
    """
    print("[EXCLUSIVITY] Calculating exclusivity date...")

    _JURISDICTION_OFFSET = {"US": 5, "EP": 10}
    _DEFAULT_OFFSET = 8

    for p in patents:
        p["exclusivity_year"] = None
        p["exclusivity_date"] = None

    all_jurisdictions = sorted(set(
        (p.get("jurisdiction") or "").upper() for p in patents
        if p.get("jurisdiction")
    ))

    for jurisdiction in all_jurisdictions:
        offset = _JURISDICTION_OFFSET.get(jurisdiction, _DEFAULT_OFFSET)

        candidates = [
            p for p in patents
            if p.get("tag") == "BLOCKING"
            and p.get("filing_date")
            and (p.get("jurisdiction") or "").upper() == jurisdiction
            and effective_filing_year(p) is not None
        ]

        if not candidates:
            print(f"[EXCLUSIVITY] No BLOCKING {jurisdiction} patents with filing dates.")
            continue

        latest = max(candidates, key=effective_filing_year)

        # Try real approval date first
        real_date = (
            latest.get("approval_date_us") if jurisdiction == "US"
            else latest.get("approval_date_eu") if jurisdiction in ("EP", "EU")
            else latest.get(f"approval_date_{jurisdiction.lower()}")
        )

        base_date = None

        if real_date and str(real_date).lower() not in ("none", "null", "n/a", ""):
            base_date = _parse_date(real_date)
            if base_date:
                print(
                    f"[EXCLUSIVITY] {latest.get('patent_number')} | {jurisdiction} | "
                    f"Real approval date: {_format_date(base_date)}"
                )

        if base_date is None and latest.get("estimated_approval_date"):
            base_date = _parse_date(latest["estimated_approval_date"])
            if base_date:
                print(
                    f"[EXCLUSIVITY] {latest.get('patent_number')} | {jurisdiction} | "
                    f"Using estimated approval date: {_format_date(base_date)}"
                )

        if base_date is None and latest.get("estimated_approval_year"):
            yr = latest["estimated_approval_year"]
            base_date = datetime(int(yr), 1, 1)

        if base_date is not None:
            excl_date = base_date + relativedelta(years=offset)
            latest["exclusivity_year"] = excl_date.year
            latest["exclusivity_date"] = _format_date(excl_date)
            print(
                f"[EXCLUSIVITY] {latest.get('patent_number')} | {jurisdiction} | "
                f"{_format_date(base_date)} + {offset}yr → {latest['exclusivity_date']}"
            )
        else:
            print(
                f"[EXCLUSIVITY] {latest.get('patent_number')} | {jurisdiction} | "
                f"No base date available — skipping"
            )

    return patents


# ─────────────────────────────────────────────
# 3. Controlling patent expiry year
# ─────────────────────────────────────────────

def assign_controlling_patent_expiry_year(patents: List[Dict]) -> List[Dict]:
    """
    Sets controlling_patent_expiry_year AND controlling_patent_expiry_date
    on the single latest BLOCKING patent per jurisdiction.

    Formula: expiry_date = filing_date + 20 years + PTE months
    """
    print("[CONTROLLING EXPIRY] Calculating controlling patent expiry date...")

    for p in patents:
        p["controlling_patent_expiry_year"] = None
        p["controlling_patent_expiry_date"] = None

    all_jurisdictions = sorted(set(
        (p.get("jurisdiction") or "").upper() for p in patents
        if p.get("jurisdiction")
    ))

    for jurisdiction in all_jurisdictions:
        candidates = [
            p for p in patents
            if p.get("tag") == "BLOCKING"
            and p.get("filing_date")
            and (p.get("jurisdiction") or "").upper() == jurisdiction
            and effective_filing_year(p) is not None
        ]

        if not candidates:
            print(f"[CONTROLLING EXPIRY] No BLOCKING {jurisdiction} patents with filing dates.")
            continue

        latest = max(candidates, key=effective_filing_year)
        expiry_dt = patent_expiry_date(latest)

        if expiry_dt:
            latest["controlling_patent_expiry_year"] = expiry_dt.year
            latest["controlling_patent_expiry_date"] = _format_date(expiry_dt)
            pte_note = ""
            if latest.get("pte"):
                pte_note = f" + PTE {latest['pte']}mo"
            print(
                f"[CONTROLLING EXPIRY] {latest.get('patent_number')} | {jurisdiction} | "
                f"Filed: {latest['filing_date']}{pte_note} → Expiry: {latest['controlling_patent_expiry_date']}"
            )
        else:
            print(
                f"[CONTROLLING EXPIRY] {latest.get('patent_number')} | {jurisdiction} | "
                f"Cannot compute expiry — filing date: {latest.get('filing_date')}"
            )

    return patents


# ─────────────────────────────────────────────
# 4. Years to entry
# ─────────────────────────────────────────────

def assign_years_to_entry(patents: List[Dict]) -> List[Dict]:
    """
    Sets years_to_entry on each patent using exact dates where available.

    Formula:
      entry_date = max(controlling_patent_expiry_date, exclusivity_date)
      years_to_entry = (entry_date - today) in years (decimal)

    Falls back to year-based calculation if dates unavailable.
    """
    print("[YEARS TO ENTRY] Calculating years to entry...")

    today = datetime.now()

    for p in patents:
        # Try date-based calculation first
        controlling_dt = _parse_date(p.get("controlling_patent_expiry_date"))
        exclusivity_dt = _parse_date(p.get("exclusivity_date"))
        dates = [d for d in [controlling_dt, exclusivity_dt] if d is not None]

        if dates:
            latest_dt = max(dates)
            p["years_to_entry"] = round((latest_dt - today).days / 365.25, 1)
            p["entry_date"] = _format_date(latest_dt)
            print(
                f"[YEARS TO ENTRY] {p.get('patent_number')} | "
                f"Entry date: {p['entry_date']} → {p['years_to_entry']} years"
            )
        else:
            # Fallback to year-based
            controlling = p.get("controlling_patent_expiry_year")
            exclusivity = p.get("exclusivity_year")
            candidates  = [v for v in [controlling, exclusivity] if v is not None]

            if candidates:
                p["years_to_entry"] = max(candidates) - today.year
                print(
                    f"[YEARS TO ENTRY] {p.get('patent_number')} | "
                    f"max({controlling}, {exclusivity}) - {today.year} = {p['years_to_entry']} (year-based)"
                )
            else:
                p["years_to_entry"] = None

    return patents


# ─────────────────────────────────────────────
# 5. Pediatric exclusivity adjustment
# ─────────────────────────────────────────────

def apply_pediatric_exclusivity(patents: List[Dict]) -> List[Dict]:
    """US only: adds 6 months to exclusivity date if pediatric_exclusivity is True."""
    print("[PEDIATRIC] Applying pediatric exclusivity adjustments...")

    today = datetime.now()

    for p in patents:
        if (p.get("jurisdiction") or "").upper() != "US":
            continue
        if not p.get("pediatric_exclusivity"):
            continue
        if p.get("exclusivity_date") is None and p.get("exclusivity_year") is None:
            continue

        # Adjust date
        if p.get("exclusivity_date"):
            excl_dt = _parse_date(p["exclusivity_date"])
            if excl_dt:
                new_dt = excl_dt + relativedelta(months=6)
                p["exclusivity_date"] = _format_date(new_dt)
                p["exclusivity_year"] = new_dt.year
                print(
                    f"[PEDIATRIC] {p.get('patent_number')} | US | "
                    f"Exclusivity: {_format_date(excl_dt)} + 6mo → {p['exclusivity_date']}"
                )
        elif p.get("exclusivity_year"):
            original = p["exclusivity_year"]
            p["exclusivity_year"] = original + 0.5
            print(
                f"[PEDIATRIC] {p.get('patent_number')} | US | "
                f"Exclusivity year: {original} + 0.5 → {p['exclusivity_year']}"
            )

        # Recalculate years to entry
        controlling_dt = _parse_date(p.get("controlling_patent_expiry_date"))
        exclusivity_dt = _parse_date(p.get("exclusivity_date"))
        latest_dt = max(filter(None, [controlling_dt, exclusivity_dt]), default=None)
        if latest_dt:
            p["years_to_entry"] = round((latest_dt - today).days / 365.25, 1)

    return patents


# ─────────────────────────────────────────────
# Scoring helper
# ─────────────────────────────────────────────

def _avg_to_score(avg: float) -> int:
    """
    Converts average years to entry into a 1-5 score.

    Score | Avg years to entry
      5   | <= 6 years
      4   | 7-8 years
      3   | 9-11 years
      2   | 12-13 years
      1   | > 13 years
    """
    if avg <= 6:
        return 5
    elif avg <= 8:
        return 4
    elif avg <= 11:
        return 3
    elif avg <= 13:
        return 2
    else:
        return 1


# ─────────────────────────────────────────────
# 6. Score (all jurisdictions)
# ─────────────────────────────────────────────

def assign_score(patents: List[Dict]) -> List[Dict]:
    """
    Calculates avg_years_to_entry and score across ALL jurisdictions.

    Steps:
      1. Collect years_to_entry from each jurisdiction's blocking patent.
      2. avg_years_to_entry = mean of available values.
      3. Score (1-5) based on avg_years_to_entry.
      4. Both values are assigned to ALL patents (drug-level metric).
    """
    print("[SCORE] Calculating avg years to entry and score...")

    for p in patents:
        p["avg_years_to_entry"] = None
        p["score"]              = None

    yte_values = []
    all_jurisdictions = sorted(set(
        (p.get("jurisdiction") or "").upper() for p in patents
        if p.get("jurisdiction")
    ))

    for jurisdiction in all_jurisdictions:
        match = next(
            (
                p for p in patents
                if (p.get("jurisdiction") or "").upper() == jurisdiction
                and p.get("years_to_entry") is not None
            ),
            None,
        )
        if match:
            yte_values.append(match["years_to_entry"])
            print(
                f"[SCORE] {jurisdiction} years_to_entry: "
                f"{match['years_to_entry']} (from {match.get('patent_number')})"
            )
        else:
            print(f"[SCORE] {jurisdiction} years_to_entry: not available")

    if not yte_values:
        print("[SCORE] No years_to_entry values available — score = N/A")
        return patents

    avg   = round(sum(yte_values) / len(yte_values), 2)
    score = _avg_to_score(avg)

    print(f"[SCORE] avg_years_to_entry: {avg} → Score: {score}")

    for p in patents:
        p["avg_years_to_entry"] = avg
        p["score"]              = score

    return patents


# ─────────────────────────────────────────────
# 7. US + EP specific score (IP Dimension 1)
# ─────────────────────────────────────────────

def assign_us_ep_score(patents: List[Dict]) -> List[Dict]:
    """
    Calculates avg_years_to_entry_us_ep and ip_dimension_1_score
    using ONLY US and EP jurisdictions.

    Steps:
      1. Collect years_to_entry from the US and EP blocking patents only.
      2. avg_years_to_entry_us_ep = mean of available US/EP values.
      3. ip_dimension_1_score (1-5) based on avg_years_to_entry_us_ep.
      4. Both values are assigned to ALL patents (drug-level metric).
    """
    print("[IP DIM 1] Calculating US+EP avg years to entry and IP Dimension 1 Score...")

    for p in patents:
        p["avg_years_to_entry_us_ep"] = None
        p["ip_dimension_1_score"]     = None

    yte_us_ep = []

    for jurisdiction in ("US", "EP"):
        match = next(
            (
                p for p in patents
                if (p.get("jurisdiction") or "").upper() == jurisdiction
                and p.get("years_to_entry") is not None
            ),
            None,
        )
        if match:
            yte_us_ep.append(match["years_to_entry"])
            print(
                f"[IP DIM 1] {jurisdiction} years_to_entry: "
                f"{match['years_to_entry']} (from {match.get('patent_number')})"
            )
        else:
            print(f"[IP DIM 1] {jurisdiction} years_to_entry: not available")

    if not yte_us_ep:
        print("[IP DIM 1] No US/EP years_to_entry available — IP Dimension 1 Score = N/A")
        return patents

    avg   = round(sum(yte_us_ep) / len(yte_us_ep), 2)
    score = _avg_to_score(avg)

    print(f"[IP DIM 1] avg_years_to_entry_us_ep: {avg} → IP Dimension 1 Score: {score}")

    for p in patents:
        p["avg_years_to_entry_us_ep"] = avg
        p["ip_dimension_1_score"]     = score

    return patents


# ─────────────────────────────────────────────
# Main public function — run all calculators
# ─────────────────────────────────────────────

def primary_market_entry_horizon(patents: List[Dict]) -> List[Dict]:
    """
    Runs all derived score calculators in the correct order, producing the
    "primary market entry horizon" for a drug — i.e. the years-to-entry /
    Score / IP Dimension 1 Score metrics derived from its blocking patents.

    Order matters:
      1. estimated_approval_year  (needs phase_at_filing)
      2. exclusivity_year         (needs estimated_approval_year or real approval date)
      3. controlling_expiry       (needs filing_date + pte)
      4. years_to_entry           (needs exclusivity_year + controlling_expiry)
      5. pediatric adjustment     (adjusts exclusivity_year + recalculates years_to_entry)
      6. score                    (needs years_to_entry — all jurisdictions)
      7. us_ep_score               (needs years_to_entry — US + EP only)

    Call this BEFORE fetching real approval dates, then call again after
    approval dates are attached (the estimated dates get replaced by real
    ones where available).

    Args:
        patents: List of patent dicts (must already have phase_at_filing set)

    Returns:
        Updated patents list with all derived score fields populated.
    """
    patents = assign_estimated_approval_year(patents)
    patents = assign_exclusivity_year(patents)
    patents = assign_controlling_patent_expiry_year(patents)
    patents = assign_years_to_entry(patents)
    patents = apply_pediatric_exclusivity(patents)
    patents = assign_score(patents)
    patents = assign_us_ep_score(patents)
    return patents


# Backward-compatible alias — existing callers using the original
# calculators.run_calculations() name keep working unchanged.
run_calculations = primary_market_entry_horizon


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m IP_refactored.primary_market_entry_horizon.primary_market_entry_horizon <patents.json>")
        sys.exit(1)

    with open(sys.argv[1]) as f:
        input_patents = json.load(f)

    result = primary_market_entry_horizon(input_patents)
    print(json.dumps(result, indent=2, default=str))
