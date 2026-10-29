"""
generate_report.py
──────────────────
LOE Calculation (Primary Market) PDF report, built from glp_master_loe.

Per drug:
    1. Load the drug's rows from config.BQ_MASTER_LOE_TABLE
    2. Compute controlling patents / Years to Entry / score   (generate_rationale.extract_drug_data)
       and use the table's stored "Rationale" column
    3. Ask Gemini for the two pages of analysis text
    4. Build the 2-page PDF and upload it to gs://{REPORT_GCS_BUCKET}/{REPORT_GCS_PATH}/:
         {drug}/{GCS_IP_SUBFOLDER}/Primary Market Entry Horizon.pdf
         {drug}/{GCS_IP_SUBFOLDER}/archive/Primary Market Entry Horizon+{timestamp}.pdf

Usage:
    python -m IP_refactored.primary_market_entry_horizon.generate_report_and_rationale.generate_report
    python -m IP_refactored.primary_market_entry_horizon.generate_report_and_rationale.generate_report --drug "Semaglutide, Tirzepatide"
    python -m IP_refactored.primary_market_entry_horizon.generate_report_and_rationale.generate_report -o ./reports --no-upload
"""

import argparse
import logging
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional
from xml.sax.saxutils import escape

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from ... import config
from ...report_utils import upload_report
from ...gemini_utils import gemini_api_call
from ...utils import is_blank, normalize_drug_str, safe_name, setup_logging
from ...bq_utils import load_master_table
from .generate_rationale import (
    SCORE_LABELS, cell, clean_year, extract_drug_data, fmt_number, forecasted_label,
    is_forecasted, normalize_forecasted_col, to_number, us_ep_rows,
)

logger = logging.getLogger(__name__)

RATIONALE_COL = "Rationale"

# ─────────────────────────────────────────────
# PDF styles
# ─────────────────────────────────────────────

_NAVY        = colors.HexColor("#1B2A4A")
_BLUE        = colors.HexColor("#2C5F8A")
_LIGHT_BLUE  = colors.HexColor("#E8F0FE")
_LIGHT_GREEN = colors.HexColor("#E6F4EA")
_RED         = colors.HexColor("#B3261E")
_GREY        = colors.HexColor("#5F6368")
_LGREY       = colors.HexColor("#F1F3F4")
_WHITE       = colors.white

_SCORE_COLORS = {5: "#0D652D", 4: "#1A7A3A", 3: "#E8A317", 2: "#D4570A", 1: "#B3261E"}


def _styles() -> Dict[str, ParagraphStyle]:
    b = getSampleStyleSheet()
    return {
        "title":   ParagraphStyle("T",  parent=b["Title"],    fontSize=18, leading=22, textColor=_NAVY, spaceAfter=2),
        "meta":    ParagraphStyle("M",  parent=b["Normal"],   fontSize=8, leading=10, textColor=_GREY, spaceAfter=6),
        "h1":      ParagraphStyle("H1", parent=b["Heading2"], fontSize=11, leading=14, textColor=_NAVY, spaceBefore=4, spaceAfter=2),
        "h2":      ParagraphStyle("H2", parent=b["Heading3"], fontSize=9.5, leading=12, textColor=_BLUE, spaceBefore=3, spaceAfter=1),
        "body":    ParagraphStyle("BD", parent=b["Normal"],   fontSize=8.5, leading=11.5, alignment=TA_JUSTIFY, spaceAfter=2),
        "legend":  ParagraphStyle("LG", parent=b["Normal"],   fontSize=7, leading=9, textColor=_GREY, spaceAfter=1),
        "th":      ParagraphStyle("TH", parent=b["Normal"],   fontSize=7.5, leading=9, textColor=_WHITE, alignment=TA_CENTER),
        "td":      ParagraphStyle("TD", parent=b["Normal"],   fontSize=7.5, leading=9, alignment=TA_CENTER),
        "tdl":     ParagraphStyle("TL", parent=b["Normal"],   fontSize=7.5, leading=9, alignment=TA_LEFT),
        "td_wrap": ParagraphStyle("TW", parent=b["Normal"],   fontSize=7.5, leading=10, alignment=TA_LEFT, wordWrap="LTR"),
    }


