"""
generate_report.py
──────────────────
Blocking Analysis PDF report (2 pages), written by Gemini from the patents the blocking analysis
just produced (primary_market_entry_horizon passes them in; nothing is read back from BigQuery):

    1. Overall Analysis   — totals, category breakdown, headline takeaway
    2. Blocking Patents   — key patents with reasoning; the controlling patent of every jurisdiction
    3. Non-Blocking Patents — patterns, the step each exited at, why they don't block

It is uploaded next to the other IP reports, to gs://{REPORT_GCS_BUCKET}/{REPORT_GCS_PATH}/:
    {drug}/{GCS_IP_SUBFOLDER}/Blocking Analysis.pdf
    {drug}/{GCS_IP_SUBFOLDER}/archive/Blocking Analysis+{timestamp}.pdf
(the file name is config.DIMENSION_BLOCKING_ANALYSIS)

    report_for_drug(drug_name, patents) -> {"gcs_uri", "archive_uri"}
"""

import logging
import re
from collections import defaultdict
from datetime import datetime
from typing import Dict, List

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .. import config, report_utils as ru
from ..gemini_utils import gemini_api_call

logger = logging.getLogger(__name__)

_DARK_BLUE  = colors.HexColor("#1B2A4A")
_MED_BLUE   = colors.HexColor("#2C5F8A")
_RED        = colors.HexColor("#B3261E")
_GREEN      = colors.HexColor("#1A7A3A")
_GREY       = colors.HexColor("#5F6368")
_LIGHT_GREY = colors.HexColor("#F1F3F4")
_WHITE      = colors.white

# Bold patent numbers in the text: two-letter country code, 6+ digits, kind code.
_PATENT_NUMBER = re.compile(r"\b((?:US|EP|WO|CN|IN|BR|AU|RU|RK|CA|TW|MX|JP)\d{6,}[A-Z]\d*)\b")


def _build_styles() -> dict:
    base = getSampleStyleSheet()
    return {
        "title":    ParagraphStyle("T",  parent=base["Title"],    fontSize=20, leading=24, textColor=_DARK_BLUE, spaceAfter=2),
        "subtitle": ParagraphStyle("ST", parent=base["Heading2"], fontSize=13, leading=16, textColor=_MED_BLUE, spaceAfter=4),
        "meta":     ParagraphStyle("M",  parent=base["Normal"],   fontSize=8.5, leading=11, textColor=_GREY, spaceAfter=8),
        "heading":  ParagraphStyle("H",  parent=base["Heading2"], fontSize=12, leading=15, textColor=_DARK_BLUE, spaceBefore=10, spaceAfter=4),
        "body":     ParagraphStyle("B",  parent=base["Normal"],   fontSize=9, leading=12.5, alignment=TA_JUSTIFY, spaceAfter=4),
        "th":       ParagraphStyle("TH", parent=base["Normal"],   fontSize=8, leading=10, textColor=_WHITE, alignment=TA_CENTER),
        "td":       ParagraphStyle("TD", parent=base["Normal"],   fontSize=8, leading=10, alignment=TA_CENTER),
        "td_left":  ParagraphStyle("TDL", parent=base["Normal"],  fontSize=8, leading=10, alignment=TA_LEFT),
        "footer":   ParagraphStyle("F",  parent=base["Normal"],   fontSize=7, leading=9, textColor=_GREY, alignment=TA_CENTER),
    }


# ─────────────────────────────────────────────
# Data helpers
# ─────────────────────────────────────────────

def _g(patent: Dict, key: str, default="N/A"):
    """patent[key], or *default* when it is missing or blank."""
    value = patent.get(key)
    if value is None or str(value).strip().lower() in ("", "nan", "none", "n/a"):
        return default
    return value


