"""
scoring.py
Core scoring: per-patent async sub-factor scoring with Gemini, weighted
score computation, final summaries, jurisdiction helpers and country-wise
aggregation.
"""

import asyncio
import concurrent.futures
import json
import logging

import pandas as pd

from ... import config
from ...gemini_utils import gemini_api_call
from ..storage.checkpoint import checkpoint_key, save_checkpoint
from ..retrieval.chunks import fetch_all_chunks_for_patent, rank_chunks_for_sf
from .constants import (
    COUNTRY_WEIGHTS,
    EXECUTOR_MAX_WORKERS,
    JURISDICTION_LABELS,
    SF1_SECTIONS,
    SF2_SECTIONS,
    SF3_SECTIONS,
    SF4_SECTIONS,
    SF_WEIGHTS,
)
from .prompts import (
    FINAL_SUMMARY_PROMPT,
    SF1_PROMPT,
    SF2_PROMPT,
    SF3_PROMPT,
    SF4_PROMPT,
)

logger = logging.getLogger(__name__)

# Thread pool for running blocking Gemini / AlloyDB calls concurrently.
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=EXECUTOR_MAX_WORKERS)

SF_CONFIG = {
    "sf1": (SF1_PROMPT, SF1_SECTIONS),
    "sf2": (SF2_PROMPT, SF2_SECTIONS),
    "sf3": (SF3_PROMPT, SF3_SECTIONS),
    "sf4": (SF4_PROMPT, SF4_SECTIONS),
}

_PREFIX_TO_JURISDICTION = {
    "US": "US", "EP": "EP", "WO": "WO", "CN": "CN", "IN": "IN",
    "JP": "JP", "KR": "KR", "BR": "BR", "MX": "MX", "TW": "TW",
    "AU": "AU", "CA": "CA", "RU": "RU", "EA": "RU",
}


# ── Jurisdiction helpers ──────────────────────────────────────────────────────

def infer_jurisdiction(patent_number: str) -> str:
    """Return the jurisdiction code for a patent number, or ``"UNKNOWN"``."""
    number = str(patent_number).strip().upper()
    for prefix, jurisdiction in _PREFIX_TO_JURISDICTION.items():
        if number.startswith(prefix):
            return jurisdiction
    return "UNKNOWN"


def get_country_weight(jurisdiction: str) -> float:
    """Return the scoring weight for a jurisdiction (0.0 if unweighted)."""
    return COUNTRY_WEIGHTS.get(jurisdiction.strip().upper(), 0.0)


def _no_chunks_result() -> dict:
    return {
        "score": None,
        "reasoning": "No patent chunks found in AlloyDB for this patent.",
        "key_findings": [],
        "chunks_used": 0,
    }


# ── Gemini sub-factor scoring ─────────────────────────────────────────────────

def _run_gemini_score_sync(prompt, chunks, drug, patent_number, filing_date, grant_date) -> dict:
    """Synchronous Gemini call — designed to run in a worker thread."""
    chunks_text = "\n\n---\n\n".join(f"[Chunk {i + 1}]\n{c}" for i, c in enumerate(chunks))
    full_prompt = prompt.format(
        chunks=chunks_text, drug=drug, patent_number=patent_number,
        filing_date=filing_date, grant_date=grant_date,
    )
    try:
        result = json.loads(gemini_api_call(
            full_prompt, config.GEMINI_TEXT_MODEL, response_mime_type="application/json",
        ))
        result["chunks_used"] = len(chunks)
        return result
    except Exception as exc:
        logger.error("Gemini scoring failed for %s: %s", patent_number, exc)
        return {
            "score": None,
            "reasoning": f"Gemini error: {exc}",
            "key_findings": [],
            "chunks_used": len(chunks),
        }


async def score_patent_async(
    gemini_semaphore: asyncio.Semaphore,
    row,
    cached: dict,
    active_sfs,
    cache: dict,
    cache_lock: asyncio.Lock,
) -> dict:
    """
    Score a single patent.

    - AlloyDB is queried ONCE (fetch_all_chunks_for_patent), not once per SF.
    - Each SF re-ranks the shared chunk list in memory (rank_chunks_for_sf).
    - The drug's checkpoint is written after EACH sub-factor (cache_lock avoids races).
    - gemini_semaphore (shared across all patents) throttles Gemini calls only.
    """
    drug = str(row.get("Drug Name", "")).strip()
    patent_number = str(row.get("Patent Number", "")).strip()
    filing_date = str(row.get("Filing Date", "")).strip()
    grant_date = str(row.get("Grant Date", "")).strip()
    source_file = str(row.get("Source File", "")).strip()

    result = dict(cached)
    sfs_to_score = [sf for sf in active_sfs if sf not in result]
    if not sfs_to_score:
        return result

    loop = asyncio.get_running_loop()

    # ONE AlloyDB round-trip for all sub-factors
    all_chunks = await loop.run_in_executor(
        _executor, fetch_all_chunks_for_patent, drug, patent_number, source_file
    )

    async def _score_one_sf(sf_key: str) -> None:
        prompt_template, sections = SF_CONFIG[sf_key]
        chunks = rank_chunks_for_sf(all_chunks, sections)  # in memory, no DB call
        if not chunks:
            sf_result = _no_chunks_result()
        else:
            async with gemini_semaphore:
                sf_result = await loop.run_in_executor(
                    _executor,
                    _run_gemini_score_sync,
                    prompt_template, chunks, drug, patent_number, filing_date, grant_date,
                )
        logger.info("%s | %s score=%s", patent_number, sf_key.upper(), sf_result.get("score", "ERR"))

        # Checkpoint after each individual SF (lock prevents concurrent writes)
        async with cache_lock:
            result[sf_key] = sf_result
            cache[checkpoint_key(drug, patent_number)] = result
            await loop.run_in_executor(_executor, save_checkpoint, cache, drug)

    await asyncio.gather(*(_score_one_sf(sf) for sf in sfs_to_score))

    for sf_key in active_sfs:
        if sf_key not in sfs_to_score:
            logger.info("%s | %s cached", patent_number, sf_key.upper())

    return result


