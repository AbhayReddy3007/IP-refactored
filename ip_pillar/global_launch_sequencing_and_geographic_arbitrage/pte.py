"""Patent term extension (PTE) lookup (Gemini)."""

import logging

import pandas as pd

from .. import config
from ..gemini_utils import gemini_json_call
from .constants import JURISDICTION_FULL, PTE_REGISTRY_HINTS, PTE_RULES
from .lookups import run_lookups

logger = logging.getLogger(__name__)

PTE_PROMPT = """You are a patent-term-extension (PTE) expert.
Drug: {drug}
Patent number: {patent_number}
Jurisdiction: {jurisdiction}
Registry to check: {registry}

{task}
Rules:
- Respond ONLY with a JSON object.
- Format: {{"pte_status": "Granted" | "Pending" | "Not filed" | "Not found", "extension_months": <int or null>, "source": "<brief>"}}
- If unsure, use "Not found" and null.
"""

_TASK = ("Task: Has a patent term extension (PTE) been GRANTED for this patent in this jurisdiction? "
         "If yes, how many months was the extension?\n")
_TASK_CANADA = ("Task: Has a Certificate of Supplementary Protection (CSP) been ISSUED for this patent in Canada? "
                "If yes, how many months of additional protection was granted? Note: CSP takes effect after "
                "patent expiry and is capped at 2 years (24 months).\n")


def _result(status: str, months: int = 0, source: str = "") -> dict:
    return {"pte_months": months, "pte_status": status, "pte_source": source}


def lookup_pte(drug_name: str, patent_number: str, jurisdiction: str) -> dict:
    """{"pte_months", "pte_status", "pte_source"} for the patent. Blocking — run it in a thread."""
    rules = PTE_RULES.get(jurisdiction, {})
    if not rules.get("allowed"):
        return _result("Not applicable")

    prompt = PTE_PROMPT.format(
        drug=drug_name, patent_number=patent_number,
        jurisdiction=JURISDICTION_FULL.get(jurisdiction, jurisdiction),
        registry=PTE_REGISTRY_HINTS.get(jurisdiction, "national patent office"),
        task=_TASK_CANADA if jurisdiction == "CA" else _TASK,
    )
    try:
        data = gemini_json_call(prompt, config.GEMINI_TEXT_MODEL)
        status, months, source = data.get("pte_status", "Not found"), data.get("extension_months"), data.get("source", "")
        if months is None or status != "Granted":
            return _result(status, source=source)
        return _result(status, min(int(months), rules.get("max_ext_years", 5) * 12), source)
    except Exception as exc:
        logger.warning("[PTE] Lookup failed for %s (%s): %s", patent_number, jurisdiction, exc)
        return _result("Not found")


async def fill_pte(shortlisted: pd.DataFrame, drug: str, cached: dict) -> None:
    """Add the PTE Status / Months / Source columns in place: from the cache, else from Gemini."""
    lookups = dict(cached)
    eligible = {
        (row["Patent Number"], row["Jurisdiction"]) for _, row in shortlisted.iterrows()
        if str(row.get("Type", "")).strip().lower() != "forecasted" and PTE_RULES.get(row["Jurisdiction"], {}).get("allowed")
    }
    to_query = {(drug, patent, jurisdiction) for patent, jurisdiction in eligible if (patent, jurisdiction) not in lookups}
    logger.info("[LAUNCH] PTE lookups needed: %d (reused %d from the previous run)", len(to_query), len(eligible) - len(to_query))

    for (_, patent, jurisdiction), info in (await run_lookups(to_query, lookup_pte)).items():
        lookups[(patent, jurisdiction)] = info
        logger.info("[LAUNCH] %s (%s) PTE -> %s, %s month(s)", patent, jurisdiction, info["pte_status"], info["pte_months"])

    shortlisted["PTE Status"] = "Not applicable"
    shortlisted["PTE Months (Granted)"] = 0
    shortlisted["PTE Source"] = ""
    for idx, row in shortlisted.iterrows():
        info = lookups.get((row["Patent Number"], row["Jurisdiction"]))
        if info:
            shortlisted.at[idx, "PTE Status"] = info["pte_status"]
            shortlisted.at[idx, "PTE Months (Granted)"] = info["pte_months"]
            shortlisted.at[idx, "PTE Source"] = info["pte_source"]
