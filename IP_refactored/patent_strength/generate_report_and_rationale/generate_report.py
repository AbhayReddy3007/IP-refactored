"""
generate_report.py
──────────────────
Patent Strength PDF report, built from the two patent strength tables.

Per drug:
    1. Load the drug's rows from config.BQ_PATENT_STRENGTH_TABLE (one per patent) and
       config.BQ_COUNTRY_SCORE_TABLE (one per jurisdiction, with the stored rationale)
    2. Build the 2-page PDF:
         page 1  scorecard + country-wise scores + rationale
         page 2  every scored patent with its four sub-factor scores
    3. Upload it to gs://{REPORT_GCS_BUCKET}/{REPORT_GCS_PATH}/:
         {drug}/{GCS_IP_SUBFOLDER}/Patent Strength.pdf
         {drug}/{GCS_IP_SUBFOLDER}/archive/Patent Strength+{timestamp}.pdf
       (the file name is config.DIMENSION_PATENT_STRENGTH)

No Gemini call is made here: the text comes from the stored rationale.

Usage:
    python -m IP_refactored.patent_strength.generate_report_and_rationale.generate_report --drug "Semaglutide"
    python -m IP_refactored.patent_strength.generate_report_and_rationale.generate_report --drug "Semaglutide" -o ./reports --no-upload
"""

import argparse
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import HRFlowable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from xml.sax.saxutils import escape

from ... import config, gcp_helpers
from ...drug_list import resolve_drug_name
from ...primary_market_entry_horizon.generate_report_and_rationale.generate_report import (
    _BLUE, _LGREY, _LIGHT_BLUE, _NAVY, _WHITE, _styles, upload_report,
)
from ...utils import safe_name, setup_logging
from ..scoring.constants import ACTIVE_SFS, SF_DISPLAY_NAMES, SF_WEIGHTS

logger = logging.getLogger(__name__)

# Score colours: 1 = very robust (green) ... 5 = weak (red), as in the sub-factor rubrics.
_SCORE_COLORS = {1: "#0D652D", 2: "#1A7A3A", 3: "#E8A317", 4: "#D4570A", 5: "#B3261E"}
_DRUG_MATCH = "REGEXP_REPLACE(LOWER(drug_name), r'\\s+', '') = REGEXP_REPLACE(LOWER(@drug_name), r'\\s+', '')"


# ─────────────────────────────────────────────
# Data
# ─────────────────────────────────────────────

def load_patent_rows(drug_name: str) -> pd.DataFrame:
    return gcp_helpers.run_query(
        f"SELECT * FROM `{config.BQ_PATENT_STRENGTH_TABLE}` WHERE {_DRUG_MATCH} ORDER BY weighted_score, patent_number",
        {"drug_name": drug_name},
    )


def load_country_rows(drug_name: str) -> pd.DataFrame:
    return gcp_helpers.run_query(
        f"SELECT * FROM `{config.BQ_COUNTRY_SCORE_TABLE}` WHERE {_DRUG_MATCH} ORDER BY country_weight DESC, jurisdiction",
        {"drug_name": drug_name},
    )


def _num(value, decimals: int = 2) -> str:
    return "N/A" if value is None or pd.isna(value) else f"{float(value):.{decimals}f}"


def _score_html(value, decimals: int = 2) -> str:
    """A score coloured by its 1-5 band."""
    if value is None or pd.isna(value):
        return "N/A"
    color = _SCORE_COLORS[min(5, max(1, round(float(value))))]
    return f'<font color="{color}"><b>{float(value):.{decimals}f}</b></font>'


# ─────────────────────────────────────────────
# PDF
# ─────────────────────────────────────────────