def _parse_filing_date(value):
    """A sortable datetime for a Filing Date value; datetime.min when it is missing or unparseable,
    so a patent with a real filing date always outranks one without."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return datetime.min
    text = str(value).strip()
    if not text or text.lower() in ("unknown", "n/a", "none", "nan", "not yet granted"):
        return datetime.min
    if re.match(r"^\d{4}$", text):
        return datetime(int(text), 1, 1)
    parsed = pd.to_datetime(text, errors="coerce")
    return parsed.to_pydatetime() if pd.notna(parsed) else datetime.min


def _completeness(patent: Dict) -> int:
    """Tie-break between patents filed on the same date: the one with more usable downstream data."""
    return sum(_g(patent, key) != "N/A" for key in (
        "approval_date", "estimated_approval_year", "exclusivity_year", "controlling_patent_expiry_year"))


def find_controlling_patents(blocking: List[Dict]) -> Dict[str, Dict]:
    """{jurisdiction: the blocking patent with the latest filing date}, the patent whose expiry
    effectively drives that jurisdiction's Years to Entry."""
    by_jurisdiction: Dict[str, List[Dict]] = defaultdict(list)
    for p in blocking:
        jurisdiction = str(_g(p, "jurisdiction", "")).strip()
        if jurisdiction:
            by_jurisdiction[jurisdiction].append(p)

    controlling = {}
    for jurisdiction, patents in by_jurisdiction.items():
        dated = [(p, _parse_filing_date(_g(p, "filing_date"))) for p in patents]
        latest = max(d for _, d in dated)
        controlling[jurisdiction] = max((p for p, d in dated if d == latest), key=_completeness)
    return controlling


def _exit_step(patent: Dict) -> str:
    """The step at which a NON-BLOCKING patent left the analysis."""
    if str(_g(patent, "step3_is_technical_barrier")).lower() == "false":
        return "Step 3"
    if str(_g(patent, "step4_is_blocking_indicator")).lower() == "false":
        return "Step 4"
    if str(_g(patent, "step4_is_blocking_indicator")).lower() == "true":
        return "Step 5"
    return "Step 2"


def analysed_patents(patents: List[Dict]) -> List[Dict]:
    """The patents the analysis classified (BLOCKING / NON-BLOCKING); skipped and failed ones are left out."""
    return [p for p in patents if _g(p, "tag") in ("BLOCKING", "NON-BLOCKING")]


# ─────────────────────────────────────────────
# Gemini analysis
# ─────────────────────────────────────────────

ANALYSIS_PROMPT = """You are a senior pharmaceutical patent analyst writing a concise
executive report on the blocking patent landscape for {drug_name}.

Below is the complete patent analysis data from our automated pipeline.

{patent_summary}

─────────────────────────────────────────────
WRITE A REPORT WITH EXACTLY 3 SECTIONS:
─────────────────────────────────────────────

SECTION 1 — OVERALL ANALYSIS (1 short paragraph)
Write a concise executive summary. State the total patents analysed,
how many are blocking vs non-blocking, and the key claim categories involved.
End with a one-sentence headline takeaway about the strength of the patent
protection for this drug.

SECTION 2 — BLOCKING PATENTS (2-4 paragraphs)
Analyse the blocking patents. You do NOT need to list every patent — highlight
the most significant ones and explain WHY they block. Group your analysis by
claim category where useful. For each key patent you mention, include:
  - The patent number and jurisdiction
  - What it protects and why it creates a barrier
  - The scientific/regulatory evidence supporting the blocking classification
For EVERY jurisdiction listed under "CONTROLLING PATENT BY JURISDICTION" above,
explicitly identify that patent as the controlling patent for that
jurisdiction, state its filing date and why it is controlling (it is the
blocking patent with the latest filing date in that jurisdiction), and explain
what its Controlling Patent Expiry Year / Exclusivity Year / Years to Entry
mean for that jurisdiction's freedom-to-operate timeline. Do this even if the
controlling patent isn't otherwise one of the "most significant" patents you'd
naturally highlight — it must be named for every jurisdiction.
Focus on insight, not listing. What makes this patent estate defensible?

SECTION 3 — NON-BLOCKING PATENTS (1-2 paragraphs)
Summarise patterns in the non-blocking patents. At which analysis steps did
most of them exit? What categories are they in? Why don't they block?
You do NOT need to list every non-blocking patent — describe the patterns
and highlight any notable ones that came close to blocking but didn't make it.

─────────────────────────────────────────────
FORMATTING RULES:
─────────────────────────────────────────────
- Start each section with the section header on its own line: "SECTION 1: OVERALL ANALYSIS" etc.
- Use plain text paragraphs. No bullet points, no markdown, no tables.
- Keep the TOTAL output under 800 words. This must fit on 2 PDF pages.
- Write in analytical, professional tone — like a patent strategy memo.
- When referencing patents, use the format: US10159713B2 (US) or EP3296310B1 (EP).
- Use only the data above; do not invent facts.
- Do NOT include any introductory preamble or sign-off. Start directly with SECTION 1.
"""


