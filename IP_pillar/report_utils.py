"""
report_utils.py
───────────────
PDF building blocks shared by the score-style dimension reports (patent thicket and circumvention,
global launch sequencing and geographic arbitrage): palette, paragraph styles, score-coloured data
tables, the headline score strip, text helpers and the A4 document.

    styles()                                   -> dict of ParagraphStyles
    data_table(headers, rows, widths, st, ...) -> Table with a navy header; score columns coloured 1-5
    stat_strip(cells, st, width)               -> Table of big values over small labels
    paragraphs(text, style)                    -> one Paragraph per non-empty line
    section(heading, text, st)                 -> heading + body flowables ([] when there is no text)
    document(output_path, title)               -> SimpleDocTemplate (A4, compact margins)
    upload_report(...) / report_gcs_paths(...) where a report goes in GCS (the same IP folder for every report)
    publish_report(drug, report_name, build)   build a report in a temp directory and upload it
"""

import logging
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Tuple

from numbers import Real
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

from . import config, gcp_helpers
from .utils import safe_name

logger = logging.getLogger(__name__)

NAVY = colors.HexColor("#1F3864")
BLUE = colors.HexColor("#2F5496")
LIGHT_GREY = colors.HexColor("#F2F2F2")
WHITE = colors.white
GREY = colors.HexColor("#666666")

# 1-5 scores where 5 is the favourable end: 1 red ... 5 green.
SCORE_COLORS = {1: "#CC0000", 2: "#E65100", 3: "#CC9900", 4: "#4CAF50", 5: "#008000"}

PAGE_WIDTH = A4[0] - 34 * mm  # usable width between the margins


def styles() -> dict:
    base = getSampleStyleSheet()
    return {
        "title":  ParagraphStyle("T",  parent=base["Title"],    fontSize=18, leading=21, textColor=NAVY, spaceAfter=0, alignment=TA_LEFT),
        "meta":   ParagraphStyle("M",  parent=base["Normal"],   fontSize=9, leading=11, textColor=GREY, spaceAfter=4),
        "h1":     ParagraphStyle("H1", parent=base["Heading2"], fontSize=11.5, leading=14, textColor=NAVY, spaceBefore=7, spaceAfter=2),
        "body":   ParagraphStyle("BD", parent=base["Normal"],   fontSize=9, leading=12, alignment=TA_JUSTIFY, spaceAfter=3),
        "bullet": ParagraphStyle("BU", parent=base["Normal"],   fontSize=9, leading=12, spaceAfter=2, leftIndent=14, bulletIndent=4),
        "th":     ParagraphStyle("TH", parent=base["Normal"],   fontSize=8.5, leading=10, textColor=WHITE, alignment=TA_CENTER),
        "td":     ParagraphStyle("TD", parent=base["Normal"],   fontSize=8.5, leading=10, alignment=TA_CENTER),
        "tdl":    ParagraphStyle("TL", parent=base["Normal"],   fontSize=8.5, leading=10.5, alignment=TA_LEFT),
        "big":    ParagraphStyle("BG", parent=base["Normal"],   fontSize=14, leading=17, textColor=NAVY, alignment=TA_CENTER),
        "small":  ParagraphStyle("SM", parent=base["Normal"],   fontSize=7.5, leading=9, textColor=GREY, alignment=TA_CENTER),
        "footer": ParagraphStyle("FT", parent=base["Normal"],   fontSize=7.5, leading=9, textColor=GREY, alignment=TA_CENTER),
    }


def esc(text) -> str:
    """*text* escaped for a reportlab Paragraph ('' for None)."""
    return "" if text is None or (isinstance(text, float) and text != text) else escape(str(text))


def band(score) -> int:
    """The 1-5 band of a score."""
    return max(1, min(5, round(score)))


