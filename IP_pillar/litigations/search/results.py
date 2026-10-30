"""Post-processing of the litigation search: case-type normalisation, de-duplication, grouping, summary."""

from typing import Dict, List

from ..constants import CASE_TYPE_ALIASES, CASE_TYPES, OTHER_CASE_TYPE


def normalize_case_type(raw) -> str:
    """One of constants.CASE_TYPES for a case type Gemini returned (variants are mapped), else "Other"."""
    value = str(raw or "").strip()
    if value in CASE_TYPES:
        return value
    lowered = value.lower()
    if lowered in CASE_TYPE_ALIASES:
        return CASE_TYPE_ALIASES[lowered]
    for alias, canonical in CASE_TYPE_ALIASES.items():
        if alias in lowered:
            return canonical
    return OTHER_CASE_TYPE


def deduplicate_litigations(litigations: List[Dict]) -> List[Dict]:
    """Drop duplicates on (patent, case_type, challenger, case_number).

    case_number is part of the key so distinct suits (different districts, refiled IPRs) are not merged.
    Rows without a case number still collapse on the first three fields.
    """
    seen: set = set()
    unique: List[Dict] = []
    for lit in litigations:
        key = (
            str(lit.get("patent_number", "")).strip(),
            str(lit.get("case_type", "")).strip(),
            str(lit.get("challenger", "")).strip().lower(),
            str(lit.get("case_number", "")).strip().lower(),
        )
        if key not in seen:
            seen.add(key)
            unique.append(lit)
    return unique


def group_by_type(litigations: List[Dict]) -> Dict[str, List[Dict]]:
    """{case_type: [cases]}."""
    groups: Dict[str, List[Dict]] = {}
    for lit in litigations:
        groups.setdefault(lit.get("case_type") or OTHER_CASE_TYPE, []).append(lit)
    return groups


def unique_challengers(litigations: List[Dict]) -> List[str]:
    """The distinct challenger names, sorted."""
    return sorted({lit["challenger"] for lit in litigations if lit.get("challenger")})


def build_summary(litigations: List[Dict], cases_by_type: Dict[str, List[Dict]]) -> str:
    """One-line summary stored on every row of the drug."""
    challengers = unique_challengers(litigations)
    return (
        f"Found {len(litigations)} litigation case(s) across {len(cases_by_type)} "
        f"category/categories. Key challengers: {', '.join(challengers[:5]) or 'none identified'}."
    )
