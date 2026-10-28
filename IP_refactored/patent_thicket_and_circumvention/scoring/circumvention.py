"""Circumvention / 505(b)(2) design-around analysis (AlloyDB chunks + Gemini + Google Search)."""

import asyncio
import json
import logging
import re
import time
from datetime import datetime

import pandas as pd

from ... import config
from ...gemini_utils import gemini_api_call, gemini_generate_async
from ..retrieval.alloydb_helpers import fetch_relevant_chunks
from .constants import CATEGORY_CONCURRENCY, MAX_PATENTS_PER_CATEGORY
from .prompts import CIRCUMVENTION_ONLINE_PROMPT, CIRCUMVENTION_PROMPT, CIRCUMVENTION_SEARCH_PROMPT

logger = logging.getLogger(__name__)

_CHUNK_SECTIONS = ["independent claim", "claim 1", "claims", "background", "summary of invention",
                   "abstract", "example", "formulation", "device", "method", "dosing", "manufacturing"]


# ─────────────────────────────────────────────
# JSON helpers
# ─────────────────────────────────────────────

def _extract_json_from_response(text: str):
    if not text:
        return None
    text = re.sub(r"^```(?:json)?", "", text.strip()).strip()
    text = re.sub(r"```$", "", text).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return match.group(0) if match else None


