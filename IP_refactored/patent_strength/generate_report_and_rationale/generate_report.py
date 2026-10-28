"""
generate_report.py
──────────────────
Patent Strength PDF report, built from the two patent strength tables.

Per drug:
    1. Load the drug's rows from config.BQ_PATENT_STRENGTH_TABLE (one per patent) and
       config.BQ_COUNTRY_SCORE_TABLE (one per jurisdiction, with the stored rationale
       and the drug-level avg_sf_score)
    2. Compute summary statistics and ask Gemini for the narrative sections
       (executive summary, key findings, risk / strength highlights, sub-factor
       analysis, highest-risk patent, geographic risk). The drug-level analysis is the
       stored rationale.
    3. Build a compact PDF (2-3 pages, no page breaks):
         Executive Summary · Portfolio Overview · Key Findings · Patent Score Summary ·
         Country-Weighted Patent Scores · Highest-Risk Patent · Sub-Factor Analysis ·
         Risk & Strength Analysis · Drug-Level Analysis (+ sub-factor / score legend)
    4. Upload it to gs://{REPORT_GCS_BUCKET}/{REPORT_GCS_PATH}/:
         {drug}/{GCS_IP_SUBFOLDER}/Patent Strength.pdf
         {drug}/{GCS_IP_SUBFOLDER}/archive/Patent Strength+{timestamp}.pdf
       (the file name is config.DIMENSION_PATENT_STRENGTH)

Usage:
    python -m IP_refactored.patent_strength.generate_report_and_rationale.generate_report --drug "Semaglutide"
    python -m IP_refactored.patent_strength.generate_report_and_rationale.generate_report --drug "Semaglutide" -o ./reports --no-upload
"""

import argparse
import json
import logging
import re
import tempfile
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from ... import config, gcp_helpers
from ...drug_list import resolve_drug_name
from ...gemini_utils import gemini_api_call
from ...primary_market_entry_horizon.generate_report_and_rationale.generate_report import upload_report
from ...utils import safe_name, setup_logging
from ..scoring.constants import ACTIVE_SFS, SF_WEIGHTS

logger = logging.getLogger(__name__)

# ── Palette / styles ──────────────────────────────────────────────────────────
_NAVY = colors.HexColor("#1F3864")
_BLUE = colors.HexColor("#2F5496")
_LGREY = colors.HexColor("#F2F2F2")
_WHITE = colors.white
_GREY = colors.HexColor("#666666")

_SCORE_CLR = {1: "#008000", 2: "#4CAF50", 3: "#CC9900", 4: "#E65100", 5: "#CC0000"}
SCORE_LABEL = {1: "Very Robust", 2: "Robust", 3: "Moderate", 4: "Vulnerable", 5: "Highly Vulnerable"}

_SF_NAMES = {
    "sf1": ("Novelty & Non-Obviousness", "Closeness of the claimed invention to prior art."),
    "sf2": ("Obvious-to-Combine Risk", "Likelihood of combining known elements with a reasonable expectation of success."),
    "sf3": ("Prosecution History Vulnerability", "Extent of claim narrowing during prosecution."),
    "sf4": ("Secondary Considerations", "Objective evidence supporting non-obviousness."),
}

MAX_PATENT_ROWS = 12  # rows of the Patent Score Summary table, to keep the report to 2-3 pages

_DRUG_MATCH = "REGEXP_REPLACE(LOWER(drug_name), r'\\s+', '') = REGEXP_REPLACE(LOWER(@drug_name), r'\\s+', '')"


def _rl_styles() -> dict:
    b = getSampleStyleSheet()
    return {
        "title":  ParagraphStyle("T",  parent=b["Title"],    fontSize=16, leading=19, textColor=_NAVY, spaceAfter=2),
        "meta":   ParagraphStyle("M",  parent=b["Normal"],   fontSize=8.5, leading=10, textColor=_GREY, spaceAfter=4),
        "h1":     ParagraphStyle("H1", parent=b["Heading2"], fontSize=11.5, leading=14, textColor=_NAVY, spaceBefore=5, spaceAfter=2),
        "h2":     ParagraphStyle("H2", parent=b["Heading3"], fontSize=9.5, leading=12, textColor=_NAVY, spaceBefore=3, spaceAfter=1),
        "h3":     ParagraphStyle("H3", parent=b["Heading4"], fontSize=10, leading=12, textColor=_BLUE, spaceBefore=4, spaceAfter=2),
        "body":   ParagraphStyle("BD", parent=b["Normal"],   fontSize=8.5, leading=11, alignment=TA_JUSTIFY, spaceAfter=3),
        "bullet": ParagraphStyle("BU", parent=b["Normal"],   fontSize=8.5, leading=11, spaceAfter=1, leftIndent=18, bulletIndent=6),
        "legend": ParagraphStyle("LG", parent=b["Normal"],   fontSize=7, leading=9, textColor=_GREY, spaceAfter=1),
        "th":     ParagraphStyle("TH", parent=b["Normal"],   fontSize=8, leading=10, textColor=_WHITE, alignment=TA_CENTER),
        "td":     ParagraphStyle("TD", parent=b["Normal"],   fontSize=8, leading=10, alignment=TA_CENTER),
        "tdl":    ParagraphStyle("TL", parent=b["Normal"],   fontSize=8, leading=10, alignment=TA_LEFT),
        "footer": ParagraphStyle("FT", parent=b["Normal"],   fontSize=7, leading=9, textColor=_GREY, alignment=TA_CENTER),
    }