# ─────────────────────────────────────────────
# Gemini text
# ─────────────────────────────────────────────

def _build_data_block(d: Dict) -> str:
    bp_lines = []
    for jur, patents in (("US", d["us_blocking_list"]), ("EP", d["ep_blocking_list"])):
        ctrl_pn = d["us_ctrl_pn"] if jur == "US" else d["ep_ctrl_pn"]
        for p in patents:
            pn = cell(p, "Patent Number")
            is_fc = is_forecasted(p)
            pn_display = forecasted_label(pn) if is_fc else pn
            fc_tag = " [FORECASTED]" if is_fc else ""
            ctrl_flag = " [CONTROLLING]" if pn == ctrl_pn else ""
            bp_lines.append(
                f"{pn_display} ({jur}){ctrl_flag}{fc_tag} | {cell(p, 'Step 1 Claim Category')} | "
                f"Filed:{clean_year(cell(p, 'Filing Date'))} | PTE:{cell(p, 'PTE (months)')}m | "
                f"Expiry:{clean_year(cell(p, 'Controlling Patent Expiry Year'))} | "
                f"YTE:{fmt_number(to_number(cell(p, 'Years to Entry')))}"
            )

    non_blocking = {}
    for patents, jur in ((d["us_non_list"], "US"), (d["ep_non_list"], "EP")):
        categories: Dict[str, int] = {}
        for p in patents:
            c = cell(p, "Step 1 Claim Category", "Other")
            categories[c] = categories.get(c, 0) + 1
        non_blocking[jur] = categories

    def ctrl_line(key: str, jur: str, with_pediatric: bool) -> str:
        fc_label = " (FORECASTED)" if d[f"{key}_ctrl_is_forecasted"] else ""
        line = (
            f"{jur} CONTROLLING: {d[f'{key}_ctrl_pn_display']}{fc_label} | Cat:{d[f'{key}_ctrl_cat']} | "
            f"Filed:{d[f'{key}_ctrl_filed']} | PTE:{d[f'{key}_ctrl_pte']}m | "
        )
        if with_pediatric:
            line += f"Ped:{d[f'{key}_ctrl_ped']} | "
        line += f"Expiry:{d[f'{key}_ctrl_expiry']} | ExclYr:{d[f'{key}_excl_yr']}"
        if d[f"{key}_approval"] != "N/A":
            line += f" | Approval:{d[f'{key}_approval']}"
        if d[f"{key}_est_approval"] != "N/A":
            line += f" | EstApproval:{d[f'{key}_est_approval']}"
        if d[f"{key}_phase"] != "N/A":
            line += f" | Phase:{d[f'{key}_phase']}"
        return line + f" | YTE:{d[f'{key}_ctrl_yte']}"

    yr = datetime.now().year
    excl_formula = (
        f"ExclYr(US)=ApprovalYr+{config.EXCLUSIVITY_OFFSET_US}(+0.5 if ped). "
        f"ExclYr(EP)=ApprovalYr+{config.EXCLUSIVITY_OFFSET_EP}. "
        f"If approval/estimated approval is unavailable: "
        f"Phase 3 -> EstApproval = {yr}+3 = {yr + 3}; Phase 2 -> EstApproval = {yr}+5 = {yr + 5}."
    )
    rationale_block = f"RATIONALE FROM INPUT DATA:\n{d['rationale']}\n\n" if d["rationale"] != "N/A" else ""
    bp_text = "\n".join(bp_lines) if bp_lines else "None"

    return (
        f"DRUG: {d['drug_name']}\n"
        f"SCORES: LOE Score={d['loe_score']}, Avg YTE(US&EP)={d['avg_yte_us_ep']}, "
        f"US YTE={d['us_yte']}, EP YTE={d['ep_yte']}\n\n"
        f"{ctrl_line('us', 'US', True)}\n"
        f"{ctrl_line('ep', 'EP', False)}\n\n"
        f"ALL BLOCKING PATENTS:\n{bp_text}\n\n"
        f"PORTFOLIO: US {d['total_us']} ({d['n_us_existing']} existing + {d['n_us_forecasted']} forecasted, "
        f"{d['n_us_blocking']}B/{d['n_us_non']}NB) | EP {d['total_ep']} ({d['n_ep_existing']} existing + "
        f"{d['n_ep_forecasted']} forecasted, {d['n_ep_blocking']}B/{d['n_ep_non']}NB)\n"
        f"NON-BLOCKING: US {non_blocking['US']}, EP {non_blocking['EP']}\n\n"
        f"{rationale_block}"
        f"FORECASTED PATENTS NOTE: Patents marked [FORECASTED] are not yet filed. "
        f"They are identified by names like 'Forecasted patent for Device in US'. "
        f"Never use encoded strings like 'US+Forecasted+Device' in the report text.\n\n"
        f"FORMULAS: CtrlPatent=latest-filed BLOCKING patent per jurisdiction. "
        f"Expiry=FilingYr+20+(PTE/12). {excl_formula} "
        f"YTE=max(Expiry,ExclYr)-{yr}. "
        f"AvgYTE=mean(US_YTE,EP_YTE). LOE Score: <={config.SCORE_THRESHOLD_5}->5, "
        f"<={config.SCORE_THRESHOLD_4}->4, <={config.SCORE_THRESHOLD_3}->3, "
        f"<={config.SCORE_THRESHOLD_2}->2, otherwise 1."
    )