def build_patent_summary(patents: List[Dict]) -> str:
    """A compact text summary of the analysed patents for the Gemini prompt."""
    blocking     = [p for p in patents if _g(p, "tag") == "BLOCKING"]
    non_blocking = [p for p in patents if _g(p, "tag") == "NON-BLOCKING"]
    controlling_by_jurisdiction = find_controlling_patents(blocking)
    controlling_numbers = {str(_g(p, "patent_number")) for p in controlling_by_jurisdiction.values()}

    lines = [f"TOTAL PATENTS: {len(patents)}", f"BLOCKING: {len(blocking)}", f"NON-BLOCKING: {len(non_blocking)}"]

    category_counts = defaultdict(lambda: {"blocking": 0, "non_blocking": 0})
    for p in patents:
        key = "blocking" if _g(p, "tag") == "BLOCKING" else "non_blocking"
        category_counts[_g(p, "claim_category", "Unknown")][key] += 1
    lines.append("\nCATEGORY BREAKDOWN:")
    for category, counts in sorted(category_counts.items()):
        lines.append(f"  {category}: {counts['blocking']} blocking, {counts['non_blocking']} non-blocking")

    # The controlling patent is named explicitly so Section 2 always names the right one.
    lines.append("\nCONTROLLING PATENT BY JURISDICTION:")
    if controlling_by_jurisdiction:
        for jurisdiction, p in sorted(controlling_by_jurisdiction.items()):
            lines.append(
                f"  {jurisdiction}: {_g(p, 'patent_number')} (filed {_g(p, 'filing_date')}) — "
                f"Blocking Category: {_g(p, 'blocking_category')}, "
                f"Controlling Patent Expiry Year: {_g(p, 'controlling_patent_expiry_year')}, "
                f"Exclusivity Year: {_g(p, 'exclusivity_year')}, Years to Entry: {_g(p, 'years_to_entry')}")
    else:
        lines.append("  (none — no blocking patents with a resolvable filing date)")

    lines.append("\n--- BLOCKING PATENTS ---")
    for p in blocking:
        number = _g(p, "patent_number")
        header = f"\n  PATENT: {number} ({_g(p, 'jurisdiction')})"
        if str(number) in controlling_numbers:
            header += "  *** CONTROLLING PATENT for this jurisdiction ***"
        lines += [
            header,
            f"  Claim Category: {_g(p, 'claim_category')}",
            f"  Blocking Category: {_g(p, 'blocking_category')}",
            f"  Filed: {_g(p, 'filing_date')} | Granted: {_g(p, 'grant_date')}",
            f"  Reason: {_g(p, 'reason')}",
        ]
        evidence = _g(p, "step3_evidence_summary")
        if evidence != "N/A":
            lines.append(f"  Scientific Evidence: {str(evidence)[:300]}")
        if str(number) in controlling_numbers:
            lines.append(f"  Controlling Patent Expiry Year: {_g(p, 'controlling_patent_expiry_year')} | "
                         f"Exclusivity Year: {_g(p, 'exclusivity_year')} | Years to Entry: {_g(p, 'years_to_entry')}")

    lines.append("\n--- NON-BLOCKING PATENTS ---")
    for p in non_blocking:
        lines += [
            f"\n  PATENT: {_g(p, 'patent_number')} ({_g(p, 'jurisdiction')})",
            f"  Claim Category: {_g(p, 'claim_category')}",
            f"  Filed: {_g(p, 'filing_date')}",
            f"  Exited At: {_exit_step(p)}",
            f"  Reason: {str(_g(p, 'reason'))[:200]}",
        ]
    return "\n".join(lines)


def parse_sections(text: str) -> Dict[str, str]:
    """The three sections of Gemini's reply."""
    sections = {"overall": "", "blocking": "", "non_blocking": ""}
    parts = re.split(r"SECTION\s*\d\s*[:—\-]\s*", text, flags=re.IGNORECASE)
    for key, part in zip(sections, parts[1:]):  # parts[0] is any preamble before the first header
        sections[key] = re.sub(r"^[A-Z\s&\-]+\n", "", part.strip(), count=1).strip()
    return sections


def generate_sections(drug_name: str, patents: List[Dict]) -> Dict[str, str]:
    """Ask Gemini for the report text; a count-based fallback is used if the call fails."""
    prompt = ANALYSIS_PROMPT.format(drug_name=drug_name, patent_summary=build_patent_summary(patents))
    sections = {}
    try:
        sections = parse_sections(gemini_api_call(prompt, config.GEMINI_TEXT_MODEL, temperature=0.3))
    except Exception:
        logger.exception("[REPORT] Gemini failed for the blocking analysis of %r — using the count-based text", drug_name)

    blocking = sum(_g(p, "tag") == "BLOCKING" for p in patents)
    fallback = {
        "overall": f"{len(patents)} patents were analysed: {blocking} blocking and {len(patents) - blocking} non-blocking.",
        "blocking": "See the category breakdown above.",
        "non_blocking": "See the category breakdown above.",
    }
    return {key: sections.get(key) or fallback[key] for key in fallback}


