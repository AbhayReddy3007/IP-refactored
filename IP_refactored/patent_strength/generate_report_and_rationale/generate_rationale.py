"""
generate_rationale.py
─────────────────────
The "rationale" of a drug's patent strength: a short summary (at most
RATIONALE_MAX_WORDS words) written from the drug's scores. It is stored in the
`rationale` column of config.BQ_COUNTRY_SCORE_TABLE (the same text on every row of
the drug) and reused by generate_report.py.

Gemini writes the summary from the scores; if that call fails the rationale is
built directly from the same numbers, so a drug always gets one.

    rationale_for_drug(drug, df_shortlist, results, country_scores) -> str
"""

import logging
import re

import pandas as pd

from ... import config
from ...gemini_utils import gemini_api_call
from ..scoring.constants import ACTIVE_SFS, SF_DISPLAY_NAMES
from ..scoring.scoring import compute_weighted_score, infer_jurisdiction

logger = logging.getLogger(__name__)

RATIONALE_MAX_WORDS = 200

RATIONALE_PROMPT = """You are a senior pharmaceutical patent analyst. Write a short summary of the patent strength of {drug} from the data below.

Scoring: every patent is scored 1-5 (1 = very robust, hard to challenge; 5 = weak, an exceptional opportunity for a challenger) on four sub-factors:
SF1 Novelty & Non-Obviousness (40%), SF2 Obvious-to-Combine Risk (30%), SF3 Prosecution History Vulnerability (20%), SF4 Secondary Considerations (10%).
A patent's weighted score combines them; a country's score is the average of its patents times the country weight, and the final patent score is the sum over countries.

DATA
{data}

Rules:
- At most {max_words} words, in plain prose (no bullet points, headings or markdown).
- Say how strong the portfolio is overall, which jurisdictions carry the score, which patents are the most and least robust, and which sub-factor is the main weakness.
- Use only the data above; do not invent facts."""


# ─────────────────────────────────────────────
# Data
# ─────────────────────────────────────────────

def _sf_scores(result: dict) -> dict:
    return {sf: result.get(sf, {}).get("score") for sf in ACTIVE_SFS}


def _fmt(value) -> str:
    return "N/A" if value is None or (isinstance(value, float) and pd.isna(value)) else f"{value:g}"


def patent_lines(df_shortlist: pd.DataFrame, results: list) -> list:
    """One dict per scored patent: number, jurisdiction, type, SF scores, weighted score."""
    lines = []
    for i, (_, row) in enumerate(df_shortlist.iterrows()):
        patent_number = str(row.get("Patent Number", "")).strip()
        lines.append({
            "patent_number": patent_number,
            "jurisdiction":  infer_jurisdiction(patent_number),
            "patent_type":   str(row.get("Step 1 Claim Category", "")).strip() or "N/A",
            "weighted":      compute_weighted_score(results[i]),
            **_sf_scores(results[i]),
        })
    return lines


def sub_factor_averages(lines: list) -> dict:
    averages = {}
    for sf in ACTIVE_SFS:
        scores = [p[sf] for p in lines if p[sf] is not None]
        averages[sf] = round(sum(scores) / len(scores), 2) if scores else None
    return averages


def _data_block(lines: list, country_entry: dict) -> str:
    rows = [f"Final patent score: {_fmt(country_entry['final_patent_score'])} ({len(lines)} patents scored)"]
    for jurisdiction, jdata in sorted(
        country_entry["jurisdictions"].items(), key=lambda item: (-item[1]["country_weight"], item[0])
    ):
        rows.append(
            f"{jurisdiction} ({jdata['label']}): weight {jdata['country_weight']}, {jdata['patent_count']} patents, "
            f"average weighted score {_fmt(jdata['avg_weighted_score'])}, contributes {_fmt(jdata['country_weighted_score'])}"
        )
    averages = sub_factor_averages(lines)
    rows.append("Sub-factor averages: " + "; ".join(f"{SF_DISPLAY_NAMES[sf]} {_fmt(averages[sf])}" for sf in ACTIVE_SFS))
    rows.append("Patents:")
    for p in sorted(lines, key=lambda p: (p["weighted"] is None, p["weighted"] or 0)):
        rows.append(
            f"- {p['patent_number']} ({p['jurisdiction']}, {p['patent_type']}) | "
            + " ".join(f"{sf.upper()}:{_fmt(p[sf])}" for sf in ACTIVE_SFS)
            + f" | weighted {_fmt(p['weighted'])}"
        )
    return "\n".join(rows)


# ─────────────────────────────────────────────
# Text
# ─────────────────────────────────────────────

def limit_words(text: str, max_words: int = RATIONALE_MAX_WORDS) -> str:
    """Collapse whitespace and cut *text* to at most *max_words* words, at a sentence end when possible."""
    text = re.sub(r"\s+", " ", text or "").strip()
    words = text.split(" ")
    if len(words) <= max_words:
        return text
    cut = " ".join(words[:max_words])
    last_stop = cut.rfind(". ")
    return cut[:last_stop + 1] if last_stop > len(cut) // 2 else cut.rstrip(",;:") + "…"


def _fallback_rationale(drug: str, lines: list, country_entry: dict) -> str:
    scored = [p for p in lines if p["weighted"] is not None]
    parts = [f"{drug}: final patent score {_fmt(country_entry['final_patent_score'])} from {len(lines)} blocking, granted patent(s)."]
    carriers = sorted(
        ((j, d) for j, d in country_entry["jurisdictions"].items() if d["country_weighted_score"]),
        key=lambda item: -item[1]["country_weighted_score"],
    )
    if carriers:
        parts.append("Score carried by " + ", ".join(
            f"{j} ({d['patent_count']} patents, average {_fmt(d['avg_weighted_score'])})" for j, d in carriers) + ".")
    if scored:
        ranked = sorted(scored, key=lambda p: p["weighted"])
        parts.append(f"Most robust: {ranked[0]['patent_number']} ({_fmt(ranked[0]['weighted'])}); "
                     f"weakest: {ranked[-1]['patent_number']} ({_fmt(ranked[-1]['weighted'])}).")
    averages = {sf: v for sf, v in sub_factor_averages(lines).items() if v is not None}
    if averages:
        worst = max(averages, key=averages.get)
        parts.append(f"Weakest sub-factor on average: {SF_DISPLAY_NAMES[worst]} ({_fmt(averages[worst])}; 1 = robust, 5 = weak).")
    return limit_words(" ".join(parts))


def rationale_for_drug(drug: str, df_shortlist: pd.DataFrame, results: list, country_scores: dict) -> str:
    """The drug's rationale (<= RATIONALE_MAX_WORDS words)."""
    country_entry = next(iter(country_scores["drug_details"].values()), None)
    if country_entry is None:
        return ""
    lines = patent_lines(df_shortlist, results)
    prompt = RATIONALE_PROMPT.format(
        drug=drug, data=_data_block(lines, country_entry), max_words=RATIONALE_MAX_WORDS,
    )
    try:
        text = limit_words(gemini_api_call(prompt, config.GEMINI_TEXT_MODEL, temperature=0.2, max_output_tokens=1024))
        if text:
            return text
        logger.warning("[RATIONALE] Gemini returned nothing for %r — using the score-based summary", drug)
    except Exception:
        logger.exception("[RATIONALE] Gemini failed for %r — using the score-based summary", drug)
    return _fallback_rationale(drug, lines, country_entry)