def _gemini_call(prompt: str) -> str:
    """One Gemini call (gemini_utils retries with back-off)."""
    try:
        return gemini_api_call(
            prompt, config.GEMINI_TEXT_MODEL, temperature=0.2, max_output_tokens=8192,
        ).strip()
    except Exception as e:
        logger.error("[REPORT] Gemini generation failed: %s", e)
        return f"[Generation failed: {e}]"


def generate_page1_text(d: Dict) -> str:
    yr = datetime.now().year
    rationale_instruction = ""
    if d["rationale"] != "N/A":
        rationale_instruction = (
            "\n- The RATIONALE FROM INPUT DATA block contains analyst notes from the "
            "source table. Incorporate key points from this rationale into the "
            "AVERAGE YEARS TO ENTRY AND LOE SCORE section where relevant, but keep "
            "the section within the 60-80 word limit."
        )

    prompt = f"""You are a senior pharmaceutical patent analyst writing a concise LOE (Primary Market) Scoring report for {d['drug_name']}.

Write clear, analytical paragraphs that fit within one PDF page. Be concise and precise. Write in plain text only. No markdown, no bullets, no asterisks. Use ALL CAPS for section headers only. Each section must be 60-80 words.

STRICT RULES:
- Forecasted patents have names like "Forecasted patent for Device in US" or "Forecasted patent for Compound in EP". Always use this human-readable format. NEVER write encoded strings like "US+Forecasted+Device".
- If a controlling patent is forecasted, clearly state it is a forecasted patent (not yet filed, predicted to be filed in that year).
- Refer to the scoring framework as "LOE (Primary Market) Scoring".
- Show actual arithmetic with real numbers in EVERY section (e.g., "2029 + 5 = 2034").
- Use clean year numbers (2026 not 2026.0).
- Every sentence must reference a specific number, date, or patent identifier.
- Do not use "N/Am" — if a value is unavailable, state it briefly.{rationale_instruction}

DATA:
{_build_data_block(d)}

Write exactly these four sections. Each must be a single focused paragraph of 60-80 words.

REGULATORY EXCLUSIVITY
State the approval or estimated approval year for each jurisdiction ONLY if available in the data. For US: add {config.EXCLUSIVITY_OFFSET_US} years (plus 0.5 if pediatric exclusivity applies) and show the arithmetic. For EP: add {config.EXCLUSIVITY_OFFSET_EP} years and show the arithmetic. State the resulting exclusivity year for each.
If the approval date or estimated approval year is unavailable for a jurisdiction, do NOT mention approval date at all for that jurisdiction. Instead, if an estimated approval year was derived from the Phase (Phase 3 = current year + 3, Phase 2 = current year + 5), use that and show the arithmetic. If neither approval nor phase-based estimate is available, simply omit the exclusivity calculation for that jurisdiction.

CONTROLLING PATENT TERM
Identify the controlling patent for each jurisdiction (latest filing date among blocking patents). State the patent identifier, filing date, any PTE applied, and show: Filing Year + 20 + PTE/12 = Expiry Year.

YEARS TO ENTRY
For each jurisdiction, identify whether the expiry year or exclusivity year is the binding constraint and show: max(Expiry, Exclusivity) - {yr} = Years to Entry.

AVERAGE YEARS TO ENTRY AND LOE SCORE
Present: ({d['us_yte']} + {d['ep_yte']}) / 2 = {d['avg_yte_us_ep']} years. State the LOE (Primary Market) Score of {d['loe_score']} out of 5, name the score band, and briefly note the commercial implication. If rationale data is present, incorporate one key insight from it.

Start directly with REGULATORY EXCLUSIVITY. No introduction or conclusion outside these four sections."""
    return _gemini_call(prompt)