def _esc(text) -> str:
    return escape(str(text)) if text is not None and not (isinstance(text, float) and pd.isna(text)) else ""


def _num(value, decimals: int = 2) -> str:
    return "N/A" if value is None or pd.isna(value) else f"{float(value):.{decimals}f}"


def _band(value) -> int:
    return min(5, max(1, round(float(value))))


# ─────────────────────────────────────────────
# Data
# ─────────────────────────────────────────────

def load_patent_rows(drug_name: str) -> pd.DataFrame:
    return gcp_helpers.run_query(
        f"SELECT DISTINCT * FROM `{config.BQ_PATENT_STRENGTH_TABLE}` WHERE {_DRUG_MATCH} "
        "ORDER BY weighted_score DESC, patent_number",
        {"drug_name": drug_name},
    )


def load_country_rows(drug_name: str) -> pd.DataFrame:
    return gcp_helpers.run_query(
        f"SELECT DISTINCT * FROM `{config.BQ_COUNTRY_SCORE_TABLE}` WHERE {_DRUG_MATCH} "
        "ORDER BY country_weight DESC, jurisdiction",
        {"drug_name": drug_name},
    )


def _first(country: pd.DataFrame, column: str):
    """A drug-level value (the same on every country row)."""
    if country.empty or column not in country.columns:
        return None
    values = country[column].dropna()
    return values.iloc[0] if len(values) else None


def _patent_summary(row: pd.Series) -> dict:
    return {
        "patent_number": str(row.get("patent_number", "N/A")),
        "patent_type":   str(row.get("patent_type", "") or "N/A"),
        "jurisdiction":  str(row.get("jurisdiction", "N/A")),
        "weighted":      None if pd.isna(row.get("weighted_score")) else float(row["weighted_score"]),
        **{sf: (None if pd.isna(row.get(f"{sf}_score")) else int(row[f"{sf}_score"])) for sf in ACTIVE_SFS},
        **{f"{sf}_reasoning": str(row.get(f"{sf}_reasoning", "") or "") for sf in ACTIVE_SFS},
        **{f"{sf}_findings": [str(row.get(f"{sf}_key_finding_{n}", "") or "") for n in (1, 2, 3)
                              if str(row.get(f"{sf}_key_finding_{n}", "") or "")] for sf in ACTIVE_SFS},
    }


def compute_statistics(patents: pd.DataFrame) -> dict:
    """Summary statistics over the drug's scored patents (1 = very robust ... 5 = highly vulnerable)."""
    stats = {"total_patents": len(patents), "avg_weighted": None, "score_distribution": {},
             "sf_averages": {}, "patents": [], "highest": None, "highest_per_jurisdiction": {}}
    stats["patents"] = [_patent_summary(r) for _, r in patents.iterrows()]
    scored = [p for p in stats["patents"] if p["weighted"] is not None]
    if scored:
        stats["avg_weighted"] = round(sum(p["weighted"] for p in scored) / len(scored), 2)
        for s in range(1, 6):
            stats["score_distribution"][s] = sum(1 for p in scored if _band(p["weighted"]) == s)
        stats["highest"] = max(scored, key=lambda p: p["weighted"])
        for jur in sorted({p["jurisdiction"] for p in scored}):
            stats["highest_per_jurisdiction"][jur] = max(
                (p for p in scored if p["jurisdiction"] == jur), key=lambda p: p["weighted"])
    for sf in ACTIVE_SFS:
        values = [p[sf] for p in stats["patents"] if p[sf] is not None]
        stats["sf_averages"][sf] = round(sum(values) / len(values), 2) if values else None
    return stats


# ─────────────────────────────────────────────
# Narrative (Gemini)
# ─────────────────────────────────────────────