def _repair_json(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        text = re.sub(r",\s*}", "}", text)
        text = re.sub(r",\s*]", "]", text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text


def _parse_object(text: str) -> dict:
    """The JSON object in a Gemini reply (raises ValueError if there is none)."""
    raw = _extract_json_from_response(text)
    if not raw:
        raise ValueError("Empty or unparseable response")
    data = _repair_json(raw)
    if isinstance(data, str):
        data = json.loads(data)
    return data


def _call_json(prompt: str) -> dict:
    """One Gemini call that must return a JSON object (blocking — run it in a thread)."""
    return json.loads(gemini_api_call(prompt, config.GEMINI_TEXT_MODEL, response_mime_type="application/json"))


async def _search(prompt: str) -> dict:
    """One Google Search-grounded Gemini call, parsed as a JSON object."""
    response = await gemini_generate_async(prompt, config.GEMINI_TEXT_MODEL, google_search=True, temperature=0.1)
    return _parse_object(response.text or "")


# ─────────────────────────────────────────────
# One claim category
# ─────────────────────────────────────────────

async def _analyse_one_category(drug_name: str, category: str, patents: list) -> tuple:
    """Analyse a single category.

    With AlloyDB chunks: a design-around analysis of the patent text, supplemented by a
    grounded FDA / Orange Book / literature search. Without any chunks: one grounded call
    (CIRCUMVENTION_ONLINE_PROMPT) that fills the same fields. A result whose Gemini call
    failed carries "failed": True so it is not cached.
    """
    logger.info("[Circumvention] Category: %s (%d patent(s))", category, len(patents))
    patent_numbers = [p["patent_number"] for p in patents]

    chunk_lists = await asyncio.gather(*[
        asyncio.to_thread(fetch_relevant_chunks, p["drug_name"], p["source_file"], _CHUNK_SECTIONS, 8)
        for p in patents
    ])
    all_chunks = [c for chunks in chunk_lists for c in chunks[:6]]

    if all_chunks:
        chunks_text = "\n\n---\n\n".join(f"[Chunk {i + 1}]\n{c}" for i, c in enumerate(all_chunks[:15]))
        prompt = CIRCUMVENTION_PROMPT.format(
            claim_category=category, drug=drug_name, chunks=chunks_text,
            patent_numbers=", ".join(str(x) for x in patent_numbers),
        )
        try:
            result = await asyncio.to_thread(_call_json, prompt)
        except Exception as e:
            logger.warning("Gemini analysis error for %s: %s", category, e)
            result = {"claim_category": category, "design_around_strategies": [], "summary": f"Error: {e}", "failed": True}

        logger.info("Searching FDA/literature for %s alternatives...", category)
        search_prompt = CIRCUMVENTION_SEARCH_PROMPT.format(
            drug=drug_name, claim_category=category, claim_category_lower=category.lower(),
        )
        try:
            search_data = await _search(search_prompt)
            result["fda_precedents"] = search_data.get("fda_precedents", [])
            result["orange_book_gaps"] = search_data.get("orange_book_gaps", [])
            result["literature_alternatives"] = search_data.get("literature_alternatives", [])
            result["regulatory_viability"] = search_data.get("regulatory_viability", "")
        except Exception as e:
            logger.warning("FDA/literature search error for %s: %s", category, e)
            result.update(fda_precedents=[], orange_book_gaps=[], literature_alternatives=[],
                          regulatory_viability=f"Search error: {e}", failed=True)
    else:
        logger.info("No AlloyDB chunks for '%s' patents of '%s' — generating the narrative via online search...",
                    category, drug_name)
        online_prompt = CIRCUMVENTION_ONLINE_PROMPT.format(
            claim_category=category, drug=drug_name, patent_numbers=", ".join(str(x) for x in patent_numbers),
        )
        try:
            result = await _search(online_prompt)
        except Exception as e:
            logger.warning("Online circumvention search error for %s: %s", category, e)
            result = {
                "claim_category": category, "key_claim_limitations": [], "design_around_strategies": [],
                "white_space_opportunities": [], "overall_circumvention_difficulty": "N/A",
                "fda_precedents": [], "orange_book_gaps": [], "literature_alternatives": [],
                "regulatory_viability": f"Search error: {e}",
                "summary": ("Patent text was unavailable in the local database and the online search also "
                            "failed. Manual review required."),
                "failed": True,
            }

    result["patent_numbers"] = patent_numbers
    result["patent_count"] = len(patents)
    logger.info("Found %d strategies for %s", len(result.get("design_around_strategies", [])), category)
    return category, result


# ─────────────────────────────────────────────
# One drug
# ─────────────────────────────────────────────

def group_patents_by_category(non_blocking_df: pd.DataFrame, max_per_category: int = MAX_PATENTS_PER_CATEGORY) -> dict:
    """{claim category: [{patent_number, drug_name, source_file}, ...]}, at most *max_per_category* each."""
    if "Step 1 Claim Category" not in non_blocking_df.columns:
        logger.warning("[Circumvention] No 'Step 1 Claim Category' column found.")
        return {}

    by_category: dict = {}
    for _, row in non_blocking_df.iterrows():
        drug = str(row.get("Drug Name", "")).strip()
        pn = str(row.get("Patent Number", "")).strip()
        cat = str(row.get("Step 1 Claim Category", "")).strip()
        source_file = str(row.get("Source File", "")).strip()

        if not drug or not pn or cat in ("", "nan", "None"):
            continue
        if not source_file or source_file in ("nan", "None"):
            logger.warning("Missing 'Source File' for patent '%s' (%s) — skipping.", pn, drug)
            continue
        by_category.setdefault(cat, []).append({"patent_number": pn, "drug_name": drug, "source_file": source_file})

    for cat, patents in by_category.items():
        if len(patents) > max_per_category:
            logger.info("[%s] Limiting from %d to %d patents", cat, len(patents), max_per_category)
            by_category[cat] = patents[:max_per_category]
    return by_category


async def run_circumvention_analysis(drug_name: str, patents_by_category: dict, cached: dict = None) -> dict:
    """Design-around analysis of every claim category of one drug.

    *cached* is {category: result} from an earlier run; a category is reused when its patents are
    unchanged, otherwise (or when it failed before) it is analysed again.
    """
    t0 = time.time()
    cached = cached or {}
    logger.info("[Circumvention] Analysing design-around strategies for %s...", drug_name)

    reuse, todo = {}, {}
    for category, patents in patents_by_category.items():
        old = cached.get(category)
        if old and not old.get("failed") and sorted(old.get("patent_numbers", [])) == sorted(p["patent_number"] for p in patents):
            reuse[category] = old
        else:
            todo[category] = patents
    if reuse:
        logger.info("[Circumvention] %d categor(ies) reused from the cache: %s", len(reuse), sorted(reuse))

    semaphore = asyncio.Semaphore(CATEGORY_CONCURRENCY)

    async def _bounded(category, patents):
        async with semaphore:
            return await _analyse_one_category(drug_name, category, patents)

    analysed = dict(await asyncio.gather(*[_bounded(c, p) for c, p in todo.items()]))
    category_results = {c: reuse.get(c) or analysed[c] for c in patents_by_category}

    elapsed = round(time.time() - t0, 1)
    logger.info("[Circumvention] Completed analysis for %s in %ss", drug_name, elapsed)
    return {
        "drug_name": drug_name,
        "categories_analysed": list(category_results),
        "results_by_category": category_results,
        "analysis_date": datetime.now().strftime("%Y-%m-%d"),
        "search_time_seconds": elapsed,
    }
