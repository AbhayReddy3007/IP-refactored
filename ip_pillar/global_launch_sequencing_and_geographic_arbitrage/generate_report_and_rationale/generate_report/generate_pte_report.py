"""
generate_pte_report.py
──────────────────────
Regulatory Exclusivity and PTE Analysis PDF report (A4), built from the run's own results (the
shortlisted patents and the arbitrage summary; nothing is read back from BigQuery):

    Header · KPI bar (drugs, jurisdictions, PTE granted, average expiry gap) ·
    1. Strategic Overview (AI-generated) · 2. Patent Term Extension (PTE) Analysis (legend, table by
    jurisdiction, exclusivity rules in scope) · 3. Geographic Arbitrage and Dimension IV Scores ·
    a footer note and page numbers

Gemini writes the strategic overview (a figures-only text is used if it fails). The report is uploaded
next to the other IP reports, to gs://{REPORT_GCS_BUCKET}/{REPORT_GCS_PATH}/:
    {drug}/{GCS_IP_SUBFOLDER}/PTE Analysis.pdf
    {drug}/{GCS_IP_SUBFOLDER}/archive/PTE Analysis+{timestamp}.pdf
(the file name is config.DIMENSION_PTE_ANALYSIS)

    report_for_drug(drug, shortlisted, arbitrage) -> {"gcs_uri", "archive_uri"}
"""

import logging
from datetime import date
from pathlib import Path

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .... import config, report_utils as ru
from ....gemini_utils import gemini_api_call
from ...constants import EXCLUSIVITY_RULES
from . import utils

logger = logging.getLogger(__name__)

PAGE_W = A4[0]
MARGIN = 1.8 * cm
CONTENT_W = PAGE_W - 2 * MARGIN

NARRATIVE_PROMPT = """You are a pharmaceutical patent & regulatory exclusivity strategist.
Below is a summary of a multi-jurisdictional patent analysis for the drug: {drug}.
Write a concise, insight-driven narrative (strictly 180-220 words) covering:
1. Overall patent expiry landscape and exclusivity risk.
2. PTE status and its impact on effective protection.
3. Geographic arbitrage opportunities and weakest jurisdictions.
4. Key strategic recommendations (2-3 bullet points).

Data summary:
- Drug analysed: {drug}
- Jurisdictions: {jurisdictions}
- PTE Granted count: {pte_granted}
- Expiry Gap (Years) — avg: {avg_gap}, min: {min_gap}, max: {max_gap}
- Dimension IV ratings: {rating}

Regulatory exclusivity rules in scope:
{rules}

Rules for your response:
- Plain prose; no markdown headers, no bullet symbols except for recommendations section.
- Recommendations section: start with "Recommendations:" then use "•" bullets.
- Do NOT include any JSON, code, or special formatting.
- Exactly 180-220 words.
- Use only the data above; do not invent facts.
"""


# ─────────────────────────────────────────────
# Data
# ─────────────────────────────────────────────

def _summary(shortlisted: pd.DataFrame, arbitrage: pd.DataFrame) -> dict:
    """The figures the KPI bar and the narrative are built from."""
    gaps = pd.to_numeric(shortlisted["Expiry Gap (Years)"], errors="coerce").dropna()
    months = pd.to_numeric(shortlisted["PTE Months (Granted)"], errors="coerce").fillna(0)
    return {
        "jurisdictions": sorted(shortlisted["Jurisdiction"].dropna().unique()),
        "pte_granted": int((months > 0).sum()),
        "avg_gap": round(gaps.mean(), 1) if not gaps.empty else "N/A",
        "min_gap": int(gaps.min()) if not gaps.empty else "N/A",
        "max_gap": int(gaps.max()) if not gaps.empty else "N/A",
        "rating": arbitrage["Dimension IV Rating"].iloc[0] if not arbitrage.empty else "N/A",
    }


def generate_narrative(drug: str, summary: dict) -> str:
    """The strategic overview (a figures-only text is used if Gemini fails)."""
    rules = "\n".join(f"  {k}: {v}" for k, v in EXCLUSIVITY_RULES.items() if k in summary["jurisdictions"])
    prompt = NARRATIVE_PROMPT.format(drug=drug, rules=rules, **{**summary, "jurisdictions": ", ".join(summary["jurisdictions"])})
    try:
        text = gemini_api_call(prompt, config.GEMINI_TEXT_MODEL, temperature=0.2).strip()
        if text:
            return text
    except Exception:
        logger.exception("[REPORT] Gemini narrative failed for %r — using the figures-only text", drug)
    return (f"{drug}: shortlisted patents in {', '.join(summary['jurisdictions'])}; PTE granted for {summary['pte_granted']}; "
            f"average expiry gap {summary['avg_gap']} years; Dimension IV rating: {summary['rating']}.")


# ─────────────────────────────────────────────
# Styles and building blocks
# ─────────────────────────────────────────────

