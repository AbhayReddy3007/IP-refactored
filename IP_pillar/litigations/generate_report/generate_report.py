"""
generate_report.py
──────────────────
Litigation Analysis PDF report (2 pages), built from the run's own results (nothing is read back from
BigQuery): the drug's rows of the litigation table.

    Page 1: outcome strip · Executive Summary · Innovator Analysis (with Google Search research)
    Page 2: Drug-Level Analysis · Key Risks and Strategic Outlook

Gemini writes the text. A drug with no cases gets a short one-page report without Gemini. The report is
uploaded to gs://{REPORT_GCS_BUCKET}/{REPORT_GCS_PATH}/:
    {drug}/{GCS_IP_SUBFOLDER}/Litigation_Analysis.pdf
    {drug}/{GCS_IP_SUBFOLDER}/archive/Litigation_Analysis+{timestamp}.pdf
(the file name is config.REPORT_NAME_LITIGATION)

    report_for_drug(drug, rows) -> {"drug_name", "gcs_uri", "archive_uri"}
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Dict, List

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import HRFlowable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from ... import config, report_utils as ru
from ...gemini_utils import gemini_generate

logger = logging.getLogger(__name__)

MAX_PAGE_WORDS = 600

# litigation table column -> the name this report uses
_COLUMNS = {
    "drug_name": "Drug Name", "innovator": "Innovator", "brand_names": "Brand Names",
    "patent_number": "Patent Number", "filing_date": "Filing Date", "case_type": "Case Type",
    "challenger": "Challenger", "case_number": "Case Number", "court": "Court",
    "status": "Status", "outcome": "Outcome",
}

# ── Outcome normalisation ────────────────────────────────────────────────────── #
OUTCOME_MAP = {
    "innovator win":       "Won",
    "win":                 "Won",
    "won":                 "Won",
    "prevailed":           "Won",
    "challenger win":      "Lost",
    "lost":                "Lost",
    "invalidated":         "Lost",
    "settled":             "Settled",
    "settlement":          "Settled",
    "pending":             "Pending",
    "ongoing":             "Pending",
    "active":              "Pending",
    "dismissed":           "Dismissed",
    "voluntary dismissal": "Dismissed",
    "resolved":            "Resolved",
    "consent judgment":    "Resolved",
    "license":             "Resolved",
    "licensed":            "Resolved",
}


def simplify_outcome(raw) -> str:
    """One of the OUTCOME_MAP labels for a free-text outcome ("Pending" when there is none)."""
    if pd.isna(raw) or str(raw).strip() == "":
        return "Pending"
    key = str(raw).strip().lower()
    if key in OUTCOME_MAP:
        return OUTCOME_MAP[key]
    for pattern, label in OUTCOME_MAP.items():
        if pattern in key:
            return label
    return key.split()[0].capitalize()


def cases_frame(rows: List[Dict]) -> pd.DataFrame:
    """The drug's cases (rows with a case type) with this report's column names and a simplified Outcome."""
    df = pd.DataFrame(rows).rename(columns=_COLUMNS)
    for column in _COLUMNS.values():
        if column not in df.columns:
            df[column] = ""
        df[column] = df[column].fillna("")
    df = df[df["Case Type"].astype(str).str.strip() != ""].copy()
    df["Outcome"] = df["Outcome"].where(df["Outcome"] != "", df["Status"]).apply(simplify_outcome)
    return df.reset_index(drop=True)


# ─────────────────────────────────────────────
# Text (Gemini)
# ─────────────────────────────────────────────

def research_innovator(company: str, drug: str) -> str:
    """Gemini + Google Search research on the innovator's filing history and litigation record ('' if it fails)."""
    prompt = (
        f"Research pharmaceutical company '{company}' and provide:\n"
        f"1. Patent filing history — total patent portfolio size, filings per year trend, "
        f"key therapeutic areas, lifecycle management and evergreening patterns\n"
        f"2. Litigation track record — win rate in Hatch-Waxman/ANDA cases, notable wins and losses, "
        f"preferred courts, litigation duration patterns\n"
        f"3. Recent patent activity for: {drug}\n"
        f"4. Industry reputation — how their IP strategy compares to peers like Pfizer, Roche, Novartis\n\n"
        f"Be specific with numbers, dates, case names. 250 words max. Plain text, no markdown."
    )
    try:
        text = (gemini_generate(prompt, config.GEMINI_TEXT_MODEL, google_search=True).text or "").strip()
    except Exception:
        logger.exception("[REPORT] Innovator research failed for %r", company)
        return ""
    logger.info("[REPORT] %s: %d chars of research", company, len(text))
    return text


