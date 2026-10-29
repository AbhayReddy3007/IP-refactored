"""
generate_rationale.py
─────────────────────
The "rationale" of a drug's patent thicket and circumvention feasibility: a summary of the
drug written from its thicket scores and design-around analysis (no length limit). It is
generated on every run and shown in the report and the run result; it is not stored in BigQuery.

Gemini writes the summary from the scores; if that call fails the rationale is built directly
from the same numbers, so a drug always gets one.

    rationale_for_drug(drug, drug_score, circumvention) -> str
"""

import logging

from ... import config
from ...gemini_utils import gemini_api_call
from ..scoring.scoring import THICKET_SCORE_LABELS

logger = logging.getLogger(__name__)

RATIONALE_PROMPT = """You are a senior pharmaceutical patent analyst. Write the drug-level analysis of the patent thicket and circumvention feasibility of {drug} from the data below.

Scoring: the thicket score is 1-5 per jurisdiction (5 = no meaningful secondary patent fence, 1 = dense patent thicket). It is the lower of a density score (the number of NON-BLOCKING patents, with forecasted patents counted at half weight) and a diversity score (the number of claim categories they cover), adjusted for how many of the counted patents are forecasted rather than granted. The drug's score is the average over its jurisdictions. Composition-of-matter patents are blocking patents and are not counted.
The circumvention analysis lists, per claim category, the design-around strategies (505(b)(2) and other pathways) that omit a key claim limitation, with feasibility and regulatory pathway.

DATA
{data}

Rules:
- Plain prose (no bullet points, headings or markdown). Be complete: finish every sentence and cover the whole picture.
- Say how dense and diverse the thicket is overall, which jurisdictions and claim categories carry it, and how hard it is to design around (which categories are easiest and hardest, and the most promising strategies and why).
- Reference specific jurisdictions, claim categories, patent counts and scores.
- Use only the data above; do not invent facts."""


# ─────────────────────────────────────────────
# Data
# ─────────────────────────────────────────────

def _fmt(value) -> str:
    return "N/A" if value is None else f"{value:g}" if isinstance(value, (int, float)) else str(value)


def _label(score) -> str:
    return THICKET_SCORE_LABELS.get(max(1, min(5, round(score))), "") if score else ""


def _data_block(drug_score: dict, circumvention: dict) -> str:
    rows = [
        f"Average thicket score: {_fmt(drug_score['avg_final_score'])} / 5 ({_label(drug_score['avg_final_score'])})",
        f"NON-BLOCKING patents counted: {drug_score['total_patents']} across {drug_score['active_areas']} claim "
        f"categor(ies): {drug_score['active_categories']}",
        "Jurisdictions:",
    ]
    for js in drug_score["jurisdiction_scores"]:
        rows.append(
            f"- {js['jurisdiction']}: final {_fmt(js['final_score'])} ({js['final_label']}); density {js['density_score']} "
            f"({js['density_label']}), diversity {js['diversity_score']} ({js['diversity_label']}); "
            f"{js['combined_total']} patents incl. forecasted, {js['validation_pct']}% granted; categories: {js['active_categories']}"
        )
    rows.append("Circumvention by claim category:")
    for category, result in circumvention["results_by_category"].items():
        rows.append(
            f"- {category} ({result.get('patent_count', 0)} patents): difficulty {result.get('overall_circumvention_difficulty', 'N/A')}; "
            f"summary: {result.get('summary', '')}"
        )
        for strat in result.get("design_around_strategies") or []:
            rows.append(f"    strategy: {strat.get('strategy', '')} | feasibility {strat.get('feasibility', '')} "
                        f"| pathway {strat.get('regulatory_pathway', '')}")
        if result.get("regulatory_viability"):
            rows.append(f"    regulatory viability: {result['regulatory_viability']}")
    return "\n".join(rows)


# ─────────────────────────────────────────────
# Text
# ─────────────────────────────────────────────

def _fallback_rationale(drug: str, drug_score: dict, circumvention: dict) -> str:
    avg = drug_score["avg_final_score"]
    parts = [f"{drug}: average thicket score {_fmt(avg)} / 5"
             + (f" ({_label(avg)})" if avg else "")
             + f" from {drug_score['total_patents']} NON-BLOCKING patent(s) in {drug_score['active_areas']} claim category(ies)."]
    jurisdictions = drug_score["jurisdiction_scores"]
    if jurisdictions:
        parts.append("By jurisdiction: " + "; ".join(
            f"{js['jurisdiction']} {_fmt(js['final_score'])} ({js['combined_total']} patents, {js['active_areas']} areas)"
            for js in jurisdictions) + ".")
    results = circumvention["results_by_category"]
    if results:
        parts.append("Circumvention: " + "; ".join(
            f"{category} {result.get('overall_circumvention_difficulty', 'N/A')} "
            f"({len(result.get('design_around_strategies') or [])} strategies)"
            for category, result in results.items()) + ".")
    return " ".join(parts)


def rationale_for_drug(drug: str, drug_score: dict, circumvention: dict) -> str:
    """The drug's rationale (no length limit)."""
    prompt = RATIONALE_PROMPT.format(drug=drug, data=_data_block(drug_score, circumvention))
    try:
        text = gemini_api_call(prompt, config.GEMINI_TEXT_MODEL, temperature=0.2).strip()
        if text:
            return text
        logger.warning("[RATIONALE] Gemini returned nothing for %r — using the score-based summary", drug)
    except Exception:
        logger.exception("[RATIONALE] Gemini failed for %r — using the score-based summary", drug)
    return _fallback_rationale(drug, drug_score, circumvention)