def _styles() -> dict:
    base = getSampleStyleSheet()

    def style(name, parent="Normal", **kw):
        return ParagraphStyle(name, parent=base[parent], **kw)

    return {
        "title":    style("RptTitle", "Title", fontSize=16, textColor=ru.NAVY, spaceAfter=2, leading=20, alignment=TA_LEFT),
        "subtitle": style("RptSub", fontSize=9, textColor=ru.BLUE, spaceAfter=6, leading=11),
        "h1":       style("RptH1", fontSize=10, textColor=ru.WHITE, leading=14),
        "h2":       style("RptH2", fontSize=9, textColor=ru.NAVY, leading=12, fontName="Helvetica-Bold", spaceAfter=3, spaceBefore=4),
        "body":     style("RptBody", fontSize=8, leading=11, spaceAfter=3, alignment=TA_JUSTIFY),
        "small":    style("RptSmall", fontSize=7, leading=9, textColor=colors.grey),
    }


def _section_header(text: str, st: dict) -> list:
    """A navy banner with the section title."""
    banner = Table([[Paragraph(text, st["h1"])]], colWidths=[CONTENT_W], style=TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), ru.NAVY),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return [Spacer(1, 0.15 * cm), banner, Spacer(1, 0.1 * cm)]


def _grid_style() -> TableStyle:
    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), ru.BLUE),
        ("TEXTCOLOR", (0, 0), (-1, 0), ru.WHITE),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [ru.LIGHT_GREY, ru.WHITE]),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.lightgrey),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ])


def _widths(fixed: list) -> list:
    """Column widths: *fixed*, with the last column widened to fill the page."""
    widths = list(fixed)
    widths[-1] += max(CONTENT_W - sum(widths), 0)
    return widths


def _kpi_bar(shortlisted: pd.DataFrame, summary: dict) -> list:
    gaps = pd.to_numeric(shortlisted["Expiry Gap (Years)"], errors="coerce").dropna()
    kpis = [
        ("Drugs", str(shortlisted["Drug Name"].nunique())),
        ("Jurisdictions", str(len(summary["jurisdictions"]))),
        ("PTE Granted", str(summary["pte_granted"])),
        ("Avg Expiry Gap", f"{gaps.mean():.1f} yrs" if not gaps.empty else "N/A"),
    ]
    value_style = ParagraphStyle("kv", fontSize=14, fontName="Helvetica-Bold", textColor=ru.NAVY, alignment=TA_CENTER)
    label_style = ParagraphStyle("kl", fontSize=7, textColor=colors.grey, alignment=TA_CENTER)
    cells = [
        Table([[Paragraph(value, value_style)], [Paragraph(label, label_style)]],
              colWidths=[CONTENT_W / len(kpis) - 0.2 * cm],
              style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), ru.LIGHT_GREY), ("BOX", (0, 0), (-1, -1), 0.5, ru.BLUE),
                                ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
        for label, value in kpis
    ]
    row = Table([cells], colWidths=[CONTENT_W / len(kpis)] * len(kpis), style=TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2)]))
    return [row, Spacer(1, 0.2 * cm)]


def _legend() -> Table:
    """The PTE status colour key."""
    items = [("Granted", utils.GREEN), ("Pending", utils.AMBER), ("Not filed / Not found", utils.RED), ("Not applicable", colors.grey)]
    chip_style = ParagraphStyle("leg", fontSize=6.5, alignment=TA_CENTER)
    chips = [Table([[Paragraph(f"<font color='white'><b>{label}</b></font>", chip_style)]], colWidths=[3 * cm],
                   style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), colour), ("TOPPADDING", (0, 0), (-1, -1), 1),
                                     ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
             for label, colour in items]
    return Table([chips], colWidths=[3.1 * cm] * len(chips),
                 style=TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 2)]))


def _pte_table(drug: str, shortlisted: pd.DataFrame) -> Table:
    """Drug | Jur. | PTE (mo.) | Expiry Year."""
    rows = [["Drug", "Jur.", "PTE (mo.)", "Expiry Year"]]
    rows += [[drug, row["Jurisdiction"], row["PTE (months)"], row["Controlling Patent Expiry Year"]] for _, row in shortlisted.iterrows()]
    return Table(rows, colWidths=_widths([3.8 * cm, 1.5 * cm, 2 * cm, 2.5 * cm]), style=_grid_style())


def _arbitrage_table(drug: str, arbitrage: pd.DataFrame) -> Table:
    """Drug | Jur. | LOE Year | Score (filled by score) | Signal."""
    rows = [["Drug", "Jur.", "LOE Year", "Score", "Signal"]]
    rows += [[drug, row["Jurisdiction"], row["Product LOE (Year)"], row["Arbitrage Score"], row["Arbitrage Signal"]]
             for _, row in arbitrage.iterrows()]
    style = _grid_style()
    for i, row in enumerate(rows[1:], start=1):
        style.add("BACKGROUND", (3, i), (3, i), utils.score_colour(row[3]))
        style.add("TEXTCOLOR", (3, i), (3, i), ru.WHITE)
        style.add("FONTNAME", (3, i), (3, i), "Helvetica-Bold")
    return Table([[str(v) for v in row] for row in rows], colWidths=_widths([3.5 * cm, 1.5 * cm, 2 * cm, 1.5 * cm, 3.5 * cm]), style=style)