def _trim(text: str, limit: int) -> str:
    """*text* cut to *limit* words, at a sentence end where one is near."""
    words = text.split()
    if len(words) <= limit:
        return text
    cut = " ".join(words[:limit])
    for mark in ".!?":
        i = cut.rfind(mark)
        if i > len(cut) * 0.7:
            return cut[: i + 1]
    return cut + "."


def generate_page1(drug: str, df: pd.DataFrame, innovator: str, research: str) -> str:
    """Executive summary and innovator analysis."""
    stats = json.dumps({
        "total_cases": len(df),
        "outcomes":    df["Outcome"].value_counts().to_dict(),
        "case_types":  df["Case Type"].value_counts().to_dict(),
    }, indent=2)
    case_lines = [f"{r['Drug Name']} | {r['Case Number']} | {r['Challenger']} | {r['Outcome']}" for _, r in df.iterrows()]

    prompt = f"""You are a senior pharmaceutical patent litigation analyst writing page 1 of a
detailed board-level report. This must be substantive and insightful — NOT a summary of bullet points.
Write in flowing, professional prose. Plain text only — no markdown, no bullets, no asterisks.
Use ALL CAPS for section headers only.

PORTFOLIO DATA:
{stats}

CASES:
{chr(10).join(case_lines)}

INNOVATOR INTELLIGENCE:
COMPANY: {innovator} | DRUGS: {drug}
WEB RESEARCH:
{research[:600]}

═══════════════════════════════════════════════
WRITE EXACTLY THESE SECTIONS — fill the full page:
═══════════════════════════════════════════════

EXECUTIVE SUMMARY
Two substantial paragraphs (~200 words total).
Paragraph 1: Portfolio landscape — total cases analysed,
outcome breakdown with exact percentages (e.g. "innovator prevailed in 65% of cases"),
the most active challengers and their success rates, and which case types dominate.
Paragraph 2: Strategic significance — what this litigation record reveals about the
strength of the innovator's patent estate, whether the trend favours innovators or
challengers, and the single most important strategic implication for decision-makers.

INNOVATOR ANALYSIS
Write a dedicated subsection with the COMPANY NAME in ALL CAPS.
2 well-developed paragraphs (~200 words):

Paragraph 1 — FILING HISTORY & IP STRATEGY: Total patents filed (from web research),
filing trends over recent years, key jurisdictions (US/EP/PCT), lifecycle management
approach — do they file continuation patents, divisionals, new formulations near expiry?
How does their filing pattern for the drug in this portfolio compare to industry norms?
Cite specific numbers and years from the web research.

Paragraph 2 — LITIGATION TRACK RECORD & POSITIONING: Win/loss record from the case data
(cite specific case numbers), preferred litigation venues, how they compare to industry
peers in IP aggressiveness, their reputation for settlement vs trial, and any notable
strategic moves (e.g. authorized generics, licensing deals, IPR defences).
Reference specific cases from the data.

═══════════════════════════════════════════════
RULES:
- Write ~550 words total. This fills one A4 page at 9.5pt font.
- Every paragraph must contain specific numbers, case references, or dates.
- Do NOT write generic statements like "the company has a strong patent portfolio" without evidence.
- Start directly with EXECUTIVE SUMMARY. No preamble, no introduction line.
"""
    return _trim((gemini_generate(prompt, config.GEMINI_TEXT_MODEL).text or "").strip(), MAX_PAGE_WORDS)