def generate_page2_text(d: Dict) -> str:
    prompt = f"""You are a senior pharmaceutical patent analyst writing the Blocking Patent Analysis section of a concise LOE report for {d['drug_name']}.

Write clear, analytical paragraphs that fit within one PDF page alongside a patent table. Be concise and precise. Write in plain text only. No markdown, no bullets, no asterisks. Use ALL CAPS for section headers only. Each section must be 60-80 words.

STRICT RULES:
- Forecasted patents have names like "Forecasted patent for Device in US" or "Forecasted patent for Compound in EP". Always use this human-readable format. NEVER write encoded strings like "US+Forecasted+Device".
- Distinguish clearly between existing and forecasted patents.
- Reference specific patent identifiers in every paragraph.
- Use clean year numbers (2026 not 2026.0).
- If PTE is not applicable or not granted, state that briefly rather than writing "N/Am".

DATA:
{_build_data_block(d)}

Write exactly these four sections, each a single focused paragraph of 60-80 words.

BLOCKING PATENT LANDSCAPE
State the total blocking patents per jurisdiction, breaking down existing vs forecasted. Summarise the claim categories covered and the overall portfolio strategy.

CONTROLLING PATENT SELECTION
For each jurisdiction, name the controlling patent (or its human-readable forecasted label), state why it was selected (latest filing date), and compare briefly to the next most recent blocking patent.

KEY BLOCKING PATENTS
Analyse the most significant blocking patents beyond the controlling patent. Name each by identifier, claim category, filing date, and expiry.

NON-BLOCKING PATENTS
Summarise the non-blocking portfolio: count per jurisdiction, claim categories, and why they are classified as non-blocking.

Start directly with BLOCKING PATENT LANDSCAPE. No introduction or conclusion outside these four sections."""
    return _gemini_call(prompt)


# ─────────────────────────────────────────────
# PDF
# ─────────────────────────────────────────────

