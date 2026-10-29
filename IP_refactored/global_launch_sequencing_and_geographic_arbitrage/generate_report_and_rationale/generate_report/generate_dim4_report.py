"""
generate_dim4_report.py
───────────────────────
Global Launch Sequencing and Arbitrage PDF report (Letter), built from the run's own results (the
shortlisted patents and the arbitrage summary; nothing is read back from BigQuery):

    Cover (drug, subtitle, date) · Dimension IV badge · Shortlisted Secondary Patents ·
    Geographic Arbitrage Map (table and a loss-of-exclusivity chart) · Analysis (Gemini: patent
    landscape, geographic arbitrage, key protection gaps) · Patent Expiry Summary

Gemini writes the analysis from the expiry and arbitrage figures (the rationale is used if it fails).
The report is uploaded next to the other IP reports, to gs://{REPORT_GCS_BUCKET}/{REPORT_GCS_PATH}/:
    {drug}/{GCS_IP_SUBFOLDER}/Global Launch Sequencing & Arbitrage.pdf
    {drug}/{GCS_IP_SUBFOLDER}/archive/Global Launch Sequencing & Arbitrage+{timestamp}.pdf
(the file name is config.DIMENSION_GLOBAL_LAUNCH)

    report_for_drug(drug, shortlisted, arbitrage, rationale) -> {"gcs_uri", "archive_uri"}
"""

import logging
import math
import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch, mm
from reportlab.graphics.shapes import Drawing, Line, Rect, String
from reportlab.platypus import HRFlowable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .... import config, report_utils as ru
from ....gemini_utils import gemini_api_call
from ...constants import TARGET_JURISDICTIONS
from . import utils

logger = logging.getLogger(__name__)

NARRATIVE_HEADERS = ["PATENT LANDSCAPE OVERVIEW", "GEOGRAPHIC ARBITRAGE ANALYSIS", "KEY PROTECTION GAPS"]

NARRATIVE_PROMPT = """
You are a senior pharmaceutical patent analyst writing a drug-level patent
assessment for an internal IP strategy report.

DRUG: {drug}

PATENT EXPIRY DATA (one row per jurisdiction):
{expiry}

GEOGRAPHIC ARBITRAGE DATA (one row per jurisdiction):
{arbitrage}

OVERALL DIMENSION IV SCORE: {score} — {rating}

TARGET JURISDICTIONS CONSIDERED: {considered}
JURISDICTIONS WITH BLOCKING PATENTS ANALYSED: {analysed}

TASK:
Write a detailed, analytical drug-level narrative (220-280 words) structured
into exactly these three sections. Use the exact plain-text section headers shown
below on their own line (no markdown, no asterisks, no bullets):

PATENT LANDSCAPE OVERVIEW
Write 7-8 sentences summarising the breadth and strength of patent protection
for this drug across all jurisdictions, including the range of expiry years
and whether PTE extensions have been granted.
Do NOT give analysis of US. It is only for reference. Analyse other jurisdictions
based on the US information. Mention the Patent Numbers.
Mention that patents from {considered} were considered, but only the blocking
ones were analysed. Jurisdictions not present in the analysis data lacked blocking
patents — do NOT say "no data is available", instead say they were considered but
not included as they lacked blocking patents.
Highlight any jurisdictions with particularly strong or weak patent protection,
and note the overall Dimension IV Score and Rating for the drug.

GEOGRAPHIC ARBITRAGE ANALYSIS
Write 10-12 sentences analysing where the strongest and weakest arbitrage
opportunities exist. Reference specific jurisdictions, LOE years, gap vs US,
gap vs longest LOE, and arbitrage signal ratings. Explain the strategic
significance of the "Gap vs Longest LOE" metric — it measures how many years
earlier a jurisdiction loses exclusivity compared to the jurisdiction with the
longest protection.

KEY PROTECTION GAPS
Write 7-8 sentences identifying any missing claim categories or jurisdictions
with weak or absent protection, and explain what this means for a potential
generic or biosimilar entrant.

Rules:
- Do NOT include any recommendations or strategic advice sections.
- Formal, precise tone. No filler phrases.
- Do NOT give Analysis for US patents. Compare other Patents with the US information.
- Use full jurisdiction names, not abbreviations.
- Do NOT invent any numbers or dates — use only the data provided above.
- Plain text only. No markdown, no bold, no asterisks, no bullet points.
""".strip()


# ─────────────────────────────────────────────
# Narrative (Gemini)
# ─────────────────────────────────────────────

