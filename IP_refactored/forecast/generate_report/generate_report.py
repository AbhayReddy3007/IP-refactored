"""
generate_report.py
──────────────────
Forecast Analysis PDF report (about 2 pages), built from the run's own results (nothing is read back from
BigQuery): the scored forecast of the drug (what is written to forecasted_loe) and its existing patents
(the glp_patent_loe rows the forecast started from).

    Header band · Executive Summary · Patent Strategy Insights · Filing Window Analysis ·
    Portfolio Gaps and Opportunities · Risk Assessment

Gemini writes the text from a structured summary of the data. The report is uploaded to
gs://{REPORT_GCS_BUCKET}/{REPORT_GCS_PATH}/:
    {drug}/{GCS_IP_SUBFOLDER}/Forecast Analysis.pdf
    {drug}/{GCS_IP_SUBFOLDER}/archive/Forecast Analysis+{timestamp}.pdf
(the file name is config.DIMENSION_FORECAST)

    report_for_drug(drug, scored, patents) -> {"drug_name", "gcs_uri", "archive_uri"}
"""

import logging
import re
from datetime import datetime
from typing import Dict, List

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Frame, HRFlowable, KeepTogether, PageTemplate, Paragraph, SimpleDocTemplate, Spacer

from ... import config, report_utils as ru
from ...gemini_utils import gemini_generate

logger = logging.getLogger(__name__)

MAX_REPORT_CHARS = 4500   # keeps the text within two pages

# ── Colour palette ────────────────────────────────────────────────────────── #
NAVY       = colors.HexColor("#0D2B55")
ACCENT     = colors.HexColor("#2E86C1")
LIGHT_BLUE = colors.HexColor("#D6EAF8")
GOLD       = colors.HexColor("#D4AC0D")
WHITE      = colors.white
DARK_GREY  = colors.HexColor("#2C3E50")
MID_GREY   = colors.HexColor("#566573")

# ── Styles ────────────────────────────────────────────────────────────────── #
section_heading_style = ParagraphStyle(
    name="ForecastSectionHeading", fontName="Helvetica-Bold", fontSize=11, leading=15,
    textColor=NAVY, spaceBefore=14, spaceAfter=4, leftIndent=0,
)
body_style = ParagraphStyle(
    name="ForecastBodyText", fontName="Helvetica", fontSize=9.5, leading=14,
    textColor=DARK_GREY, alignment=TA_JUSTIFY, spaceAfter=6, leftIndent=2,
)

SECTION_KEYWORDS = [
    "executive summary",
    "patent strategy insights",
    "filing window analysis",
    "portfolio gaps and opportunities",
    "portfolio gap",
    "risk assessment",
]


def is_section_heading(line: str) -> bool:
    low = re.sub(r"^\d+[\.\)]\s*", "", line.lower().strip())
    return any(low == kw or low.startswith(kw) for kw in SECTION_KEYWORDS)


# ─────────────────────────────────────────────
# Data summary for the prompt
# ─────────────────────────────────────────────

def _first(df: pd.DataFrame, column: str):
    """The first non-null value of *column*, or None."""
    if column not in df.columns:
        return None
    values = df[column].dropna().unique()
    return values[0] if len(values) else None


def _build_data_summary(drug_name: str, forecast_df: pd.DataFrame, patents_df: pd.DataFrame) -> str:
    """A structured text summary of the drug's forecast and existing patents for the prompt."""
    lines: List[str] = []

    if not forecast_df.empty:
        lines.append(f"=== FORECAST DATA FOR {drug_name.upper()} ===")
        lines.append(f"Total forecasted patent entries: {len(forecast_df)}")

        for label, column in (
            ("IP Dimension 1 Score", "ip_dimension_1_score"),
            ("Avg Years to Entry (US & EP)", "avg_years_to_entry_us_ep"),
            ("Avg Years to Entry (all jurisdictions)", "avg_years_to_entry"),
            ("Global Phase", "global_phase"),
            ("Company", "company"),
            ("Drug Class", "drug_class"),
        ):
            value = _first(forecast_df, column)
            if value is not None:
                lines.append(f"{label}: {value}")

        for column in ("overall_forecast", "portfolio_gaps", "risk_assessment", "existing_patent_summary"):
            if column in forecast_df.columns:
                values = [v for v in forecast_df[column].dropna().astype(str).unique()
                          if v.strip() and v.lower() not in ("nan", "none")]
                if values:
                    lines.append(f"\n{column.replace('_', ' ').title()}:")
                    lines.append(f"  {values[0][:1500]}")

        lines.append("\n--- Forecast Entries (top 40) ---")
        display_cols = [c for c in (
            "patent_number", "step1_claim_category", "jurisdiction", "likelihood", "filing_window",
            "phase_in_jurisdiction", "controlling_patent_expiry_year", "years_to_entry",
            "rationale", "strategic_purpose", "no_of_forecasted_patents",
        ) if c in forecast_df.columns]
        for _, row in forecast_df.head(40).iterrows():
            lines.append("  " + " | ".join(f"{c}: {row[c]}" for c in display_cols if pd.notna(row.get(c))))

    if not patents_df.empty:
        lines.append(f"\n=== EXISTING PATENTS FOR {drug_name.upper()} ===")
        lines.append(f"Total existing patents: {len(patents_df)}")
        for label, column in (("Jurisdictions", "Jurisdiction"), ("Patent categories", "Step 1 Claim Category"), ("Tags", "Tag")):
            if column in patents_df.columns:
                lines.append(f"{label}: {patents_df[column].value_counts().to_dict()}")
        if "Years to Entry" in patents_df.columns:
            years = pd.to_numeric(patents_df["Years to Entry"], errors="coerce").dropna()
            if not years.empty:
                lines.append(f"Existing YTE range: {years.min()} – {years.max()}")

        lines.append("\n--- Existing Patent Details (first 20) ---")
        show_cols = [c for c in (
            "Patent Number", "Step 1 Claim Category", "Jurisdiction", "Tag", "Filing Date",
            "Controlling Patent Expiry Year", "Years to Entry",
        ) if c in patents_df.columns]
        for _, row in patents_df.head(20).iterrows():
            lines.append("  " + " | ".join(f"{c}: {row[c]}" for c in show_cols if pd.notna(row.get(c))))

    return "\n".join(lines)


