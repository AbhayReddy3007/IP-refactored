"""
generate_report.py
──────────────────
Patent Thicket and Circumvention Feasibility PDF report, built from the run's own results
(nothing is read back from BigQuery):
    Drug-Level Analysis (the rationale) · Thicket Score by jurisdiction ·
    Circumvention Analysis per claim category (strategies, white space, regulatory viability) · legend

It is uploaded to gs://{REPORT_GCS_BUCKET}/{REPORT_GCS_PATH}/:
    {drug}/{GCS_IP_SUBFOLDER}/Patent Thicket and Circumvention Feasibility.pdf
    {drug}/{GCS_IP_SUBFOLDER}/archive/Patent Thicket and Circumvention Feasibility+{timestamp}.pdf
(the file name is config.DIMENSION_PATENT_THICKET)

    report_for_drug(drug, drug_score, circumvention, rationale) -> {"gcs_uri", "archive_uri"}
"""

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
from ...primary_market_entry_horizon.generate_report_and_rationale.generate_report import upload_report
from ..scoring.scoring import THICKET_SCORE_LABELS

logger = logging.getLogger(__name__)

_NAVY = colors.HexColor("#1F3864")
_BLUE = colors.HexColor("#2F5496")
_LGREY = colors.HexColor("#F2F2F2")
_WHITE = colors.white
_GREY = colors.HexColor("#666666")

# Thicket scores: 5 = no meaningful fence ... 1 = dense thicket.
_SCORE_CLR = {1: "#CC0000", 2: "#E65100", 3: "#CC9900", 4: "#4CAF50", 5: "#008000"}


def _styles() -> dict:
    b = getSampleStyleSheet()
    return {
        "title":  ParagraphStyle("T",  parent=b["Title"],    fontSize=16, leading=19, textColor=_NAVY, spaceAfter=2),
        "meta":   ParagraphStyle("M",  parent=b["Normal"],   fontSize=8.5, leading=10, textColor=_GREY, spaceAfter=4),
        "h1":     ParagraphStyle("H1", parent=b["Heading2"], fontSize=11.5, leading=14, textColor=_NAVY, spaceBefore=6, spaceAfter=2),
        "h2":     ParagraphStyle("H2", parent=b["Heading3"], fontSize=9.5, leading=12, textColor=_BLUE, spaceBefore=4, spaceAfter=1),
        "body":   ParagraphStyle("BD", parent=b["Normal"],   fontSize=8.5, leading=11, alignment=TA_JUSTIFY, spaceAfter=3),
        "bullet": ParagraphStyle("BU", parent=b["Normal"],   fontSize=8.5, leading=11, spaceAfter=1, leftIndent=18, bulletIndent=6),
        "legend": ParagraphStyle("LG", parent=b["Normal"],   fontSize=7, leading=9, textColor=_GREY, spaceAfter=1),
        "th":     ParagraphStyle("TH", parent=b["Normal"],   fontSize=8, leading=10, textColor=_WHITE, alignment=TA_CENTER),
        "td":     ParagraphStyle("TD", parent=b["Normal"],   fontSize=8, leading=10, alignment=TA_CENTER),
        "tdl":    ParagraphStyle("TL", parent=b["Normal"],   fontSize=8, leading=10, alignment=TA_LEFT),
        "footer": ParagraphStyle("FT", parent=b["Normal"],   fontSize=7, leading=9, textColor=_GREY, alignment=TA_CENTER),
    }


def _esc(text) -> str:
    return escape(str(text)) if text is not None else ""


def _paragraphs(text: str, style) -> list:
    """One Paragraph per non-empty line of *text*."""
    return [Paragraph(_esc(line.strip()), style) for line in (text or "").split("\n") if line.strip()]


def _data_table(headers: list, rows: list, col_widths: list, st: dict, score_cols: tuple = (), left_cols: tuple = ()) -> Table:
    """A table with a navy header; columns in *score_cols* are coloured by their 1-5 thicket score."""
    out = [[Paragraph(f"<b>{h}</b>", st["th"]) for h in headers]]
    for data_row in rows:
        cells = []
        for ci, value in enumerate(data_row):
            text = "N/A" if value is None else str(value)
            style = st["tdl"] if ci in left_cols else st["td"]
            if ci in score_cols and isinstance(value, (int, float)) and value:
                band = max(1, min(5, round(value)))
                cells.append(Paragraph(f'<font color="{_SCORE_CLR[band]}"><b>{_esc(text)}</b></font>', style))
            else:
                cells.append(Paragraph(_esc(text), style))
        out.append(cells)
    table = Table(out, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _NAVY),
        ("BOX", (0, 0), (-1, -1), 0.5, _BLUE),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [_WHITE, _LGREY]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
    ]))
    return table