def shorten(text: str, limit: int) -> str:
    """*text* cut at the last full sentence within *limit* characters."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return cut[:end + 1] if end > limit // 2 else cut.rstrip() + "…"


def paragraphs(text: str, style) -> list:
    """One Paragraph per non-empty line of *text*."""
    return [Paragraph(esc(line.strip()), style) for line in (text or "").split("\n") if line.strip()]


def section(heading: str, text: str, st: dict) -> list:
    """A heading and its body text; nothing when there is no text."""
    return [Paragraph(heading, st["h1"]), *paragraphs(text, st["body"])] if text else []


def data_table(headers: list, rows: list, col_widths: list, st: dict, score_cols: tuple = (), left_cols: tuple = (),
               score_colors: dict = None, padding: int = 2) -> Table:
    """A table with a navy header; numeric values in *score_cols* are coloured by their 1-5 band
    (*score_colors*, default SCORE_COLORS with 5 the favourable end)."""
    score_colors = score_colors or SCORE_COLORS
    out = [[Paragraph(f"<b>{h}</b>", st["th"]) for h in headers]]
    for data_row in rows:
        cells = []
        for ci, value in enumerate(data_row):
            text = "N/A" if value is None else str(value)
            style = st["tdl"] if ci in left_cols else st["td"]
            if ci in score_cols and isinstance(value, Real) and not isinstance(value, bool) and value:
                cells.append(Paragraph(f'<font color="{score_colors[band(value)]}"><b>{esc(text)}</b></font>', style))
            else:
                cells.append(Paragraph(esc(text), style))
        out.append(cells)
    table = Table(out, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("BOX", (0, 0), (-1, -1), 0.5, BLUE),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, LIGHT_GREY]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), padding),
        ("BOTTOMPADDING", (0, 0), (-1, -1), padding),
    ]))
    return table


def stat_strip(cells: list, st: dict, width: float = PAGE_WIDTH) -> Table:
    """The headline numbers side by side: *cells* is [(value, label), ...]."""
    table = Table([[Paragraph(f"<b>{esc(value)}</b>", st["big"]) for value, _ in cells],
                   [Paragraph(label, st["small"]) for _, label in cells]], colWidths=[width / len(cells)] * len(cells))
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_GREY),
        ("BOX", (0, 0), (-1, -1), 0.5, BLUE),
        ("LINEAFTER", (0, 0), (-2, -1), 0.25, colors.lightgrey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return table


def document(output_path: str, title: str) -> SimpleDocTemplate:
    """The A4 document the reports are built into."""
    return SimpleDocTemplate(
        output_path, pagesize=A4,
        topMargin=14 * mm, bottomMargin=14 * mm, leftMargin=17 * mm, rightMargin=17 * mm,
        title=title, author="IP Pipeline",
    )


# ─────────────────────────────────────────────
# Where a report goes
# ─────────────────────────────────────────────

def report_gcs_paths(drug_name: str, run_ts: str, report_name: Optional[str] = None) -> Tuple[str, str]:
    """(current, archived) object paths in config.REPORT_GCS_BUCKET. The report is
    named after its dimension: *report_name*, default
    config.DIMENSION_PRIMARY_MARKET_ENTRY_HORIZON."""
    ip_folder = f"{config.REPORT_GCS_PATH}/{safe_name(drug_name)}/{config.GCS_IP_SUBFOLDER}"
    name = report_name or config.DIMENSION_PRIMARY_MARKET_ENTRY_HORIZON
    return f"{ip_folder}/{name}.pdf", f"{ip_folder}/archive/{name}+{run_ts}.pdf"


def upload_report(drug_name: str, pdf_bytes: bytes, run_ts: str, report_name: Optional[str] = None) -> Tuple[str, str]:
    """Upload the PDF as the current report and as a timestamped archive copy.
    Returns (current_uri, archive_uri)."""
    current_path, archive_path = report_gcs_paths(drug_name, run_ts, report_name)
    return tuple(
        gcp_helpers.upload_blob(path, pdf_bytes, content_type="application/pdf", bucket=config.REPORT_GCS_BUCKET)
        for path in (current_path, archive_path)
    )


def publish_report(drug_name: str, report_name: str, build: Callable[[str], str]) -> dict:
    """Build a report in a temporary directory and upload it. *build(output_path)* writes the PDF and
    returns its path. Returns {"drug_name", "gcs_uri", "archive_uri"}."""
    with tempfile.TemporaryDirectory() as tmp:
        local_path = build(str(Path(tmp) / "report.pdf"))
        gcs_uri, archive_uri = upload_report(
            drug_name, Path(local_path).read_bytes(), datetime.now().strftime("%Y%m%d_%H%M%S"), report_name=report_name)
    logger.info("[REPORT] %s: uploaded: %s | archive: %s", drug_name, gcs_uri, archive_uri)
    return {"drug_name": drug_name, "gcs_uri": gcs_uri, "archive_uri": archive_uri}