def _extract_json(text: str):
    """Parse a JSON object from a Gemini reply that may carry markdown fences."""
    text = re.sub(r"^```(?:json)?", "", (text or "").strip()).strip()
    text = re.sub(r"```$", "", text).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return None


def _fallback_narrative() -> dict:
    return {
        "executive_summary": "Analysis complete. See the tables below for details.",
        "key_findings": ["See the detailed tables."],
        "risk_highlights": "Refer to the score tables.",
        "strength_highlights": "Refer to the score tables.",
        "sf_analysis": "See the sub-factor scores in the tables.",
        "highest_score_narrative": "See the score tables for details on the highest-scoring patent.",
        "country_score_narrative": "See the country-weighted score table for details.",
    }


def generate_narrative(drug_name: str, stats: dict, country: pd.DataFrame) -> dict:
    """Ask Gemini for the report's narrative sections (a fallback is used if it fails)."""
    patent_rows = [{
        "patent": p["patent_number"], "type": p["patent_type"], "jurisdiction": p["jurisdiction"],
        "sf1": p["sf1"], "sf2": p["sf2"], "sf3": p["sf3"], "sf4": p["sf4"], "weighted": p["weighted"],
        **{f"{sf}_reasoning": p[f"{sf}_reasoning"][:300] for sf in ACTIVE_SFS},
        **{f"{sf}_key_findings": p[f"{sf}_findings"] for sf in ACTIVE_SFS},
    } for p in stats["patents"][:15]]

    highest = stats["highest"]
    highest_text = (
        f"Highest weighted score patent: {highest['patent_number']} (Jurisdiction: {highest['jurisdiction']}, "
        f"Type: {highest['patent_type']}, Score: {highest['weighted']})" if highest else "N/A"
    )
    per_jur = "; ".join(f"{jur}: {p['patent_number']} (Score: {p['weighted']})"
                        for jur, p in stats["highest_per_jurisdiction"].items()) or "N/A"
    country_lines = [
        f"  {r['jurisdiction']} ({r['country_name']}): weight={r['country_weight']}, patents={r['patent_count']}, "
        f"avg_weighted={r['avg_weighted_score']}, country_weighted={r['country_weighted_score']}"
        for _, r in country.iterrows()
    ]

    prompt = f"""You are a senior pharmaceutical patent analyst writing a detailed report on the patent strength of {drug_name}.
Based on the data below, produce a concise analytical report (the whole report must fit on 2-3 pages, so keep every
section short). Be specific: reference patent numbers, jurisdictions and actual scores.

PORTFOLIO STATISTICS:
- Blocking, granted patents scored: {stats['total_patents']}
- Average weighted robustness score: {stats['avg_weighted']} (1 = Very Robust, 5 = Highly Vulnerable)
- Drug-level average sub-factor score (avg_sf_score): {_num(_first(country, 'avg_sf_score'))}
- Final country-weighted patent score: {_num(_first(country, 'final_patent_score'), 4)}
- Score distribution: {json.dumps(stats['score_distribution'])}
- Sub-factor averages: SF1 (Novelty)={stats['sf_averages']['sf1']}, SF2 (Obvious-to-Combine)={stats['sf_averages']['sf2']}, SF3 (Prosecution History)={stats['sf_averages']['sf3']}, SF4 (Secondary Considerations)={stats['sf_averages']['sf4']}
- {highest_text}
- Highest score patent per jurisdiction: {per_jur}

COUNTRY-WEIGHTED SCORES:
{chr(10).join(country_lines) or 'N/A'}

PATENT-LEVEL DATA (JSON):
{json.dumps(patent_rows, indent=1)}

Respond ONLY with a valid JSON object (no markdown fences):
{{
  "executive_summary": "<3-4 sentences giving an overview of the portfolio's legal robustness and overall risk posture>",
  "key_findings": ["<one-sentence finding 1 with patent numbers>", "<finding 2>", "<finding 3>", "<finding 4>"],
  "risk_highlights": "<2-3 sentences on the highest-risk patents and why they are exposed to challenge; reference patent numbers and scores>",
  "strength_highlights": "<2-3 sentences on the most robust patents and what makes them resilient; reference patent numbers and scores>",
  "sf_analysis": "<2-3 sentences on sub-factor trends: which sub-factor is weakest / strongest overall>",
  "highest_score_narrative": "<2-3 sentences on the patent with the highest weighted score: why it scores so high and what it means for challengers or defenders>",
  "country_score_narrative": "<2-3 sentences on the country-weighted scores: which jurisdictions drive the risk and how the country weights affect the overall score>"
}}"""
    try:
        parsed = _extract_json(gemini_api_call(prompt, config.GEMINI_TEXT_MODEL, temperature=0.3))
    except Exception:
        logger.exception("[REPORT] Gemini narrative failed for %r — using the fallback text", drug_name)
        parsed = None
    return {**_fallback_narrative(), **(parsed or {})}