def _names(codes) -> str:
    return ", ".join(sorted(utils.jurisdiction_name(c) for c in codes))


def _narrative_prompt(drug: str, shortlisted: pd.DataFrame, arbitrage: pd.DataFrame) -> str:
    expiry = [
        f"  {utils.jurisdiction_name(r['Jurisdiction'])} | Patent: {r['Patent Number']} | {r['Step 1 Claim Category']} | "
        f"Expiry Year: {r['Controlling Patent Expiry Year']} | Gap: {r['Expiry Gap (Years)']} yrs | PTE: {r['PTE (months)']} months"
        for _, r in shortlisted.iterrows()]
    rows = [
        f"  {utils.jurisdiction_name(r['Jurisdiction'])} | LOE: {r['Product LOE (Year)']} | Gap vs US: {r['Gap vs US (Years)']} yrs | "
        f"Gap vs Longest LOE: {r['Gap vs Longest LOE (Years)']} yrs | Signal: {r['Arbitrage Signal']} | "
        f"Protection gap: {r['Key Protection Gap']}"
        for _, r in arbitrage.iterrows()]
    first = arbitrage.iloc[0] if not arbitrage.empty else None
    analysed = (set(shortlisted["Jurisdiction"]) | set(arbitrage["Jurisdiction"])) & TARGET_JURISDICTIONS
    return NARRATIVE_PROMPT.format(
        drug=drug, expiry="\n".join(expiry) or "  No data.", arbitrage="\n".join(rows) or "  No data.",
        score=first["Dimension IV Score"] if first is not None else "N/A",
        rating=first["Dimension IV Rating"] if first is not None else "N/A",
        considered=_names(TARGET_JURISDICTIONS), analysed=_names(analysed))


def generate_narrative(drug: str, shortlisted: pd.DataFrame, arbitrage: pd.DataFrame, rationale: str) -> str:
    """The analysis text (the rationale is used if Gemini fails)."""
    try:
        text = gemini_api_call(_narrative_prompt(drug, shortlisted, arbitrage), config.GEMINI_TEXT_MODEL, temperature=0.2).strip()
        if text:
            return text
    except Exception:
        logger.exception("[REPORT] Gemini narrative failed for %r — using the rationale", drug)
    return rationale


# ─────────────────────────────────────────────
# Styles and building blocks
# ─────────────────────────────────────────────

def _styles() -> dict:
    base = getSampleStyleSheet()
    return {
        "title":  ParagraphStyle("T", parent=base["Title"], fontSize=18, leading=22, textColor=ru.NAVY, spaceAfter=2),
        "meta":   ParagraphStyle("M", parent=base["Normal"], fontSize=9, leading=11, textColor=colors.HexColor("#666666"), spaceAfter=6),
        "h1":     ParagraphStyle("H1", parent=base["Heading2"], fontSize=12, leading=15, textColor=ru.NAVY, spaceBefore=8, spaceAfter=4),
        "h2":     ParagraphStyle("H2", parent=base["Heading3"], fontSize=11, leading=14, textColor=ru.NAVY, spaceBefore=6, spaceAfter=2),
        "body":   ParagraphStyle("BD", parent=base["Normal"], fontSize=9, leading=12, alignment=TA_JUSTIFY, spaceAfter=4),
        "th":     ParagraphStyle("TH", parent=base["Normal"], fontSize=8, leading=10, textColor=ru.WHITE, alignment=TA_CENTER),
        "td":     ParagraphStyle("TD", parent=base["Normal"], fontSize=8, leading=10, alignment=TA_CENTER),
        "tdl":    ParagraphStyle("TL", parent=base["Normal"], fontSize=8, leading=10, alignment=TA_LEFT),
        "footer": ParagraphStyle("FT", parent=base["Normal"], fontSize=7, leading=9, textColor=colors.HexColor("#999999"), alignment=TA_CENTER),
    }


def _table(headers: list, rows: list, widths: list, st: dict, signal_col: int = -1, score_col: int = -1) -> Table:
    """A table with a navy header; the signal and score columns are coloured."""
    out = [[Paragraph(f"<b>{h}</b>", st["th"]) for h in headers]]
    for data_row in rows:
        cells = []
        for ci, value in enumerate(data_row):
            text = ru.esc(utils.cell(value))
            if ci == signal_col:
                cells.append(Paragraph(f'<font color="#{utils.SIGNAL_HEX.get(text, "BDC3C7")}"><b>{text}</b></font>', st["td"]))
            elif ci == score_col and pd.notna(pd.to_numeric(value, errors="coerce")):
                colour = utils.score_colour(value).hexval().replace("0x", "#")
                cells.append(Paragraph(f'<font color="{colour}"><b>{text}</b></font>', st["td"]))
            else:
                cells.append(Paragraph(text, st["td"]))
        out.append(cells)
    table = Table(out, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), ru.NAVY),
        ("BOX", (0, 0), (-1, -1), 0.5, ru.BLUE),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [ru.WHITE, colors.HexColor("#F2F2F2")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))
    return table


