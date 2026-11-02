"""
drug_aliases.py
───────────────
Drug aliases derived from clinical_efficacy.
"""

from typing import Dict
import pandas as pd


# ─────────────────────────────────────────────
# Drug aliases — derived from clinical_efficacy
# ─────────────────────────────────────────────

def _build_drug_aliases(clinical_df: pd.DataFrame) -> Dict[str, str]:
    """
    Builds drug alias map from clinical_efficacy molecule names.
    Groups names that normalise to the same base (e.g. 'aleniglipron l-arginine' → 'aleniglipron').
    """
    aliases = {}
    if clinical_df.empty:
        return aliases

    names = clinical_df["Molecule"].dropna().unique().tolist()
    # Group by the first word (base molecule name)
    from collections import defaultdict
    groups = defaultdict(list)
    for name in names:
        base = name.strip().split()[0].lower()
        groups[base].append(name.strip())

    for base, variants in groups.items():
        if len(variants) > 1:
            # Use the shortest name as canonical
            canonical = min(variants, key=len)
            for v in variants:
                if v.lower() != canonical.lower():
                    aliases[v.lower()] = canonical
    return aliases


_DRUG_ALIASES: Dict[str, str] = {}


def _canonicalise_drug(name: str) -> str:
    """Resolves drug aliases."""
    lower = name.strip().lower()
    return _DRUG_ALIASES.get(lower, name.strip())
