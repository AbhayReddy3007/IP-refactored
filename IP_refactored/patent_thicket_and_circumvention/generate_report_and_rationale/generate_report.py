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

import json
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from ... import config
from ...gemini_utils import gemini_api_call
from ...primary_market_entry_horizon.generate_report_and_rationale.generate_report import upload_report
from ..scoring.scoring import THICKET_SCORE_LABELS, patent_count_label
from .generate_rationale import data_block

logger = logging.getLogger(__name__)

_NAVY = colors.HexColor("#1F3864")
_BLUE = colors.HexColor("#2F5496")
_LGREY = colors.HexColor("#F2F2F2")
_WHITE = colors.white
_GREY = colors.HexColor("#666666")

# Thicket scores: 5 = no meaningful fence ... 1 = dense thicket.
_SCORE_CLR = {1: "#CC0000", 2: "#E65100", 3: "#CC9900", 4: "#4CAF50", 5: "#008000"}

MAX_SUMMARY_CHARS = 420  # per-category summary in the Design-Around table, to keep the report short


def _styles() -> dict:
    b = getSampleStyleSheet()
    return {
        "title":  ParagraphStyle("T",  parent=b["Title"],    fontSize=18, leading=21, textColor=_NAVY, spaceAfter=0, alignment=TA_LEFT),
        "meta":   ParagraphStyle("M",  parent=b["Normal"],   fontSize=9, leading=11, textColor=_GREY, spaceAfter=4),
        "h1":     ParagraphStyle("H1", parent=b["Heading2"], fontSize=11.5, leading=14, textColor=_NAVY, spaceBefore=7, spaceAfter=2),
        "body":   ParagraphStyle("BD", parent=b["Normal"],   fontSize=9, leading=12, alignment=TA_JUSTIFY, spaceAfter=3),
        "bullet": ParagraphStyle("BU", parent=b["Normal"],   fontSize=9, leading=12, spaceAfter=2, leftIndent=14, bulletIndent=4),
        "th":     ParagraphStyle("TH", parent=b["Normal"],   fontSize=8.5, leading=10, textColor=_WHITE, alignment=TA_CENTER),
        "td":     ParagraphStyle("TD", parent=b["Normal"],   fontSize=8.5, leading=10, alignment=TA_CENTER),
        "tdl":    ParagraphStyle("TL", parent=b["Normal"],   fontSize=8.5, leading=10.5, alignment=TA_LEFT),
        "big":    ParagraphStyle("BG", parent=b["Normal"],   fontSize=14, leading=17, textColor=_NAVY, alignment=TA_CENTER),
        "small":  ParagraphStyle("SM", parent=b["Normal"],   fontSize=7.5, leading=9, textColor=_GREY, alignment=TA_CENTER),
        "footer": ParagraphStyle("FT", parent=b["Normal"],   fontSize=7.5, leading=9, textColor=_GREY, alignment=TA_CENTER),
    }


def _esc(text) -> str:
    return escape(str(text)) if text is not None else ""


def _band(score) -> int:
    return max(1, min(5, round(score)))


def _label(score) -> str:
    return THICKET_SCORE_LABELS[_band(score)] if score else "No score"


