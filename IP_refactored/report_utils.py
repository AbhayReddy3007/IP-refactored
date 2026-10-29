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
"""

from numbers import Real
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

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
    return escape(str(text)) if text is not None else ""


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


def data_table(headers: list, rows: list, col_widths: list, st: dict, score_cols: tuple = (), left_cols: tuple = ()) -> Table:
    """A table with a navy header; numeric values in *score_cols* are coloured by their 1-5 band."""
    out = [[Paragraph(f"<b>{h}</b>", st["th"]) for h in headers]]
    for data_row in rows:
        cells = []
        for ci, value in enumerate(data_row):
            text = "N/A" if value is None else str(value)
            style = st["tdl"] if ci in left_cols else st["td"]
            if ci in score_cols and isinstance(value, Real) and not isinstance(value, bool) and value:
                cells.append(Paragraph(f'<font color="{SCORE_COLORS[band(value)]}"><b>{esc(text)}</b></font>', style))
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
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
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