# ─────────────────────────────────────────────
# PDF
# ─────────────────────────────────────────────

def _paragraphs(text: str, style) -> List[Paragraph]:
    """One Paragraph per blank-line-separated paragraph, patent numbers in bold."""
    out = []
    for para in (text or "").split("\n\n"):
        para = " ".join(para.split())
        if para:
            out.append(Paragraph(_PATENT_NUMBER.sub(r"<b>\1</b>", ru.esc(para)), style))
    return out


def build_report(drug_name: str, patents: List[Dict], output_path: str) -> str:
    """Write the PDF to *output_path* and return it."""
    sections = generate_sections(drug_name, patents)
    styles = _build_styles()
    blocking = [p for p in patents if _g(p, "tag") == "BLOCKING"]

    category_counts = defaultdict(lambda: [0, 0])
    for p in patents:
        category_counts[_g(p, "claim_category", "Other")][0 if _g(p, "tag") == "BLOCKING" else 1] += 1
    rows = [[Paragraph("<b>Category</b>", styles["th"]), Paragraph("<b>Blocking</b>", styles["th"]),
             Paragraph("<b>Non-Blocking</b>", styles["th"])]]
    for category, (b, nb) in sorted(category_counts.items()):
        rows.append([Paragraph(str(category), styles["td_left"]),
                     Paragraph(f'<font color="{_RED.hexval()}">{b}</font>' if b else "0", styles["td"]),
                     Paragraph(f'<font color="{_GREEN.hexval()}">{nb}</font>' if nb else "0", styles["td"])])
    table = Table(rows, colWidths=[200, 70, 85])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _DARK_BLUE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [_WHITE, _LIGHT_GREY]),
        ("BOX", (0, 0), (-1, -1), 0.5, _MED_BLUE),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#D0D0D0")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))

    today = datetime.now()
    story = [
        Paragraph(drug_name, styles["title"]),
        Paragraph(config.DIMENSION_BLOCKING_ANALYSIS, styles["subtitle"]),
        Paragraph(f"Analysis Date: {today.strftime('%Y-%m-%d')}&nbsp;&nbsp;|&nbsp;&nbsp;"
                  f"Patents Analysed: {len(patents)}&nbsp;&nbsp;|&nbsp;&nbsp;"
                  f"<font color='{_RED.hexval()}'>Blocking: {len(blocking)}</font>&nbsp;&nbsp;|&nbsp;&nbsp;"
                  f"<font color='{_GREEN.hexval()}'>Non-Blocking: {len(patents) - len(blocking)}</font>", styles["meta"]),
        HRFlowable(width="100%", thickness=1.5, color=_MED_BLUE, spaceAfter=8),
        table,
        Spacer(1, 6),
        Paragraph("1. Overall Analysis", styles["heading"]), *_paragraphs(sections["overall"], styles["body"]),
        Paragraph("2. Blocking Patents", styles["heading"]), *_paragraphs(sections["blocking"], styles["body"]),
        Paragraph("3. Non-Blocking Patents", styles["heading"]), *_paragraphs(sections["non_blocking"], styles["body"]),
        Spacer(1, 10),
        HRFlowable(width="100%", thickness=0.5, color=_GREY, spaceAfter=3),
        Paragraph(f"Report Date: {today.strftime('%d-%b-%Y')}", styles["footer"]),
    ]
    SimpleDocTemplate(
        output_path, pagesize=A4,
        topMargin=16 * mm, bottomMargin=12 * mm, leftMargin=16 * mm, rightMargin=16 * mm,
        title=f"{drug_name} — {config.DIMENSION_BLOCKING_ANALYSIS}", author="IP Pipeline",
    ).build(story)
    return output_path


def report_for_drug(drug_name: str, patents: List[Dict]) -> dict:
    """Build the report in a temporary directory and upload it. Called by every
    primary_market_entry_horizon run. Returns {"drug_name", "gcs_uri", "archive_uri"}."""
    analysed = analysed_patents(patents)
    if not analysed:
        raise ValueError(f"No blocking / non-blocking patents to report for {drug_name!r}")
    return ru.publish_report(drug_name, config.DIMENSION_BLOCKING_ANALYSIS, lambda path: build_report(drug_name, analysed, path))