# ─────────────────────────────────────────────
# PDF building blocks
# ─────────────────────────────────────────────

def _info_table(items: list, st: dict, width: float) -> Table:
    """A two-column label / value box."""
    rows = [[Paragraph(f"<b>{_esc(label)}</b>", st["tdl"]), Paragraph(value, st["td"])] for label, value in items]
    table = Table(rows, colWidths=[width * 0.38, width * 0.62])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#E8EDF3")),
        ("BOX", (0, 0), (-1, -1), 0.5, _BLUE),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    return table


def _data_table(headers: list, rows: list, col_widths: list, st: dict, score_cols: tuple = (), left_cols: tuple = ()) -> Table:
    """A table with a navy header; columns in *score_cols* are coloured by their 1-5 band."""
    out = [[Paragraph(f"<b>{h}</b>", st["th"]) for h in headers]]
    for data_row in rows:
        cells = []
        for ci, value in enumerate(data_row):
            text = "N/A" if value is None or (not isinstance(value, str) and pd.isna(value)) else str(value)
            style = st["tdl"] if ci in left_cols else st["td"]
            if ci in score_cols and text != "N/A":
                try:
                    cells.append(Paragraph(f'<font color="{_SCORE_CLR[_band(value)]}"><b>{_esc(text)}</b></font>', style))
                    continue
                except (ValueError, TypeError):
                    pass
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


def _paragraphs(text: str, style) -> list:
    """One Paragraph per non-empty line of *text*."""
    return [Paragraph(_esc(line.strip()), style) for line in (text or "").split("\n") if line.strip()]