def _loe_chart(drug: str, arbitrage: pd.DataFrame, width=5.8 * inch):
    """The loss-of-exclusivity bar chart by jurisdiction, drawn with reportlab graphics (None if there is
    nothing to plot). Each bar runs from this year to the jurisdiction's LOE, coloured by arbitrage signal."""
    df = arbitrage[arbitrage["Jurisdiction"] != "US"].copy()
    df["LOE"] = pd.to_numeric(df["Product LOE (Year)"], errors="coerce")
    df = df.dropna(subset=["LOE"]).sort_values("LOE")
    if df.empty:
        return None

    today = date.today().year
    last = max(int(df["LOE"].max()), today + 1)
    left, legend_w, row_h, top, bottom = 72, 118, 24, 30, 30
    plot_w = width - left - legend_w
    height = top + row_h * len(df) + bottom
    drawing = Drawing(width, height)

    def x_of(year):
        return left + (year - today) / (last - today) * plot_w

    drawing.add(String(left + plot_w / 2, height - 14, f"{drug} — Loss of Exclusivity by Jurisdiction",
                       fontName="Helvetica-Bold", fontSize=9, textAnchor="middle"))

    # year axis
    drawing.add(Line(left, bottom, left + plot_w, bottom, strokeColor=colors.HexColor("#444444"), strokeWidth=0.8))
    step = max(1, math.ceil((last - today) / 6))
    for year in range(today, last + 1, step):
        drawing.add(Line(x_of(year), bottom - 3, x_of(year), bottom, strokeColor=colors.HexColor("#444444"), strokeWidth=0.8))
        drawing.add(String(x_of(year), bottom - 12, str(year), fontName="Helvetica", fontSize=7, textAnchor="middle"))
    drawing.add(String(left + plot_w / 2, 4, "Year", fontName="Helvetica", fontSize=8, textAnchor="middle"))

    # bars, earliest LOE at the bottom
    for i, (_, row) in enumerate(df.iterrows()):
        y = bottom + i * row_h
        loe = int(row["LOE"])
        fill = colors.HexColor("#" + utils.SIGNAL_HEX.get(row["Arbitrage Signal"], "BDC3C7"))
        drawing.add(Rect(left, y + 3, x_of(loe) - left, row_h - 6, fillColor=fill, strokeColor=colors.white, strokeWidth=0.8))
        drawing.add(String(left - 5, y + row_h / 2 - 3, utils.jurisdiction_name(row["Jurisdiction"]),
                           fontName="Helvetica", fontSize=8, textAnchor="end"))
        drawing.add(String(x_of(loe) - 4, y + row_h / 2 - 3, str(loe), fontName="Helvetica-Bold", fontSize=7,
                           fillColor=colors.white, textAnchor="end"))

    # today marker and legend
    drawing.add(Line(left, bottom, left, bottom + row_h * len(df), strokeColor=utils.RED, strokeWidth=1.2, strokeDashArray=[4, 3]))
    legend_x = left + plot_w + 14
    for i, (signal, hex_colour) in enumerate(utils.SIGNAL_HEX.items()):
        y = height - top - 6 - i * 11
        drawing.add(Rect(legend_x, y, 8, 7, fillColor=colors.HexColor("#" + hex_colour), strokeColor=None))
        drawing.add(String(legend_x + 12, y, signal, fontName="Helvetica", fontSize=6.5))
    drawing.hAlign = "CENTER"
    return drawing


def _narrative_flowables(narrative: str, st: dict) -> list:
    """The analysis text as flowables: a sub-heading per section header, a paragraph per block."""
    parts = re.split("(" + "|".join(re.escape(h) for h in NARRATIVE_HEADERS) + ")", narrative)
    out = []
    for part in (p.strip() for p in parts):
        if not part:
            continue
        out.append(Paragraph(f"<b>{ru.esc(part.title())}</b>", st["h2"]) if part in NARRATIVE_HEADERS
                   else Paragraph(ru.esc(part), st["body"]))
    return out


# ─────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────