def _dimension4_table(drug: str, arbitrage: pd.DataFrame) -> Table:
    """The drug's Dimension IV score and rating, the rating cell coloured."""
    first = arbitrage.iloc[0]
    rating = str(first["Dimension IV Rating"])
    style = _grid_style()
    style.add("ALIGN", (1, 0), (1, -1), "CENTER")
    style.add("BACKGROUND", (2, 1), (2, 1), colors.HexColor("#" + utils.RATING_HEX.get(rating, "BDC3C7")))
    style.add("TEXTCOLOR", (2, 1), (2, 1), ru.WHITE)
    style.add("FONTNAME", (2, 1), (2, 1), "Helvetica-Bold")
    return Table([["Drug", "Dim. IV Score", "Rating"], [drug, str(first["Dimension IV Score"]), rating]],
                 colWidths=[4.5 * cm, 2.5 * cm, CONTENT_W - 7 * cm], style=style)


def _page_footer(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(colors.grey)
    canvas.drawRightString(PAGE_W - MARGIN, 0.7 * cm,
                           f"Page {doc.page}  |  IPD Patent Analysis — Regulatory Exclusivity & PTE Report  |  {date.today()}")
    canvas.drawString(MARGIN, 0.7 * cm, "CONFIDENTIAL")
    canvas.restoreState()


# ─────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────

def build_report(drug: str, shortlisted: pd.DataFrame, arbitrage: pd.DataFrame, output_path: str) -> str:
    """Write the PDF to *output_path* and return it."""
    shortlisted = utils.prepare_shortlist(shortlisted)
    summary = _summary(shortlisted, arbitrage)
    st = _styles()

    story = [
        Paragraph(f"{drug} — Regulatory Exclusivity &amp; PTE Analysis", st["title"]),
        Paragraph(f"Generated {date.today().strftime('%d %B %Y')}", st["subtitle"]),
        HRFlowable(width="100%", thickness=1.5, color=ru.NAVY, spaceAfter=6),
        *_kpi_bar(shortlisted, summary),
        *_section_header("1. Strategic Overview (AI-Generated)", st),
        Paragraph(generate_narrative(drug, summary).replace("\n", " "), st["body"]),
        *_section_header("2. Patent Term Extension (PTE) Analysis", st),
        _legend(),
        Spacer(1, 0.15 * cm),
        _pte_table(drug, shortlisted),
    ]

    rules = [f"<b>{k}</b>: {v}" for k, v in EXCLUSIVITY_RULES.items() if k in summary["jurisdictions"]]
    if rules:
        story += [Spacer(1, 0.15 * cm), Paragraph("<b>Exclusivity rules in scope:</b>", st["h2"])]
        story += [Paragraph(f"&bull; {rule}", st["small"]) for rule in rules]

    if not arbitrage.empty:
        story += _section_header("3. Geographic Arbitrage &amp; Dimension IV Scores", st)
        story += [
            Paragraph("<b>Arbitrage Score key:</b> 5 = Immediate opportunity &nbsp;|&nbsp; 4 = Strong &nbsp;|&nbsp; "
                      "3 = Meaningful &nbsp;|&nbsp; 2 = Limited &nbsp;|&nbsp; 1 = None &nbsp;|&nbsp; N/A = Reference (US)", st["small"]),
            Spacer(1, 0.1 * cm),
            _arbitrage_table(drug, arbitrage),
            Spacer(1, 0.2 * cm),
            Paragraph("<b>Dimension IV — Overall Geographic Arbitrage Rating</b>", st["h2"]),
            _dimension4_table(drug, arbitrage),
        ]

    story += [
        Spacer(1, 0.3 * cm),
        HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey),
        Paragraph("This report is auto-generated. Regulatory exclusivity periods and PTE grants are sourced from AI inference "
                  "and may require verification against official registers (USPTO, CNIPA, IP Australia, Rospatent, CDSCO, "
                  "ANVISA). Not legal advice.", st["small"]),
    ]

    SimpleDocTemplate(output_path, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=1.5 * cm,
                      bottomMargin=1.5 * cm, title=f"{config.DIMENSION_PTE_ANALYSIS} - {drug}",
                      author="IP Pipeline").build(story, onFirstPage=_page_footer, onLaterPages=_page_footer)
    return str(Path(output_path).resolve())


def report_for_drug(drug_name: str, shortlisted: pd.DataFrame, arbitrage: pd.DataFrame) -> dict:
    """Build the report in a temporary directory and upload it. Called at the end of every
    global_launch_sequencing_and_geographic_arbitrage run. Returns {"drug_name", "gcs_uri", "archive_uri"}."""
    return ru.publish_report(drug_name, config.DIMENSION_PTE_ANALYSIS, lambda path: build_report(drug_name, shortlisted, arbitrage, path))
