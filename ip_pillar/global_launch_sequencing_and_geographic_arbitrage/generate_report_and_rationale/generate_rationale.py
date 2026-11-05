"""
generate_rationale.py
─────────────────────
The "rationale" of a drug's global launch sequencing and geographic arbitrage: a summary of the
drug written from its launch order, patent expiries and arbitrage scores (no length limit). It is
generated on every run, stored in the Rationale column of config.BQ_ARBITRAGE_TABLE (the same text
on every row of the drug) and shown in the run result.

Gemini writes the summary from the scores; if that call fails the rationale is built directly
from the same numbers, so a drug always gets one.

    rationale_for_drug(drug, arbitrage) -> str
"""

import logging

import pandas as pd

from ... import config
from ...gemini_utils import gemini_api_call

logger = logging.getLogger(__name__)

RATIONALE_PROMPT = """You are a senior pharmaceutical patent analyst. Write the drug-level analysis of the global launch sequencing and geographic arbitrage of {drug} from the data below.

Scoring: for each jurisdiction the product LOE (loss of exclusivity) year is the year its last-expiring qualifying patent expires. Jurisdictions are ordered by LOE, earliest first, which is the order in which a generic or 505(b)(2) product could launch. A jurisdiction's arbitrage score is 1-5 (5 = an immediate opportunity, LOE already reached; 4 = LOE 5+ years before the longest; 3 = 3-4 years before; 2 = 2 years before; 1 = no meaningful gap). The Dimension IV score is the average of the non-US scores (the US is the reference market). The protection gap lists which key claim categories (Composition Of Matter, Device, Formulation) are covered by blocking patents in a jurisdiction.

DATA
{data}

Rules:
- Plain prose (no bullet points, headings or markdown). Be complete: finish every sentence and cover the whole picture.
- Say how strong the geographic arbitrage opportunity is overall, in which order and in which years the drug could launch, which jurisdictions offer the most arbitrage and why, and where protection gaps make a launch easier.
- Reference specific jurisdictions, years and scores.
- Use only the data above; do not invent facts."""


# ─────────────────────────────────────────────
# Data
# ─────────────────────────────────────────────

def data_block(arbitrage: pd.DataFrame) -> str:
    """The arbitrage summary as text for the prompts."""
    first = arbitrage.iloc[0]
    rows = [f"Dimension IV score: {first['Dimension IV Score']} / 5 — {first['Dimension IV Rating']}",
            "Jurisdictions in launch order (earliest LOE first):"]
    for _, row in arbitrage.iterrows():
        rows.append(
            f"- #{row['Launch Order']} {row['Jurisdiction']}: LOE {row['Product LOE (Year)']}; gap vs US {row['Gap vs US (Years)']} yr; "
            f"gap vs longest LOE {row['Gap vs Longest LOE (Years)']} yr; step from the previous launch "
            f"{row['Country-to-Country Arbitrage (Years)']} yr; arbitrage score {row['Arbitrage Score']} ({row['Arbitrage Signal']}); "
            f"protection: {row['Key Protection Gap']}")
    return "\n".join(rows)


# ─────────────────────────────────────────────
# Text
# ─────────────────────────────────────────────

def _fallback_rationale(drug: str, arbitrage: pd.DataFrame) -> str:
    first = arbitrage.iloc[0]
    order = ", ".join(f"{row['Jurisdiction']} ({row['Product LOE (Year)']})" for _, row in arbitrage.iterrows())
    parts = [f"{drug}: Dimension IV score {first['Dimension IV Score']} / 5 — {first['Dimension IV Rating']}.",
             f"Launch order by loss of exclusivity: {order}."]
    scored = arbitrage[pd.to_numeric(arbitrage["Arbitrage Score"], errors="coerce").notna()]
    if not scored.empty:
        best = scored.loc[pd.to_numeric(scored["Arbitrage Score"]).idxmax()]
        parts.append(f"Strongest arbitrage: {best['Jurisdiction']} (score {best['Arbitrage Score']}, {best['Arbitrage Signal']}).")
    return " ".join(parts)


def rationale_for_drug(drug: str, arbitrage: pd.DataFrame) -> str:
    """The drug's rationale (no length limit)."""
    if arbitrage.empty:
        return config.NO_QUALIFYING_PATENTS_RATIONALE
    prompt = RATIONALE_PROMPT.format(drug=drug, data=data_block(arbitrage))
    try:
        text = gemini_api_call(prompt, config.GEMINI_TEXT_MODEL, temperature=0.2).strip()
        if text:
            return text
        logger.warning("[RATIONALE] Gemini returned nothing for %r — using the score-based summary", drug)
    except Exception:
        logger.exception("[RATIONALE] Gemini failed for %r — using the score-based summary", drug)
    return _fallback_rationale(drug, arbitrage)