def _scorecard(final_score, n_patents: int, coverage: float, st: Dict, width: float) -> Table:
    big = ParagraphStyle("big", alignment=TA_CENTER, leading=26)
    rows = [
        [Paragraph("<b>Final Patent Score</b>", st["th"]),
         Paragraph("<b>Patents Scored</b>", st["th"]),
         Paragraph("<b>Country Weight Covered</b>", st["th"])],
        [Paragraph(f'<font size="22">{_score_html(final_score, 2)}</font>', big),
         Paragraph(f'<font size="16"><b>{n_patents}</b></font>', big),
         Paragraph(f'<font size="16"><b>{coverage:.0%}</b></font>', big)],
    ]
    table = Table(rows, colWidths=[width / 3] * 3)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _NAVY),
        ("BACKGROUND", (0, 1), (-1, 1), _LIGHT_BLUE),
        ("BOX", (0, 0), (-1, -1), 0.75, _BLUE),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#C0C0C0")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 1), (-1, 1), 5),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 5),
    ]))
    return table


def _country_table(country: pd.DataFrame, st: Dict, width: float) -> Table:
    th, td, tl = st["th"], st["td"], st["tdl"]
    rows = [[Paragraph(f"<b>{h}</b>", th) for h in
             ("Jurisdiction", "Weight", "Patents", "Avg Weighted Score", "Country Score")]]
    for _, r in country.iterrows():
        rows.append([
            Paragraph(escape(f"{r['jurisdiction']} - {r['country_name']}"), tl),
            Paragraph(_num(r["country_weight"]), td),
            Paragraph(str(int(r["patent_count"])), td),
            Paragraph(_score_html(r["avg_weighted_score"]), td),
            Paragraph(_num(r["country_weighted_score"], 4), td),
        ])
    table = Table(rows, colWidths=[width * w for w in (0.34, 0.12, 0.12, 0.22, 0.20)], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _NAVY),
        ("BOX", (0, 0), (-1, -1), 0.5, _BLUE),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#D0D0D0")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [_WHITE, _LGREY]),
    ]))
    return table


def _patent_table(patents: pd.DataFrame, st: Dict, width: float) -> Table:
    th, td, tl = st["th"], st["td"], st["tdl"]
    headers = ["Patent", "Jur", "Type", *[sf.upper() for sf in ACTIVE_SFS], "Weighted"]
    rows = [[Paragraph(f"<b>{h}</b>", th) for h in headers]]
    for _, r in patents.iterrows():
        rows.append([
            Paragraph(escape(str(r["patent_number"])), tl),
            Paragraph(escape(str(r["jurisdiction"])), td),
            Paragraph(escape(str(r["patent_type"] or "")), td),
            *[Paragraph(_score_html(r[f"{sf}_score"], 0), td) for sf in ACTIVE_SFS],
            Paragraph(_score_html(r["weighted_score"]), td),
        ])
    table = Table(rows, colWidths=[width * w for w in (0.24, 0.07, 0.21, 0.09, 0.09, 0.09, 0.09, 0.12)], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _NAVY),
        ("BOX", (0, 0), (-1, -1), 0.5, _BLUE),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#D0D0D0")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [_WHITE, _LGREY]),
    ]))
    return table