def build_report(drug_name: str, patents: pd.DataFrame, country: pd.DataFrame, output_path: str) -> str:
    """Write the PDF to *output_path* and return its resolved path."""
    stats = compute_statistics(patents)
    logger.info("[REPORT] %s: generating the narrative with Gemini ...", drug_name)
    narrative = generate_narrative(drug_name, stats, country)

    st = _rl_styles()
    title = f"{config.DIMENSION_PATENT_STRENGTH} - {drug_name}"
    doc = SimpleDocTemplate(
        output_path, pagesize=A4,
        topMargin=14 * mm, bottomMargin=12 * mm, leftMargin=17 * mm, rightMargin=17 * mm,
        title=title, author="IP Pipeline",
    )
    width = A4[0] - 34 * mm
    final_score = _first(country, "final_patent_score")
    avg_sf_score = _first(country, "avg_sf_score")
    if avg_sf_score is None and not country.empty:  # rows written before avg_sf_score existed
        avg_sf_score = country["avg_weighted_score"].dropna().mean()
    rationale = _first(country, "rationale")
    coverage = float(country.loc[country["patent_count"] > 0, "country_weight"].sum()) if not country.empty else 0.0

    story: list = [
        Paragraph(_esc(title), st["title"]),
        Paragraph(f"Generated {datetime.now().strftime('%B %d, %Y')}&nbsp;&nbsp;•&nbsp;&nbsp;"
                  f"{stats['total_patents']} Blocking Patents&nbsp;&nbsp;•&nbsp;&nbsp;"
                  f"{len(country)} Jurisdiction(s)", st["meta"]),
        HRFlowable(width="100%", thickness=1.5, color=_NAVY, spaceAfter=4),
    ]

    # Executive Summary
    story += [Paragraph("Executive Summary", st["h1"]), *_paragraphs(narrative["executive_summary"], st["body"])]

    # Portfolio Overview
    sf_avgs = " · ".join(f"SF{i} {stats['sf_averages'][sf]}" for i, sf in enumerate(ACTIVE_SFS, 1)
                         if stats["sf_averages"][sf] is not None)
    dist = "; ".join(f"{SCORE_LABEL[s]}: {n}" for s, n in stats["score_distribution"].items() if n)
    overview = [
        ("Patents Analysed", str(stats["total_patents"])),
        ("Final Patent Score (country-weighted)", _num(final_score, 4)),
        ("Drug-level Score (avg_sf_score)", f"{_num(avg_sf_score)} / 5.0"),
        ("Average Weighted Score", f"{_num(stats['avg_weighted'])} / 5.0"),
        ("Country Weight Covered", f"{coverage:.0%}"),
    ]
    if sf_avgs:
        overview.append(("Sub-factor Averages", sf_avgs))
    if dist:
        overview.append(("Score Distribution", dist))
    story += [Paragraph("Portfolio Overview", st["h1"]), _info_table(overview, st, width)]

    # Key Findings
    story.append(Paragraph("Key Findings", st["h1"]))
    story += [Paragraph(f"&bull; {_esc(f)}", st["bullet"]) for f in narrative["key_findings"]]

    # Patent Score Summary (highest weighted score first; capped so the report stays short)
    shown = stats["patents"][:MAX_PATENT_ROWS]
    story.append(Paragraph("Patent Score Summary", st["h1"]))
    summary_rows = [[p["patent_number"], p["jurisdiction"], p["patent_type"], p["sf1"], p["sf2"], p["sf3"], p["sf4"], p["weighted"]]
                    for p in shown]
    story.append(_data_table(["Patent No.", "Jur", "Type", "SF1", "SF2", "SF3", "SF4", "Weighted"], summary_rows,
                             [width * w for w in (0.22, 0.08, 0.22, 0.09, 0.09, 0.09, 0.09, 0.12)], st,
                             score_cols=(3, 4, 5, 6, 7), left_cols=(0,)))
    if len(stats["patents"]) > len(shown):
        story.append(Paragraph(f"Showing the {len(shown)} highest-scoring of {len(stats['patents'])} patents "
                               "(all are in the patent strength table).", st["legend"]))

    # Country-Weighted Scores
    if not country.empty:
        story.append(Paragraph("Country-Weighted Patent Scores", st["h1"]))
        country_rows = [[r["jurisdiction"], r["country_name"], r["country_weight"], r["patent_count"],
                         _num(r["avg_weighted_score"]), _num(r["country_weighted_score"], 4)] for _, r in country.iterrows()]
        story += [_data_table(["Jurisdiction", "Country", "Weight", "# Patents", "Avg Weighted Score", "Country Weighted Score"],
                              country_rows, [width * w for w in (0.12, 0.22, 0.10, 0.10, 0.20, 0.26)], st, score_cols=(4,)),
                  Spacer(1, 3),
                  *_paragraphs(narrative["country_score_narrative"], st["body"])]

    # Highest-risk patent
    highest = stats["highest"]
    if highest:
        story += [Paragraph("Highest-Risk Patent", st["h1"]),
                  Paragraph(f"<b>{_esc(highest['patent_number'])}</b> ({_esc(highest['jurisdiction'])}, {_esc(highest['patent_type'])}) — "
                            f"weighted score {highest['weighted']} / 5.0, {SCORE_LABEL[_band(highest['weighted'])]} "
                            f"(SF1 {highest['sf1']}, SF2 {highest['sf2']}, SF3 {highest['sf3']}, SF4 {highest['sf4']})", st["body"]),
                  *_paragraphs(narrative["highest_score_narrative"], st["body"])]

    # Sub-Factor Analysis
    story += [Paragraph("Sub-Factor Analysis", st["h1"]), *_paragraphs(narrative["sf_analysis"], st["body"])]

    # Risk & Strength
    story += [Paragraph("Risk &amp; Strength Analysis", st["h1"]),
              Paragraph('<font color="#CC0000"><b>Highest Risk Patents</b></font>', st["h2"]),
              *_paragraphs(narrative["risk_highlights"], st["body"]),
              Paragraph('<font color="#008000"><b>Most Robust Patents</b></font>', st["h2"]),
              *_paragraphs(narrative["strength_highlights"], st["body"])]

    # Drug-Level Analysis = the stored rationale
    story += [Paragraph(f"Drug-Level Analysis — {_esc(drug_name)}", st["h1"]),
              *_paragraphs(rationale or "No rationale provided.", st["body"])]

    # Framework + legend, in two lines
    story += [Spacer(1, 4),
              Paragraph("<b>Sub-factors:</b> " + " · ".join(f"SF{i} {_SF_NAMES[sf][0]} ({SF_WEIGHTS[sf]:.0%})"
                                                          for i, sf in enumerate(ACTIVE_SFS, 1)), st["legend"]),
              Paragraph("<b>Score legend:</b> " + " | ".join(f"{k} = {v}" for k, v in SCORE_LABEL.items()), st["legend"]),
              Spacer(1, 4),
              Paragraph("<i>Auto-generated from the Patent Strength scoring output using Gemini.</i>", st["footer"])]

    doc.build(story)
    return str(Path(output_path).resolve())


# ─────────────────────────────────────────────
# Per-drug run
# ─────────────────────────────────────────────

def generate_report(drug_name: str, output_dir: Path, upload: bool = True) -> dict:
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


def report_for_drug(drug_name: str, upload: bool = True) -> dict:
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