def _trim_to_height(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_period = max(truncated.rfind(". "), truncated.rfind(".\n"))
    return truncated[:last_period + 1] if last_period > 0 else truncated


def _render_ai(text: str, story: list, s_h2: ParagraphStyle, s_body: ParagraphStyle) -> None:
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        is_header = (re.match(r"^[0-9]+\.\s+[A-Z\s&]+$", line)
                     or (line.isupper() and 4 < len(line) < 80 and not line.startswith("[")))
        if is_header:
            story.append(Spacer(1, 0.5 * mm))
            story.append(Paragraph(escape(line), s_h2))
        else:
            story.append(Paragraph(escape(line), s_body))


def _scorecard(d: Dict, st: Dict, width: float) -> Table:
    si = to_number(d["loe_score"])
    color = _SCORE_COLORS.get(int(si), "#5F6368") if si else "#5F6368"
    label = SCORE_LABELS.get(int(si), "N/A") if si else "N/A"
    shown = str(int(si)) if si else "N/A"

    rows = [
        [Paragraph("<b>LOE Score</b>", st["th"]),
         Paragraph("<b>Avg Years (US &amp; EP)</b>", st["th"]),
         Paragraph("<b>US Years to Entry</b>", st["th"]),
         Paragraph("<b>EP Years to Entry</b>", st["th"])],
        [Paragraph(f'<font color="{color}" size="22"><b>{shown}</b></font>'
                   f'<font size="9" color="#5F6368"> / 5</font>',
                   ParagraphStyle("sc", alignment=TA_CENTER, leading=26)),
         Paragraph(f'<font size="14"><b>{d["avg_yte_us_ep"]}</b></font>',
                   ParagraphStyle("av", alignment=TA_CENTER, leading=26)),
         Paragraph(f'<font size="12"><b>{d["us_yte"]}</b></font>',
                   ParagraphStyle("uv", alignment=TA_CENTER, leading=26)),
         Paragraph(f'<font size="12"><b>{d["ep_yte"]}</b></font>',
                   ParagraphStyle("ev", alignment=TA_CENTER, leading=26))],
        [Paragraph(f'<font color="{color}"><b>{label}</b></font>',
                   ParagraphStyle("sl", alignment=TA_CENTER, fontSize=8, leading=10)),
         Paragraph("", st["td"]), Paragraph("", st["td"]), Paragraph("", st["td"])],
        [Paragraph("<b>Rationale</b>", st["th"]),
         Paragraph(escape(d["rationale"] if d["rationale"] != "N/A" else "No rationale provided."), st["td_wrap"]),
         Paragraph("", st["td"]), Paragraph("", st["td"])],
    ]
    table = Table(rows, colWidths=[width * 0.25] * 4)
    table.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), _NAVY),
        ("BACKGROUND",    (0, 1), (-1, 1), _LIGHT_BLUE),
        ("BACKGROUND",    (0, 2), (-1, 2), _LGREY),
        ("BOX",           (0, 0), (-1, -1), 0.75, _BLUE),
        ("INNERGRID",     (0, 0), (-1, -1), 0.4, colors.HexColor("#C0C0C0")),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 1), (-1, 1), 5),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 5),
        ("BACKGROUND",    (0, 3), (0, 3), _NAVY),
        ("BACKGROUND",    (1, 3), (-1, 3), _LGREY),
        ("SPAN",          (1, 3), (-1, 3)),
        ("TOPPADDING",    (0, 3), (-1, 3), 4),
        ("BOTTOMPADDING", (0, 3), (-1, 3), 4),
        ("LEFTPADDING",   (1, 3), (-1, 3), 4),
        ("RIGHTPADDING",  (1, 3), (-1, 3), 4),
        ("VALIGN",        (0, 3), (-1, 3), "TOP"),
    ]))
    return table


def _blocking_table(d: Dict, st: Dict, width: float) -> Table:
    th, td, tl = st["th"], st["td"], st["tdl"]
    all_blocking = d["us_blocking_list"] + d["ep_blocking_list"]
    rows = [[Paragraph("<b>Patent</b>", th), Paragraph("<b>Jur</b>", th),
             Paragraph("<b>Category</b>", th), Paragraph("<b>Filed</b>", th),
             Paragraph("<b>Expiry</b>", th), Paragraph("<b>Controlling</b>", th)]]
    styles = [
        ("BACKGROUND", (0, 0), (-1, 0), _NAVY),
        ("BOX",        (0, 0), (-1, -1), 0.5, _BLUE),
        ("INNERGRID",  (0, 0), (-1, -1), 0.3, colors.HexColor("#D0D0D0")),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [_WHITE, _LGREY]),
    ]
    for i, p in enumerate(all_blocking, 1):
        pn, jur = cell(p, "Patent Number"), cell(p, "Jurisdiction")
        is_ctrl = ((pn == d["us_ctrl_pn"] and jur.upper() == "US") or
                   (pn == d["ep_ctrl_pn"] and jur.upper() == "EP"))
        is_fc = is_forecasted(p)
        display_pn = escape(forecasted_label(pn) if is_fc else pn)
        rows.append([
            Paragraph(f"<b>{display_pn}</b>" if is_ctrl else display_pn, tl),
            Paragraph(escape(jur), td),
            Paragraph(escape(cell(p, "Step 1 Claim Category")), td),
            Paragraph(clean_year(cell(p, "Filing Date")), td),
            Paragraph(clean_year(cell(p, "Controlling Patent Expiry Year")), td),
            Paragraph(f'<font color="{_RED.hexval()}"><b>YES</b></font>' if is_ctrl else chr(8212), td),
        ])
        if is_fc:
            styles.append(("BACKGROUND", (0, i), (-1, i), _LIGHT_GREEN))
        elif is_ctrl:
            styles.append(("BACKGROUND", (0, i), (-1, i), _LIGHT_BLUE))
    table = Table(rows, colWidths=[width * w for w in (0.27, 0.07, 0.20, 0.14, 0.14, 0.18)], repeatRows=1)
    table.setStyle(TableStyle(styles))
    return table


