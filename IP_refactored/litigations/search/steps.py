"""Gemini + Google Search steps: brand names and innovator, then the grouped litigation search."""

import asyncio
import logging
from typing import List, Tuple

from ... import config
from ...gemini_utils import gemini_generate, gemini_generate_async
from ...utils import normalize_patent_number
from ..constants import (
    BRAND_LOOKUP_TEMPERATURE,
    LITIGATION_SEARCH_TEMPERATURE,
    PATENT_GROUP_CONCURRENCY,
    PATENTS_PER_LLM_CALL,
)
from .json_utils import extract_json_from_response, safe_parse_json
from .prompts import BRAND_AND_INNOVATOR_PROMPT, INNOVATOR_PROMPT, LITIGATION_SEARCH_PROMPT
from .results import normalize_case_type

logger = logging.getLogger(__name__)


def _parse_reply(text: str):
    """The JSON in a Gemini reply (a string holding JSON is parsed once more), or None."""
    json_str = extract_json_from_response((text or "").strip())
    data = safe_parse_json(json_str) if json_str else None
    return safe_parse_json(data) if isinstance(data, str) else data


# ─────────────────────────────────────────────
# Brand names and innovator
# ─────────────────────────────────────────────

async def get_brand_and_innovator(drug_name: str, marketed: bool) -> Tuple[List[str], str]:
    """The drug's innovator (original patent holder) and, only for a Marketed drug, its brand names
    (a drug that is not on the market has none). ([], "") for whatever cannot be found."""
    prompt = BRAND_AND_INNOVATOR_PROMPT if marketed else INNOVATOR_PROMPT
    try:
        response = await gemini_generate_async(
            prompt.format(drug_name=drug_name), config.GEMINI_TEXT_MODEL,
            google_search=True, temperature=BRAND_LOOKUP_TEMPERATURE,
        )
        data = _parse_reply(response.text)
    except Exception:
        logger.exception("[LITIGATION] Brand lookup failed for %r", drug_name)
        return [], ""

    if not isinstance(data, dict):
        return [], ""
    brand_names = data.get("brand_names") or []
    innovator = data.get("innovator") or ""
    logger.info("[LITIGATION] %s | brands: %s | innovator: %s", drug_name, ", ".join(brand_names) or "none", innovator or "unknown")
    return brand_names, innovator


# ─────────────────────────────────────────────
# Grouped litigation search
# ─────────────────────────────────────────────

def _search_group(patent_numbers: List[str], drug_name: str, innovator: str) -> Tuple[List[dict], List[str]]:
    """Search one group of patents. Returns (litigations, failed_patents).

    If the reply cannot be parsed (usually truncated JSON), the group is split in half and searched again,
    down to single patents, so nothing is silently lost.
    """
    if not patent_numbers:
        return [], []

    prompt = LITIGATION_SEARCH_PROMPT.format(
        drug_name=drug_name,
        innovator=innovator,
        patent_numbers="\n".join(f"- {p}" for p in patent_numbers),
    )
    data = None
    parse_failed = False
    try:
        response = gemini_generate(
            prompt, config.GEMINI_TEXT_MODEL, google_search=True, temperature=LITIGATION_SEARCH_TEMPERATURE)
        if not (response.text or "").strip():
            return [], []
        data = _parse_reply(response.text)
        parse_failed = data is None
    except Exception:
        logger.exception("[LITIGATION] Search failed for a group of %d patent(s)", len(patent_numbers))
        parse_failed = True

    if parse_failed:
        if len(patent_numbers) == 1:
            return [], list(patent_numbers)
        mid = len(patent_numbers) // 2
        left, left_failed = _search_group(patent_numbers[:mid], drug_name, innovator)
        right, right_failed = _search_group(patent_numbers[mid:], drug_name, innovator)
        return left + right, left_failed + right_failed

    if not isinstance(data, list):
        return [], []

    # Match by normalised number, then restore the exact requested spelling
    requested = {normalize_patent_number(p): p for p in patent_numbers}
    results = []
    for item in data:
        if not isinstance(item, dict):
            continue
        number = normalize_patent_number(item.get("patent_number", ""))
        if number in requested:
            item["patent_number"] = requested[number]
            item["case_type"] = normalize_case_type(item.get("case_type"))
            results.append(item)
    return results, []


async def search_litigations(patents: List[str], drug_name: str, innovator: str) -> Tuple[List[dict], List[str]]:
    """Search every patent for litigation, a few groups at a time. Returns (litigations, failed_patents)."""
    if not patents:
        return [], []

    groups = [patents[i:i + PATENTS_PER_LLM_CALL] for i in range(0, len(patents), PATENTS_PER_LLM_CALL)]
    semaphore = asyncio.Semaphore(PATENT_GROUP_CONCURRENCY)
    logger.info("[LITIGATION] %s | %d patent(s) in %d group(s), %d at a time",
                drug_name, len(patents), len(groups), PATENT_GROUP_CONCURRENCY)

    async def run_group(group: List[str]) -> Tuple[List[dict], List[str]]:
        async with semaphore:
            return await asyncio.to_thread(_search_group, group, drug_name, innovator)

    litigations: List[dict] = []
    failed: List[str] = []
    for group_litigations, group_failed in await asyncio.gather(*(run_group(g) for g in groups)):
        litigations.extend(group_litigations)
        failed.extend(group_failed)

    if failed:
        logger.warning("[LITIGATION] %s | %d patent(s) could not be parsed: %s", drug_name, len(failed), failed)
    return litigations, failed