def build_report(drug_name: str, patents: pd.DataFrame, country: pd.DataFrame, output_path: str) -> str:
    """Write the 2-page PDF to *output_path* and return its resolved path."""
    st = _styles()
    title = f"{drug_name} - {config.DIMENSION_PATENT_STRENGTH}"
    doc = SimpleDocTemplate(
        output_path, pagesize=A4,
        topMargin=14 * mm, bottomMargin=10 * mm, leftMargin=17 * mm, rightMargin=17 * mm,
        title=title, author="IP Pipeline",
    )
    width = A4[0] - 34 * mm
    final_score = country["final_patent_score"].iloc[0] if not country.empty else None
    coverage = float(country.loc[country["patent_count"] > 0, "country_weight"].sum()) if not country.empty else 0.0
    rationale = next((str(r).strip() for r in country.get("rationale", []) if isinstance(r, str) and r.strip()), "")

    story: list = [
        Paragraph(escape(title), st["title"]),
        Paragraph(
            f"Analysis Date: {datetime.now().strftime('%d-%b-%Y')}&nbsp;&nbsp;|&nbsp;&nbsp;"
            "Blocking, granted patents scored on four legal-robustness sub-factors "
            "(1 = very robust, 5 = weak)", st["meta"]),
        HRFlowable(width="100%", thickness=1.5, color=_BLUE, spaceAfter=5),
        _scorecard(final_score, len(patents), coverage, st, width),
        Spacer(1, 3 * mm),
        Paragraph("Country-wise Scores", st["h1"]),
        _country_table(country, st, width),
        Spacer(1, 3 * mm),
        Paragraph("Rationale", st["h1"]),
        Paragraph(escape(rationale or "No rationale provided."), st["body"]),
        PageBreak(),
        Paragraph("Patent Scores", st["h1"]),
        HRFlowable(width="100%", thickness=1.5, color=_BLUE, spaceAfter=3),
        _patent_table(patents, st, width),
        Spacer(1, 2 * mm),
        Paragraph(" &nbsp;|&nbsp; ".join(
            f"<b>{sf.upper()}</b> {SF_DISPLAY_NAMES[sf].split('(', 1)[1].rstrip(')')} ({SF_WEIGHTS[sf]:.0%})"
            for sf in ACTIVE_SFS), st["legend"]),
    ]
    doc.build(story)
    return str(Path(output_path).resolve())


# ─────────────────────────────────────────────
# Per-drug run
# ─────────────────────────────────────────────

def generate_report(drug_name: str, output_dir: Path, upload: bool = True) -> Dict:
    """Build (and optionally upload) the report for one drug.
    Returns {"drug_name", "local_path", "gcs_uri", "archive_uri"}."""
    patents, country = load_patent_rows(drug_name), load_country_rows(drug_name)
    if patents.empty or country.empty:
        raise ValueError(f"No patent strength rows for {drug_name!r} — run patent_strength first")

    output_dir.mkdir(parents=True, exist_ok=True)
    local_path = build_report(drug_name, patents, country, str(output_dir / f"{safe_name(drug_name)}_patent_strength_report.pdf"))
    logger.info("[REPORT] %s: saved locally: %s", drug_name, local_path)

    gcs_uri = archive_uri = None
    if upload:
        gcs_uri, archive_uri = upload_report(
            drug_name, Path(local_path).read_bytes(), datetime.now().strftime("%Y%m%d_%H%M%S"),
            report_name=config.DIMENSION_PATENT_STRENGTH,
        )
        logger.info("[REPORT] %s: uploaded: %s | archive: %s", drug_name, gcs_uri, archive_uri)
    return {"drug_name": drug_name, "local_path": local_path, "gcs_uri": gcs_uri, "archive_uri": archive_uri}


def report_for_drug(drug_name: str, upload: bool = True) -> Dict:
    """generate_report() with a temporary local copy (the report lives in GCS).
    Called at the end of every patent_strength run."""
    with tempfile.TemporaryDirectory() as tmp:
        result = generate_report(drug_name, Path(tmp), upload=upload)
    result["local_path"] = None
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the Patent Strength report from the patent strength tables")
    parser.add_argument("--drug", required=True, help="Drug name")
    parser.add_argument("-o", "--output_dir", default="reports", help="Local output directory (default: ./reports)")
    parser.add_argument("--no-upload", action="store_true", help="Build the PDF locally without uploading to GCS")
    args = parser.parse_args()

    setup_logging()
    result = generate_report(resolve_drug_name(args.drug), Path(args.output_dir), upload=not args.no_upload)
    logger.info("[REPORT] %s -> %s%s", result["drug_name"], result["local_path"],
                f" | {result['gcs_uri']} | {result['archive_uri']}" if result["gcs_uri"] else "")


if __name__ == "__main__":
    main()