# ── Weighted score + summary ──────────────────────────────────────────────────

def compute_weighted_score(result: dict) -> float | None:
    """Weighted average of the available sub-factor scores (None if none scored)."""
    valid = [
        (score, weight)
        for sf_key, weight in SF_WEIGHTS.items()
        if (score := result.get(sf_key, {}).get("score")) is not None
    ]
    if not valid:
        return None
    return round(sum(s * w for s, w in valid) / sum(w for _, w in valid), 2)


def generate_final_summary(row, result: dict) -> dict:
    """Ask Gemini for a short narrative summary of one scored patent."""
    drug = str(row.get("Drug Name", "")).strip()
    patent_number = str(row.get("Patent Number", "")).strip()
    patent_type = str(row.get("Step 1 Claim Category", "N/A")).strip()

    sf1, sf2, sf3, sf4 = (result.get(k, {}) for k in ("sf1", "sf2", "sf3", "sf4"))
    weighted = compute_weighted_score(result)

    prompt = FINAL_SUMMARY_PROMPT.format(
        patent_number=patent_number, drug=drug, patent_type=patent_type,
        sf1_score=sf1.get("score") or "N/A", sf1_reasoning=sf1.get("reasoning", "Not scored"),
        sf2_score=sf2.get("score") or "N/A", sf2_reasoning=sf2.get("reasoning", "Not scored"),
        sf3_score=sf3.get("score") or "N/A", sf3_reasoning=sf3.get("reasoning", "Not scored"),
        sf4_score=sf4.get("score") or "N/A", sf4_reasoning=sf4.get("reasoning", "Not scored"),
        weighted_score=weighted if weighted is not None else "N/A",
    )
    try:
        return json.loads(gemini_api_call(
            prompt, config.GEMINI_TEXT_MODEL, response_mime_type="application/json",
        ))
    except Exception as exc:
        logger.error("Final summary generation failed for %s: %s", patent_number, exc)
        return {
            "core_inventive_step": f"Error generating summary: {exc}",
            "key_vulnerabilities": "",
            "key_strengths": "",
        }


# ── Country-wise scoring ──────────────────────────────────────────────────────

def compute_country_wise_scores(df_shortlist: pd.DataFrame, results: list[dict]) -> dict:
    """Aggregate patent scores per drug and jurisdiction into a final drug score."""
    drug_data: dict = {}
    patent_lookup: dict = {}

    for i, (_, row) in enumerate(df_shortlist.iterrows()):
        drug = str(row.get("Drug Name", "")).strip()
        patent_number = str(row.get("Patent Number", "")).strip()

        jurisdiction = infer_jurisdiction(patent_number)
        weighted = compute_weighted_score(results[i])

        patent_lookup[f"{drug}|{patent_number}"] = {
            "jurisdiction": jurisdiction,
            "country_weight": get_country_weight(jurisdiction),
            "weighted_score": weighted,
        }
        drug_data.setdefault(drug, {}).setdefault(jurisdiction, []).append(
            {"patent_number": patent_number, "weighted_score": weighted}
        )

    output = {"drug_details": {}, "patent_lookup": patent_lookup}

    for drug, jurisdictions in drug_data.items():
        drug_entry = {"jurisdictions": {}, "final_patent_score": 0.0}

        for jurisdiction, patents in jurisdictions.items():
            weight = get_country_weight(jurisdiction)
            valid_scores = [p["weighted_score"] for p in patents if p["weighted_score"] is not None]

            avg_score = round(sum(valid_scores) / len(valid_scores), 2) if valid_scores else None
            country_score = round(avg_score * weight, 4) if (avg_score is not None and weight > 0) else 0.0

            drug_entry["jurisdictions"][jurisdiction] = {
                "label": JURISDICTION_LABELS.get(jurisdiction, jurisdiction),
                "country_weight": weight,
                "patents": patents,
                "patent_count": len(patents),
                "avg_weighted_score": avg_score,
                "country_weighted_score": country_score,
            }
            drug_entry["final_patent_score"] += country_score

        drug_entry["final_patent_score"] = round(drug_entry["final_patent_score"], 4)
        output["drug_details"][drug] = drug_entry

    return output
