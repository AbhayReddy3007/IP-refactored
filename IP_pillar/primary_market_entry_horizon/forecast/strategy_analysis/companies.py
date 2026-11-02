"""
companies.py
────────────
Per-company configuration.
"""

from typing import Dict


# ─────────────────────────────────────────────────────────────────────────────
# Known company → config
# ─────────────────────────────────────────────────────────────────────────────

_COMPANY_CONFIG = {
    "novo nordisk": {"sec_cik": "0000353278", "ticker": "NVO"},
    "eli lilly":    {"sec_cik": "0000059478", "ticker": "LLY"},
    "astrazeneca":  {"sec_cik": "0000806535", "ticker": "AZN"},
    "pfizer":       {"sec_cik": "0000078003", "ticker": "PFE"},
    "amgen":        {"sec_cik": "0000318154", "ticker": "AMGN"},
}


def _get_company_config(company: str) -> Dict:
    key = company.strip().lower()
    for known, cfg in _COMPANY_CONFIG.items():
        if known in key or key in known:
            return cfg
    return {}