def generate_page2(drug: str, df: pd.DataFrame, innovator: str) -> str:
    """Drug-level analysis, key risks and strategic outlook."""
    cols = [c for c in ("Patent Number", "Case Number", "Case Type", "Challenger", "Court", "Outcome") if c in df.columns]
    block = f"DRUG: {drug} | INNOVATOR: {innovator} | CASES: {len(df)}\n{df[cols].to_string(index=False)}"

    prompt = f"""You are a senior pharmaceutical patent litigation analyst writing page 2 of a
detailed board-level report. This must be substantive — rich with case-specific analysis.
Write in flowing, professional prose. Plain text only — no markdown, no bullets, no asterisks.
Use ALL CAPS for section headers only.

DRUG-LEVEL DATA:
{block}

═══════════════════════════════════════════════
WRITE EXACTLY THESE SECTIONS — fill the full page:
═══════════════════════════════════════════════

DRUG-LEVEL ANALYSIS
Write a subsection with the DRUG NAME in ALL CAPS as header.
1-2 detailed paragraphs (~380 words). Cover:
- Case outcomes in detail: which specific cases (cite case numbers) were won, lost,
  or settled, and the key reasoning behind each outcome
- Challenger landscape: who challenged, their litigation strategy (ANDA, IPR, declaratory
  judgment), and whether they succeeded
- Patent strength assessment: based on the outcomes, how defensible is this drug's
  patent estate? Were any patents invalidated? On what grounds?
- Court patterns: any notable jurisdictional choices or venue strategies

KEY RISKS AND STRATEGIC OUTLOOK
Two paragraphs (~150 words total).
Paragraph 1: Identify the specific risks — which patents
are weakest, which challengers are most likely to succeed, and any upcoming expirations
or pending cases that could change the landscape. Be specific about timelines.
Paragraph 2: Strategic recommendations — what the innovator should do (strengthen
specific patents, settle particular cases, file continuations, pursue authorized generics)
and what generic challengers should target. End with a forward-looking assessment.

═══════════════════════════════════════════════
RULES:
- Write ~550 words total. This fills one A4 page at 9.5pt font.
- Every paragraph must cite specific case numbers and outcomes.
- Do NOT write generic statements. Every claim must be grounded in the data.
- Start directly with the drug header. No preamble.
"""
    return _trim((gemini_generate(prompt, config.GEMINI_TEXT_MODEL).text or "").strip(), MAX_PAGE_WORDS)


# ─────────────────────────────────────────────
# PDF
# ─────────────────────────────────────────────

def _render(text: str, story: list, s_h2, s_body) -> None:
    """The text as flowables: ALL-CAPS lines are sub-headings, the rest paragraphs."""
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        is_header = (
            re.match(r"^[0-9]+\.\s+[A-Z\s&]+$", line)
            or (line.isupper() and 4 < len(line) < 80 and not line.startswith("["))
        )
        if is_header:
            story.append(Spacer(1, 0.06 * cm))
            story.append(Paragraph(ru.esc(line), s_h2))
        else:
            story.append(Paragraph(ru.esc(line), s_body))


def _brands(rows: List[Dict]) -> str:
    """The drug's brand names, comma separated ('' if none were looked up)."""
    for row in rows:
        if row.get("brand_names"):
            try:
                return ", ".join(json.loads(row["brand_names"]))
            except (TypeError, ValueError):
                return str(row["brand_names"])
    return ""