# ─────────────────────────────────────────────
# Report text (Gemini)
# ─────────────────────────────────────────────

def _generate_report_text(drug_name: str, data_summary: str) -> str:
    """The report text, written by Gemini from the data summary."""
    prompt = f"""
You are a senior pharma patent strategist producing a concise consulting report.

DRUG: {drug_name}
DATE: {datetime.now().strftime('%Y-%m-%d')}

STRICT RULES:
- Maximum 700–800 words (STRICT)
- Must NOT exceed 2 pages
- No repetition of table data
- Be concise, analytical, and structured
- Write in direct analytical voice — state findings and insights directly
- Do NOT use phrases like "this report analyses", "this report examines", or "in this report"
- Do NOT use markdown bold markers like ** around headings or text
- Section headings should be plain text only
- Include specific numbers from the data (scores, years to entry, patent counts)
- Compare forecasted patents against existing patents where data is available

MANDATORY SECTIONS (use these exact heading titles, plain text):
1. Executive Summary
2. Patent Strategy Insights
3. Filing Window Analysis
4. Portfolio Gaps and Opportunities
5. Risk Assessment

DATA:
{data_summary}
"""
    text = gemini_generate(prompt, config.GEMINI_TEXT_MODEL).text or ""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)      # strip any residual markdown bold / italics
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    return text[:MAX_REPORT_CHARS]


# ─────────────────────────────────────────────
# PDF
# ─────────────────────────────────────────────

def _build_pdf(drug_name: str, report_text: str, output_path: str) -> None:
    """The styled PDF from the report text."""
    header_height = 52

    def draw_page(canvas, doc):
        canvas.saveState()
        w, h = A4
        canvas.setFillColor(NAVY)
        canvas.rect(0, h - header_height, w, header_height, fill=1, stroke=0)
        canvas.setFillColor(GOLD)
        canvas.rect(0, h - header_height - 3, w, 3, fill=1, stroke=0)
        canvas.setFont("Helvetica-Bold", 16)
        canvas.setFillColor(WHITE)
        canvas.drawCentredString(w / 2, h - header_height + 22, f"{drug_name} — Forecast Report")
        canvas.setFont("Helvetica", 9)
        canvas.setFillColor(LIGHT_BLUE)
        canvas.drawCentredString(w / 2, h - header_height + 8, "Patent Landscape & Strategic Analysis")
        canvas.setStrokeColor(ACCENT)
        canvas.setLineWidth(0.8)
        canvas.line(18 * mm, 15 * mm, w - 18 * mm, 15 * mm)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(MID_GREY)
        canvas.drawCentredString(w / 2, 9 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        output_path, pagesize=A4,
        rightMargin=18 * mm, leftMargin=18 * mm, topMargin=22 * mm, bottomMargin=22 * mm,
        title=f"{drug_name} — {config.DIMENSION_FORECAST}", author="IP Pipeline",
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height - header_height - 6, id="main",
                  leftPadding=0, rightPadding=0, topPadding=4, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="report_page", frames=[frame], onPage=draw_page)])

    content: list = [Spacer(1, 6)]
    section_items: list = []

    def flush_section() -> None:
        if section_items:
            content.append(KeepTogether(section_items[:3]))       # a heading stays with its first lines
            content.extend(section_items[3:])
            section_items.clear()

    for line in report_text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        plain = re.sub(r"^\d+[\.\)]\s*", "", stripped)
        if is_section_heading(stripped):
            flush_section()
            section_items.append(HRFlowable(width="100%", thickness=1.5, color=ACCENT, spaceAfter=4, spaceBefore=8))
            section_items.append(Paragraph(ru.esc(plain.title()), section_heading_style))
        else:
            section_items.append(Paragraph(ru.esc(plain), body_style))
    flush_section()
    doc.build(content)


def build_report(drug_name: str, scored: List[Dict], patents: pd.DataFrame, output_path: str) -> str:
    """Write the PDF to *output_path* and return the path."""
    summary = _build_data_summary(drug_name, pd.DataFrame(scored), patents)
    logger.info("[REPORT] %s: data summary %d chars - writing the report text", drug_name, len(summary))
    _build_pdf(drug_name, _generate_report_text(drug_name, summary), output_path)
    return output_path


def report_for_drug(drug_name: str, scored: List[Dict], patents: pd.DataFrame) -> dict:
    """Build the report in a temporary directory and upload it. Called at the end of every
    forecast_analysis run. Returns {"drug_name", "gcs_uri", "archive_uri"}."""
    return ru.publish_report(drug_name, config.DIMENSION_FORECAST,
                             lambda path: build_report(drug_name, scored, patents, path))