def _bullets(label: str, items, st: dict) -> list:
    items = [i for i in (items or []) if i]
    if not items:
        return []
    return [Paragraph(f"<b>{_esc(label)}</b>", st["body"]),
            *[Paragraph(f"&bull; {_esc(i)}", st["bullet"]) for i in items]]


def build_report(drug_name: str, drug_score: dict, circumvention: dict, rationale: str, output_path: str) -> str:
    """Write the PDF to *output_path* and return it."""
    st = _styles()
    title = f"{config.DIMENSION_PATENT_THICKET} - {drug_name}"
    doc = SimpleDocTemplate(
        output_path, pagesize=A4,
        topMargin=14 * mm, bottomMargin=12 * mm, leftMargin=17 * mm, rightMargin=17 * mm,
        title=title, author="IP Pipeline",
    )
    width = A4[0] - 34 * mm
    avg = drug_score["avg_final_score"]
    label = THICKET_SCORE_LABELS.get(max(1, min(5, round(avg))), "") if avg else "No score"
    results = circumvention["results_by_category"]

    story: list = [
        Paragraph(_esc(title), st["title"]),
        Paragraph(f"Generated {datetime.now().strftime('%B %d, %Y')}&nbsp;&nbsp;•&nbsp;&nbsp;"
                  f"{drug_score['total_patents']} Non-Blocking Patents&nbsp;&nbsp;•&nbsp;&nbsp;"
                  f"{len(drug_score['jurisdiction_scores'])} Jurisdiction(s)&nbsp;&nbsp;•&nbsp;&nbsp;"
                  f"Average thicket score {avg} / 5 — {_esc(label)}", st["meta"]),
        HRFlowable(width="100%", thickness=1.5, color=_NAVY, spaceAfter=4),
        Paragraph(f"Drug-Level Analysis — {_esc(drug_name)}", st["h1"]),
        *_paragraphs(rationale or "No rationale provided.", st["body"]),
    ]

    # Thicket score per jurisdiction
    if drug_score["jurisdiction_scores"]:
        story.append(Paragraph("Patent Thicket Score by Jurisdiction", st["h1"]))
        rows = [[js["jurisdiction"], js["combined_total"], js["active_areas"], js["density_score"],
                 js["diversity_score"], f"{js['validation_pct']:g}%", js["final_score"], js["final_label"]]
                for js in drug_score["jurisdiction_scores"]]
        story.append(_data_table(
            ["Jur", "Patents", "Areas", "Density", "Diversity", "Granted", "Final", "Interpretation"], rows,
            [width * w for w in (0.07, 0.09, 0.08, 0.09, 0.10, 0.09, 0.08, 0.40)], st,
            score_cols=(3, 4, 6), left_cols=(7,)))

    # Circumvention per claim category
    story.append(Paragraph("Circumvention Analysis by Claim Category", st["h1"]))
    if results:
        story.append(_data_table(
            ["Claim Category", "# Patents", "Difficulty", "# Strategies"],
            [[category, r.get("patent_count", 0), r.get("overall_circumvention_difficulty", "N/A"),
              len(r.get("design_around_strategies") or [])] for category, r in results.items()],
            [width * w for w in (0.40, 0.15, 0.25, 0.20)], st, left_cols=(0,)))
    else:
        story.append(Paragraph("No claim categories were analysed.", st["body"]))

    for category, r in results.items():
        story.append(Paragraph(f"{_esc(category)} — {_esc(r.get('overall_circumvention_difficulty', 'N/A'))}", st["h2"]))
        story += _paragraphs(r.get("summary", ""), st["body"])
        strategies = r.get("design_around_strategies") or []
        if strategies:
            story.append(_data_table(
                ["Strategy", "Feasibility", "Regulatory pathway"],
                [[s.get("strategy", ""), s.get("feasibility", ""), s.get("regulatory_pathway", "")] for s in strategies],
                [width * w for w in (0.50, 0.14, 0.36)], st, left_cols=(0, 2)))
        story += _bullets("Key claim limitations", r.get("key_claim_limitations"), st)
        story += _bullets("White-space opportunities", r.get("white_space_opportunities"), st)
        if r.get("regulatory_viability"):
            story.append(Paragraph(f"<b>Regulatory viability:</b> {_esc(r['regulatory_viability'])}", st["body"]))

    story += [Spacer(1, 4),
              Paragraph("<b>Thicket score:</b> 5 = no meaningful secondary patent fence · 4 = high probability of design-around · "
                        "3 = requires a structured strategy · 2 = circumvention and litigation become costly · 1 = dense patent thicket",
                        st["legend"]),
              Spacer(1, 4),
              Paragraph("<i>Auto-generated from the Patent Thicket and Circumvention scoring output using Gemini.</i>", st["footer"])]

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