def _shorten(text: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    """*text* cut at the last full sentence within *limit* characters."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return cut[:end + 1] if end > limit // 2 else cut.rstrip() + "…"


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
        parsed = json.loads(gemini_api_call(prompt, config.GEMINI_TEXT_MODEL, temperature=0.2,
                                            response_mime_type="application/json"))
    except Exception:
        logger.exception("[REPORT] Gemini narrative failed for %r — using the score-based text", drug)
        parsed = {}
    return {**fallback, **{k: v for k, v in (parsed or {}).items() if v}}


# ─────────────────────────────────────────────
# PDF building blocks
# ─────────────────────────────────────────────

def _table_style() -> TableStyle:
    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _NAVY),
        ("BOX", (0, 0), (-1, -1), 0.5, _BLUE),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [_WHITE, _LGREY]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ])


def _data_table(headers: list, rows: list, col_widths: list, st: dict, score_cols: tuple = (), left_cols: tuple = ()) -> Table:
    """A table with a navy header; columns in *score_cols* are coloured by their 1-5 thicket score."""
    out = [[Paragraph(f"<b>{h}</b>", st["th"]) for h in headers]]
    for data_row in rows:
        cells = []
        for ci, value in enumerate(data_row):
            text = "N/A" if value is None else str(value)
            style = st["tdl"] if ci in left_cols else st["td"]
            if ci in score_cols and isinstance(value, (int, float)) and value:
                cells.append(Paragraph(f'<font color="{_SCORE_CLR[_band(value)]}"><b>{_esc(text)}</b></font>', style))
            else:
                cells.append(Paragraph(_esc(text), style))
        out.append(cells)
    table = Table(out, colWidths=col_widths, repeatRows=1)
    table.setStyle(_table_style())
    return table


def _score_strip(drug_score: dict, st: dict, width: float) -> Table:
    """Final score / average score / combined patents / jurisdictions / density, side by side."""
    avg = drug_score["avg_final_score"]
    cells = [
        (f"{_band(avg)} / 5" if avg else "N/A", "Final Score"),
        (f"{avg:.1f}", "Avg Score"),
        (str(drug_score["combined_total"]), "Combined Patents"),
        (str(len(drug_score["jurisdiction_scores"])), "Jurisdictions"),
        (patent_count_label(drug_score["adjusted_count"]).split(" (")[0], "Density"),
    ]
    table = Table([[Paragraph(f"<b>{_esc(v)}</b>", st["big"]) for v, _ in cells],
                   [Paragraph(label, st["small"]) for _, label in cells]], colWidths=[width / len(cells)] * len(cells))
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _LGREY),
        ("BOX", (0, 0), (-1, -1), 0.5, _BLUE),
        ("LINEAFTER", (0, 0), (-2, -1), 0.25, colors.lightgrey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return table


def build_report(drug_name: str, drug_score: dict, circumvention: dict, rationale: str, output_path: str) -> str:
    """Write the PDF to *output_path* and return it."""
    narrative = generate_narrative(drug_name, drug_score, circumvention, rationale)
    st = _styles()
    title = f"{config.DIMENSION_PATENT_THICKET} - {drug_name}"
    doc = SimpleDocTemplate(
        output_path, pagesize=A4,
        topMargin=14 * mm, bottomMargin=14 * mm, leftMargin=17 * mm, rightMargin=17 * mm,
        title=title, author="IP Pipeline",
    )
    width = A4[0] - 34 * mm
    avg = drug_score["avg_final_score"]
    results = circumvention["results_by_category"]

    def section(heading: str, text: str) -> list:
        return [Paragraph(heading, st["h1"]), Paragraph(_esc(text), st["body"])] if text else []

    story: list = [
        Paragraph(_esc(drug_name), st["title"]),
        Paragraph(f"Patent Thicket &amp; Circumvention Feasibility Analysis&nbsp;&nbsp;|&nbsp;&nbsp;"
                  f"{datetime.now().strftime('%B %d, %Y')}", st["meta"]),
        HRFlowable(width="100%", thickness=1.5, color=_NAVY, spaceAfter=5),
        _score_strip(drug_score, st, width),
    ]

    # Score breakdown: forecasted patents replace the old "granted %" column
    if drug_score["jurisdiction_scores"]:
        story.append(Paragraph("Score Breakdown by Jurisdiction", st["h1"]))
        story.append(_data_table(
            ["Jurisdiction", "Density", "Diversity", "Base", "Forecasted Patents", "Final"],
            [[js["jurisdiction"], js["density_score"], js["diversity_score"], js["base_score"],
              js["forecasted_total"], js["final_score"]] for js in drug_score["jurisdiction_scores"]],
            [width * w for w in (0.22, 0.14, 0.14, 0.12, 0.24, 0.14)], st, score_cols=(1, 2, 3, 5)))

    story += section("Executive Summary", narrative["executive_summary"])
    story += section("Patent Landscape", narrative["patent_landscape"])
    story += section(f"Final Score: {_band(avg)}/5 — {_label(avg)}" if avg else "Final Score", narrative["score_narrative"])
    story += section("Circumvention Outlook", narrative["circumvention_outlook"])

    # Design-around summary by category
    if results:
        story.append(Paragraph("Design-Around Summary by Category", st["h1"]))
        story.append(_data_table(
            ["Category", "Difficulty", "Strategy Summary"],
            [[category, r.get("overall_circumvention_difficulty", "N/A"), _shorten(r.get("summary", ""))]
             for category, r in results.items()],
            [width * w for w in (0.20, 0.14, 0.66)], st, left_cols=(0, 2)))

    risks = [r for r in narrative["key_risks"] if r]
    if risks:
        story.append(Paragraph("Key Risks", st["h1"]))
        story += [Paragraph(f"&bull; {_esc(r)}", st["bullet"]) for r in risks]

    story += section("Conclusion", narrative["conclusion"])
    story += [Spacer(1, 8), Paragraph("Patent Thicket Analysis - Confidential", st["footer"])]

    doc.build(story)
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