def build_report(drug: str, shortlisted: pd.DataFrame, arbitrage: pd.DataFrame, rationale: str, output_path: str) -> str:
    """Write the PDF to *output_path* and return it."""
    shortlisted = utils.prepare_shortlist(shortlisted)
    st = _styles()
    width = letter[0] - 40 * mm

    story = [
        Spacer(1, 40),
        Paragraph(ru.esc(drug), st["title"]),
        HRFlowable(width="100%", thickness=2, color=ru.NAVY, spaceAfter=6),
        Paragraph("Shortlisted Secondary Patents &amp; Geographic Arbitrage Analysis", st["body"]),
        Paragraph(f"Generated {datetime.now().strftime('%B %d, %Y')}", st["meta"]),
        Spacer(1, 20),
    ]

    # Dimension IV badge
    if not arbitrage.empty:
        score, rating = arbitrage["Dimension IV Score"].iloc[0], arbitrage["Dimension IV Rating"].iloc[0]
        badge = Table([[Paragraph("<b>Dimension IV Score</b>", st["th"]), Paragraph(f"<b>{ru.esc(score)}  —  {ru.esc(rating)}</b>", st["th"])]],
                      colWidths=[width * 0.3, width * 0.7])
        badge.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), ru.NAVY),
            ("BACKGROUND", (1, 0), (1, 0), colors.HexColor("#" + utils.RATING_HEX.get(str(rating), "BDC3C7"))),
            ("ALIGN", (0, 0), (-1, 0), "CENTER"), ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, 0), 6), ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
            ("BOX", (0, 0), (-1, -1), 0.5, ru.BLUE),
        ]))
        story += [badge, Spacer(1, 10)]

    # Shortlisted patents
    columns = ["Patent Number", "Jurisdiction", "Step 1 Claim Category", "Controlling Patent Expiry Year", "Expiry Gap (Years)", "Tag"]
    story += [Paragraph("Shortlisted Secondary Patents", st["h1"]),
              _table(columns, shortlisted[columns].values.tolist(), [width / len(columns)] * len(columns), st), Spacer(1, 10)]

    # Geographic arbitrage map
    if not arbitrage.empty:
        columns = ["Jurisdiction", "Product LOE (Year)", "Gap vs US (Years)", "Gap vs Longest LOE (Years)", "Key Protection Gap",
                   "Arbitrage Score", "Arbitrage Signal"]
        story += [Paragraph("Geographic Arbitrage Map", st["h1"]),
                  _table(columns, arbitrage[columns].values.tolist(), [width * w for w in (0.12, 0.13, 0.12, 0.13, 0.24, 0.11, 0.15)], st,
                         signal_col=6, score_col=5),
                  Spacer(1, 10)]
        chart = _loe_chart(drug, arbitrage)
        if chart:
            story += [chart, Spacer(1, 8)]

    # Analysis
    story += [Paragraph("Analysis", st["h1"]), HRFlowable(width="100%", thickness=1, color=ru.NAVY, spaceAfter=6),
              *_narrative_flowables(generate_narrative(drug, shortlisted, arbitrage, rationale), st)]

    # Patent expiry summary
    columns = ["Patent Number", "Jurisdiction", "Step 1 Claim Category", "Controlling Patent Expiry Year", "Expiry Gap (Years)", "PTE (months)"]
    story += [PageBreak(), Paragraph("Patent Expiry Summary", st["h1"]),
              HRFlowable(width="100%", thickness=1, color=ru.NAVY, spaceAfter=6),
              _table(columns, shortlisted[columns].values.tolist(), [width / len(columns)] * len(columns), st),
              Spacer(1, 10), Paragraph("<i>Auto-generated using Gemini.</i>", st["footer"])]

    SimpleDocTemplate(output_path, pagesize=letter, topMargin=18 * mm, bottomMargin=14 * mm, leftMargin=20 * mm, rightMargin=20 * mm,
                      title=f"{drug} — {config.DIMENSION_GLOBAL_LAUNCH}", author="IP Pipeline").build(story)
    return str(Path(output_path).resolve())


def report_for_drug(drug_name: str, shortlisted: pd.DataFrame, arbitrage: pd.DataFrame, rationale: str) -> dict:
    """Build the report in a temporary directory and upload it. Called at the end of every
    global_launch_sequencing_and_geographic_arbitrage run. Returns {"drug_name", "gcs_uri", "archive_uri"}."""
    return ru.publish_report(drug_name, config.DIMENSION_GLOBAL_LAUNCH, lambda path: build_report(drug_name, shortlisted, arbitrage, rationale, path))
