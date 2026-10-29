"""
generate_report.py
──────────────────
Patent Thicket and Circumvention Feasibility PDF report (2-3 pages), built from the run's own
results (nothing is read back from BigQuery):

    Header · score strip (final / average score, patents, jurisdictions, density) ·
    Score Breakdown by Jurisdiction · Executive Summary · Patent Landscape · Final Score ·
    Circumvention Outlook · Design-Around Summary by Category · Key Risks · Conclusion

Gemini writes the narrative sections from the scores and the design-around analysis (a
score-based fallback is used if it fails). The report is uploaded to
gs://{REPORT_GCS_BUCKET}/{REPORT_GCS_PATH}/:
    {drug}/{GCS_IP_SUBFOLDER}/Patent Thicket and Circumvention Feasibility.pdf
    {drug}/{GCS_IP_SUBFOLDER}/archive/Patent Thicket and Circumvention Feasibility+{timestamp}.pdf
(the file name is config.DIMENSION_PATENT_THICKET)

    report_for_drug(drug, drug_score, circumvention, rationale) -> {"gcs_uri", "archive_uri"}
"""

import logging
import tempfile
from datetime import datetime
from pathlib import Path

from reportlab.platypus import HRFlowable, Paragraph, Spacer

from ... import config, report_utils as ru
from ...gemini_utils import gemini_json_call
from ...primary_market_entry_horizon.generate_report_and_rationale.generate_report import upload_report
from ..scoring.scoring import THICKET_SCORE_LABELS, patent_count_label
from .generate_rationale import data_block

logger = logging.getLogger(__name__)

MAX_SUMMARY_CHARS = 420  # per-category summary in the Design-Around table, to keep the report short


def _label(score) -> str:
    return THICKET_SCORE_LABELS[ru.band(score)] if score else "No score"


# ─────────────────────────────────────────────
# Narrative (Gemini)
# ─────────────────────────────────────────────

NARRATIVE_PROMPT = """You are a senior pharmaceutical patent analyst writing a crisp, easy-to-read report for a business reader on the patent thicket and circumvention feasibility of {drug} (505(b)(2) development).

Scoring: the thicket score is 1-5 per jurisdiction (5 = no meaningful secondary patent fence, 1 = dense patent thicket), the lower of a density score (patent count; forecasted patents count at half) and a diversity score (claim categories covered). The drug's score is the average over jurisdictions.

DATA
{data}

Rules:
- Plain language, short sentences. Each text section is at most 3 sentences; each key risk is one sentence.
- Reference specific jurisdictions, claim categories, patent counts and scores. Use only the data above; do not invent facts.

Respond ONLY with a valid JSON object:
{{
  "executive_summary": "<overall IP landscape and score, in plain terms>",
  "patent_landscape": "<how dense and diverse the thicket is; which categories and jurisdictions carry it>",
  "score_narrative": "<why the final score is what it is, and what it means practically>",
  "circumvention_outlook": "<how hard the design-around is per category and the most promising strategies>",
  "key_risks": ["<risk 1>", "<risk 2>", "<risk 3>"],
  "conclusion": "<bottom line for the reader>"
}}"""


def generate_narrative(drug: str, drug_score: dict, circumvention: dict, rationale: str) -> dict:
    """The report's narrative sections (a score-based fallback is used if Gemini fails)."""
    fallback = {
        "executive_summary": rationale,
        "patent_landscape": f"{drug_score['total_patents']} NON-BLOCKING patent(s) across "
                            f"{drug_score['active_areas']} claim categor(ies): {drug_score['active_categories']}.",
        "score_narrative": f"Average thicket score {drug_score['avg_final_score']} / 5 — {_label(drug_score['avg_final_score'])}.",
        "circumvention_outlook": " ".join(
            f"{category}: {r.get('overall_circumvention_difficulty', 'N/A')} difficulty, "
            f"{len(r.get('design_around_strategies') or [])} strategies."
            for category, r in circumvention["results_by_category"].items()),
        "key_risks": [],
        "conclusion": "",
    }
    prompt = NARRATIVE_PROMPT.format(drug=drug, data=data_block(drug_score, circumvention))
    try:
        parsed = gemini_json_call(prompt, config.GEMINI_TEXT_MODEL, temperature=0.2)
    except Exception:
        logger.exception("[REPORT] Gemini narrative failed for %r — using the score-based text", drug)
        parsed = {}
    return {**fallback, **{k: v for k, v in parsed.items() if v}}