def build_report(d: Dict, page1_text: str, page2_text: str, output_path: str) -> str:
    """Write the 2-page PDF to *output_path* and return its resolved path."""
    page1_text = _trim_to_height(page1_text, 2800)
    page2_text = _trim_to_height(page2_text, 2500)

    st = _styles()
    doc = SimpleDocTemplate(
        output_path, pagesize=A4,
        topMargin=14 * mm, bottomMargin=10 * mm, leftMargin=17 * mm, rightMargin=17 * mm,
        title=f"{d['drug_name'].title()} - LOE Calculation (Primary Market) Report",
        author="IP Pipeline",
    )
    width = A4[0] - 34 * mm
    story: list = []

    # Page 1 — scorecard + scoring explanation
    story.append(Paragraph(escape(f"{d['drug_name'].title()} - LOE Calculation (Primary Market)"), st["title"]))
    story.append(Paragraph(
        f"Analysis Date: {datetime.now().strftime('%d-%b-%Y')}&nbsp;&nbsp;|&nbsp;&nbsp;"
        f"US Patents: {d['total_us']} ({d['n_us_existing']} existing + {d['n_us_forecasted']} forecasted, "
        f"{d['n_us_blocking']} blocking)&nbsp;&nbsp;|&nbsp;&nbsp;"
        f"EP Patents: {d['total_ep']} ({d['n_ep_existing']} existing + {d['n_ep_forecasted']} forecasted, "
        f"{d['n_ep_blocking']} blocking)", st["meta"],
    ))
    story.append(HRFlowable(width="100%", thickness=1.5, color=_BLUE, spaceAfter=5))
    story.append(_scorecard(d, st, width))
    story.append(Spacer(1, 3 * mm))
    _render_ai(page1_text, story, st["h2"], st["body"])
    story.append(PageBreak())

    # Page 2 — blocking patents
    story.append(Paragraph("Blocking Patent Analysis", st["h1"]))
    story.append(HRFlowable(width="100%", thickness=1.5, color=_BLUE, spaceAfter=3))
    if d["us_blocking_list"] or d["ep_blocking_list"]:
        story.append(_blocking_table(d, st, width))
        story.append(Spacer(1, 1 * mm))
        story.append(Paragraph(
            '<font color="#1A7A3A">&#9632;</font> Green = Forecasted patents (predicted, not yet filed)',
            st["legend"],
        ))
        story.append(Spacer(1, 2 * mm))
    _render_ai(page2_text, story, st["h2"], st["body"])

    doc.build(story)
    return str(Path(output_path).resolve())


# ─────────────────────────────────────────────
# glp_master_loe -> DataFrame (loaded by bq_utils.load_master_table)
# ─────────────────────────────────────────────

def _stored_rationale(drug_df: pd.DataFrame) -> Optional[str]:
    """The Rationale glp_master_loe holds for this drug (same on every row)."""
    if RATIONALE_COL not in drug_df.columns:
        return None
    for value in drug_df[RATIONALE_COL]:
        if not is_blank(value):
            return str(value).strip()
    return None


# ─────────────────────────────────────────────
# Per-drug run
# ─────────────────────────────────────────────