def build_pdf(drug: str, rows: List[Dict], df: pd.DataFrame, innovator: str, page1: str, page2: str, output_path: str) -> None:
    """The report PDF; with no cases (*page1* empty) a one-page note."""
    doc = SimpleDocTemplate(
        output_path, pagesize=A4,
        leftMargin=1.8 * cm, rightMargin=1.8 * cm, topMargin=1.6 * cm, bottomMargin=1.3 * cm,
        title=f"{drug} — {config.DIMENSION_LITIGATION}", author="IP Pipeline",
    )
    styles = getSampleStyleSheet()
    width = A4[0] - 3.6 * cm
    s_title = ParagraphStyle("LitTitle", parent=styles["Title"], fontSize=18, textColor=colors.HexColor("#1a237e"),
                             spaceAfter=3, alignment=TA_CENTER)
    s_h1 = ParagraphStyle("LitH1", parent=styles["Heading1"], fontSize=11.5, textColor=colors.HexColor("#1a237e"),
                          spaceBefore=6, spaceAfter=3)
    s_h2 = ParagraphStyle("LitH2", parent=styles["Heading2"], fontSize=10, textColor=colors.HexColor("#283593"),
                          spaceBefore=5, spaceAfter=2)
    s_body = ParagraphStyle("LitBody", parent=styles["Normal"], fontSize=9.5, leading=13, alignment=TA_JUSTIFY, spaceAfter=4)
    s_meta = ParagraphStyle("LitMeta", parent=styles["Normal"], fontSize=7.5, textColor=colors.HexColor("#78909c"), alignment=TA_CENTER)
    s_footer = ParagraphStyle("LitFooter", parent=styles["Normal"], fontSize=6.5, textColor=colors.HexColor("#9e9e9e"), alignment=TA_CENTER)

    patents_searched = len({row["patent_number"] for row in rows if row.get("patent_number")})
    brands = _brands(rows)
    story: list = [
        Paragraph("PHARMACEUTICAL LITIGATION ANALYSIS", s_title),
        HRFlowable(width=width, thickness=2, color=colors.HexColor("#1a237e")),
        Spacer(1, 0.08 * cm),
        Paragraph(
            f"Drug: {ru.esc(drug)}"
            + (f"&nbsp;&nbsp;|&nbsp;&nbsp;Brands: {ru.esc(brands)}" if brands else "")
            + f"&nbsp;&nbsp;|&nbsp;&nbsp;Innovator: {ru.esc(innovator or 'Unknown')}"
            f"&nbsp;&nbsp;|&nbsp;&nbsp;Patents searched: {patents_searched}"
            f"&nbsp;&nbsp;|&nbsp;&nbsp;Cases: {len(df)}"
            f"&nbsp;&nbsp;|&nbsp;&nbsp;Challengers: {df['Challenger'].replace('', pd.NA).nunique()}", s_meta),
        Spacer(1, 0.12 * cm),
    ]

    if df.empty:
        story.append(Paragraph(
            f"No patent litigation or court cases were found for the {patents_searched} patent(s) searched.", s_body))
    else:
        counts = df["Outcome"].value_counts()
        table = Table([[str(o) for o in counts.index], [f"{c} ({c / len(df) * 100:.0f}%)" for c in counts.values]],
                      colWidths=[width / len(counts)] * len(counts))
        table.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), colors.HexColor("#37474f")),
            ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
            ("BACKGROUND",    (0, 1), (-1, 1), colors.HexColor("#eceff1")),
            ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, -1), 7.5),
            ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("GRID",          (0, 0), (-1, -1), 0.3, colors.HexColor("#b0bec5")),
        ]))
        story += [table, Spacer(1, 0.12 * cm)]
        _render(page1, story, s_h2, s_body)
        story += [
            PageBreak(),
            Paragraph("DRUG-LEVEL ANALYSIS &amp; STRATEGIC OUTLOOK", s_h1),
            HRFlowable(width=width, thickness=1.5, color=colors.HexColor("#1a237e")),
            Spacer(1, 0.08 * cm),
        ]
        _render(page2, story, s_h2, s_body)

    story += [
        Spacer(1, 0.12 * cm),
        HRFlowable(width=width, thickness=0.3, color=colors.HexColor("#cfd8dc")),
        Paragraph(
            f"Generated by {config.GEMINI_TEXT_MODEL} with Google Search"
            f"&nbsp;&nbsp;|&nbsp;&nbsp;Source: {config.BQ_LITIGATION_TABLE}"
            f"&nbsp;&nbsp;|&nbsp;&nbsp;Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
            f"&nbsp;&nbsp;|&nbsp;&nbsp;AI-generated — review by qualified professionals recommended.", s_footer),
    ]
    doc.build(story)


def build_report(drug_name: str, rows: List[Dict], output_path: str) -> str:
    """Write the PDF to *output_path* and return the path."""
    df = cases_frame(rows)
    innovator = next((row["innovator"] for row in rows if row.get("innovator")), "")
    page1 = page2 = ""
    if not df.empty:
        research = research_innovator(innovator, drug_name) if innovator else ""
        page1 = generate_page1(drug_name, df, innovator or "Unknown", research)
        page2 = generate_page2(drug_name, df, innovator or "Unknown")
    build_pdf(drug_name, rows, df, innovator, page1, page2, output_path)
    return output_path


def report_for_drug(drug_name: str, rows: List[Dict]) -> dict:
    """Build the report in a temporary directory and upload it. Called at the end of every
    litigation_analysis run. Returns {"drug_name", "gcs_uri", "archive_uri"}."""
    return ru.publish_report(drug_name, config.REPORT_NAME_LITIGATION, lambda path: build_report(drug_name, rows, path))