# ─────────────────────────────────────────────
# PDF
# ─────────────────────────────────────────────

def build_report(drug_name: str, drug_score: dict, circumvention: dict, rationale: str, output_path: str) -> str:
    """Write the PDF to *output_path* and return it."""
    narrative = generate_narrative(drug_name, drug_score, circumvention, rationale)
    st = ru.styles()
    avg = drug_score["avg_final_score"]
    results = circumvention["results_by_category"]

    story: list = [
        Paragraph(ru.esc(drug_name), st["title"]),
        Paragraph(f"Patent Thicket &amp; Circumvention Feasibility Analysis&nbsp;&nbsp;|&nbsp;&nbsp;"
                  f"{datetime.now().strftime('%B %d, %Y')}", st["meta"]),
        HRFlowable(width="100%", thickness=1.5, color=ru.NAVY, spaceAfter=5),
        ru.stat_strip([
            (f"{ru.band(avg)} / 5" if avg else "N/A", "Final Score"),
            (f"{avg:.1f}", "Avg Score"),
            (str(drug_score["combined_total"]), "Combined Patents"),
            (str(len(drug_score["jurisdiction_scores"])), "Jurisdictions"),
            (patent_count_label(drug_score["adjusted_count"]).split(" (")[0], "Density"),
        ], st),
    ]

    # Score breakdown: forecasted patents replace the old "granted %" column
    if drug_score["jurisdiction_scores"]:
        story.append(Paragraph("Score Breakdown by Jurisdiction", st["h1"]))
        story.append(ru.data_table(
            ["Jurisdiction", "Density", "Diversity", "Base", "Forecasted Patents", "Final"],
            [[js["jurisdiction"], js["density_score"], js["diversity_score"], js["base_score"],
              js["forecasted_total"], js["final_score"]] for js in drug_score["jurisdiction_scores"]],
            [ru.PAGE_WIDTH * w for w in (0.22, 0.14, 0.14, 0.12, 0.24, 0.14)], st, score_cols=(1, 2, 3, 5)))

    story += ru.section("Executive Summary", narrative["executive_summary"], st)
    story += ru.section("Patent Landscape", narrative["patent_landscape"], st)
    story += ru.section(f"Final Score: {ru.band(avg)}/5 — {_label(avg)}" if avg else "Final Score", narrative["score_narrative"], st)
    story += ru.section("Circumvention Outlook", narrative["circumvention_outlook"], st)

    # Design-around summary by category
    if results:
        story.append(Paragraph("Design-Around Summary by Category", st["h1"]))
        story.append(ru.data_table(
            ["Category", "Difficulty", "Strategy Summary"],
            [[category, r.get("overall_circumvention_difficulty", "N/A"), ru.shorten(r.get("summary", ""), MAX_SUMMARY_CHARS)]
             for category, r in results.items()],
            [ru.PAGE_WIDTH * w for w in (0.20, 0.14, 0.66)], st, left_cols=(0, 2)))

    risks = [r for r in narrative["key_risks"] if r]
    if risks:
        story.append(Paragraph("Key Risks", st["h1"]))
        story += [Paragraph(f"&bull; {ru.esc(r)}", st["bullet"]) for r in risks]

    story += ru.section("Conclusion", narrative["conclusion"], st)
    story += [Spacer(1, 8), Paragraph("Patent Thicket Analysis - Confidential", st["footer"])]

    ru.document(output_path, f"{config.DIMENSION_PATENT_THICKET} - {drug_name}").build(story)
    return str(Path(output_path).resolve())


def report_for_drug(drug_name: str, drug_score: dict, circumvention: dict, rationale: str) -> dict:
    """Build the report in a temporary directory and upload it. Called at the end of every
    patent_thicket_and_circumvention run. Returns {"drug_name", "gcs_uri", "archive_uri"}."""
    with tempfile.TemporaryDirectory() as tmp:
        local_path = build_report(drug_name, drug_score, circumvention, rationale, str(Path(tmp) / "report.pdf"))
        gcs_uri, archive_uri = upload_report(
            drug_name, Path(local_path).read_bytes(), datetime.now().strftime("%Y%m%d_%H%M%S"),
            report_name=config.DIMENSION_PATENT_THICKET,
        )
    logger.info("[REPORT] %s: uploaded: %s | archive: %s", drug_name, gcs_uri, archive_uri)
    return {"drug_name": drug_name, "gcs_uri": gcs_uri, "archive_uri": archive_uri}