def generate_report(drug_name: str, drug_df: pd.DataFrame, output_dir: Path, upload: bool = True) -> Dict:
    """Build (and optionally upload) the report for one drug's glp_master_loe rows.
    Returns {"drug_name", "local_path", "gcs_uri", "archive_uri"}."""
    rows = us_ep_rows(normalize_forecasted_col(drug_df.copy()))
    if rows.empty:
        raise ValueError(f"No US/EP rows for {drug_name!r}")

    logger.info("[REPORT] %s: extracting data...", drug_name)
    d = extract_drug_data(rows)
    stored = _stored_rationale(rows)
    if stored:
        d["rationale"] = stored
    logger.info("[REPORT] %s: US YTE %s | EP YTE %s | avg %s | LOE score %s",
                drug_name, d["us_yte"], d["ep_yte"], d["avg_yte_us_ep"], d["loe_score"])

    logger.info("[REPORT] %s: generating page 1 (scoring explanation)...", drug_name)
    page1 = generate_page1_text(d)
    logger.info("[REPORT] %s: generating page 2 (blocking analysis)...", drug_name)
    page2 = generate_page2_text(d)

    output_dir.mkdir(parents=True, exist_ok=True)
    local_path = build_report(d, page1, page2, str(output_dir / f"{safe_name(drug_name)}_loe_report.pdf"))
    logger.info("[REPORT] %s: saved locally: %s", drug_name, local_path)

    gcs_uri = archive_uri = None
    if upload:
        gcs_uri, archive_uri = upload_report(
            drug_name, Path(local_path).read_bytes(), datetime.now().strftime("%Y%m%d_%H%M%S"),
        )
        logger.info("[REPORT] %s: uploaded: %s | archive: %s", drug_name, gcs_uri, archive_uri)
    return {"drug_name": drug_name, "local_path": local_path, "gcs_uri": gcs_uri, "archive_uri": archive_uri}


def report_for_drug(drug_name: str, upload: bool = True) -> Dict:
    """Load the drug's rows from glp_master_loe and build + upload its report.
    Called at the end of every primary_market_entry_horizon run; the local PDF is a
    temporary copy (the report lives in GCS). Returns generate_report()'s dict."""
    df = load_master_table([drug_name])
    if df.empty:
        raise ValueError(f"No rows for {drug_name!r} in {config.BQ_MASTER_LOE_TABLE}")
    with tempfile.TemporaryDirectory() as tmp:
        result = generate_report(drug_name, df, Path(tmp), upload=upload)
    result["local_path"] = None
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate LOE Calculation (Primary Market) reports from glp_master_loe")
    parser.add_argument("--drug", default=None,
                        help="Drug name, or comma-separated names (default: DRUG_NAME if set, else every drug in the table)")
    parser.add_argument("-o", "--output_dir", default="reports", help="Local output directory (default: ./reports)")
    parser.add_argument("--no-upload", action="store_true", help="Build the PDFs locally without uploading to GCS")
    args = parser.parse_args()

    setup_logging()
    requested = [n.strip() for n in (args.drug or config.DRUG_NAME or "").split(",") if n.strip()]
    df = load_master_table(requested or None)
    if "Drug Name" not in df.columns or df.empty:
        raise SystemExit(f"No rows found in {config.BQ_MASTER_LOE_TABLE}" + (f" for {requested}" if requested else ""))

    # One report per drug, whatever case / spacing its rows were written with.
    df["_drug_key"] = df["Drug Name"].map(normalize_drug_str)
    results, failed = [], []
    drugs = [k for k in df["_drug_key"].unique() if k]
    for i, name in enumerate(drugs, 1):
        logger.info("[REPORT] [%d/%d] %s", i, len(drugs), name)
        try:
            results.append(generate_report(name, df[df["_drug_key"] == name], Path(args.output_dir), upload=not args.no_upload))
        except Exception:
            logger.exception("[REPORT] %s failed", name)
            failed.append(name)

    logger.info("[REPORT] Generated %d report(s)%s", len(results), f" | failed: {failed}" if failed else "")
    for r in results:
        logger.info("[REPORT]   %s -> %s%s", r["drug_name"], r["local_path"], f" | {r['gcs_uri']} | {r['archive_uri']}" if r["gcs_uri"] else "")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
