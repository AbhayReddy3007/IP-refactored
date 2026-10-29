"""
generate_dim4_report.py
───────────────────────
Global Launch Sequencing and Geographic Arbitrage PDF report (2-3 pages), built from the run's own
results (nothing is read back from BigQuery):

    Header · score strip (Dimension IV score, jurisdictions, earliest / longest LOE, best opportunity) ·
    Launch Sequence by Jurisdiction · Executive Summary · Launch Sequence · Arbitrage Opportunity ·
    Protection Gaps · Key Risks · Conclusion

Gemini writes the narrative sections from the arbitrage summary (a score-based fallback is used if
it fails). The report is uploaded to gs://{REPORT_GCS_BUCKET}/{REPORT_GCS_PATH}/:
    {drug}/{GCS_IP_SUBFOLDER}/Global Launch Sequencing & Arbitrage.pdf
    {drug}/{GCS_IP_SUBFOLDER}/archive/Global Launch Sequencing & Arbitrage+{timestamp}.pdf
(the file name is config.DIMENSION_GLOBAL_LAUNCH)

    report_for_drug(drug, arbitrage, rationale) -> {"gcs_uri", "archive_uri"}
"""

import logging
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd
from reportlab.platypus import HRFlowable, Paragraph, Spacer

from .... import config, report_utils as ru
from ....gemini_utils import gemini_json_call
from ....primary_market_entry_horizon.generate_report_and_rationale.generate_report import upload_report
from ..generate_rationale import data_block

logger = logging.getLogger(__name__)

NARRATIVE_PROMPT = """You are a senior pharmaceutical patent analyst writing a crisp, easy-to-read report for a business reader on the global launch sequencing and geographic arbitrage of {drug}.

Scoring: a jurisdiction's product LOE (loss of exclusivity) year is the year its last-expiring qualifying patent expires. Jurisdictions are ordered by LOE, earliest first, which is the order in which a product could launch. The arbitrage score is 1-5 (5 = immediate opportunity, 1 = no meaningful gap); the Dimension IV score is the average of the non-US scores (the US is the reference market).

DATA
{data}

Rules:
- Plain language, short sentences. Each text section is at most 3 sentences; each key risk is one sentence.
- Reference specific jurisdictions, years and scores. Use only the data above; do not invent facts.

Respond ONLY with a valid JSON object:
{{
  "executive_summary": "<overall arbitrage opportunity and score, in plain terms>",
  "launch_sequence": "<the order and years in which the drug could launch across jurisdictions>",
  "arbitrage_opportunity": "<which jurisdictions offer the most arbitrage and why>",
  "protection_gaps": "<where missing claim categories (composition of matter, device, formulation) make a launch easier>",
  "key_risks": ["<risk 1>", "<risk 2>", "<risk 3>"],
  "conclusion": "<bottom line for the reader>"
}}"""


def generate_narrative(drug: str, arbitrage: pd.DataFrame, rationale: str) -> dict:
    """The report's narrative sections (a score-based fallback is used if Gemini fails)."""
    fallback = {
        "executive_summary": rationale,
        "launch_sequence": " → ".join(f"{row['Jurisdiction']} ({row['Product LOE (Year)']})" for _, row in arbitrage.iterrows()),
        "arbitrage_opportunity": "",
        "protection_gaps": " ".join(f"{row['Jurisdiction']}: {row['Key Protection Gap']}." for _, row in arbitrage.iterrows()),
        "key_risks": [],
        "conclusion": "",
    }
    prompt = NARRATIVE_PROMPT.format(drug=drug, data=data_block(arbitrage))
    try:
        parsed = gemini_json_call(prompt, config.GEMINI_TEXT_MODEL, temperature=0.2)
    except Exception:
        logger.exception("[REPORT] Gemini narrative failed for %r — using the score-based text", drug)
        parsed = {}
    return {**fallback, **{k: v for k, v in parsed.items() if v}}


def _score_strip(arbitrage: pd.DataFrame, st: dict) -> object:
    """Dimension IV score, jurisdictions, earliest and longest LOE, and the best opportunity."""
    first = arbitrage.iloc[0]
    score = pd.to_numeric(first["Dimension IV Score"], errors="coerce")
    earliest = arbitrage.loc[arbitrage["Product LOE (Year)"].astype(int).idxmin()]
    longest = arbitrage.loc[arbitrage["Product LOE (Year)"].astype(int).idxmax()]
    scores = pd.to_numeric(arbitrage["Arbitrage Score"], errors="coerce")
    best = arbitrage.loc[scores.idxmax(), "Jurisdiction"] if scores.notna().any() else "N/A"
    return ru.stat_strip([
        (f"{score:.1f} / 5" if pd.notna(score) else "N/A", "Dimension IV Score"),
        (str(len(arbitrage)), "Jurisdictions"),
        (f"{earliest['Jurisdiction']} {earliest['Product LOE (Year)']}", "Earliest LOE"),
        (f"{longest['Jurisdiction']} {longest['Product LOE (Year)']}", "Longest LOE"),
        (str(best), "Best Opportunity"),
    ], st)


def build_report(drug_name: str, arbitrage: pd.DataFrame, rationale: str, output_path: str) -> str:
    """Write the PDF to *output_path* and return it."""
    narrative = generate_narrative(drug_name, arbitrage, rationale)
    st = ru.styles()
    first = arbitrage.iloc[0]
    rating = first["Dimension IV Rating"]

    story: list = [
        Paragraph(ru.esc(drug_name), st["title"]),
        Paragraph(f"Global Launch Sequencing &amp; Geographic Arbitrage Analysis&nbsp;&nbsp;|&nbsp;&nbsp;"
                  f"{datetime.now().strftime('%B %d, %Y')}", st["meta"]),
        HRFlowable(width="100%", thickness=1.5, color=ru.NAVY, spaceAfter=5),
        _score_strip(arbitrage, st),
        Paragraph("Launch Sequence by Jurisdiction", st["h1"]),
        ru.data_table(
            ["Order", "Jurisdiction", "Product LOE", "Gap vs US (yrs)", "Gap vs Longest (yrs)", "Score", "Signal"],
            [[row["Launch Order"], row["Jurisdiction"], row["Product LOE (Year)"], row["Gap vs US (Years)"],
              row["Gap vs Longest LOE (Years)"], row["Arbitrage Score"], row["Arbitrage Signal"]]
             for _, row in arbitrage.iterrows()],
            [ru.PAGE_WIDTH * w for w in (0.08, 0.14, 0.13, 0.15, 0.18, 0.09, 0.23)], st, score_cols=(5,), left_cols=(6,)),
    ]
    story += ru.section(f"Executive Summary — {ru.esc(rating)}", narrative["executive_summary"], st)
    story += ru.section("Launch Sequence", narrative["launch_sequence"], st)
    story += ru.section("Arbitrage Opportunity", narrative["arbitrage_opportunity"], st)
    story += ru.section("Protection Gaps", narrative["protection_gaps"], st)

    risks = [r for r in narrative["key_risks"] if r]
    if risks:
        story.append(Paragraph("Key Risks", st["h1"]))
        story += [Paragraph(f"&bull; {ru.esc(r)}", st["bullet"]) for r in risks]

    story += ru.section("Conclusion", narrative["conclusion"], st)
    story += [Spacer(1, 8), Paragraph("Global Launch Sequencing Analysis - Confidential", st["footer"])]

    ru.document(output_path, f"{config.DIMENSION_GLOBAL_LAUNCH} - {drug_name}").build(story)
    return str(Path(output_path).resolve())


def report_for_drug(drug_name: str, arbitrage: pd.DataFrame, rationale: str) -> dict:
    """Build the report in a temporary directory and upload it. Called at the end of every
    global_launch_sequencing_and_geographic_arbitrage run. Returns {"drug_name", "gcs_uri", "archive_uri"}."""
    with tempfile.TemporaryDirectory() as tmp:
        local_path = build_report(drug_name, arbitrage, rationale, str(Path(tmp) / "report.pdf"))
        gcs_uri, archive_uri = upload_report(
            drug_name, Path(local_path).read_bytes(), datetime.now().strftime("%Y%m%d_%H%M%S"),
            report_name=config.DIMENSION_GLOBAL_LAUNCH,
        )
    logger.info("[REPORT] %s: uploaded: %s | archive: %s", drug_name, gcs_uri, archive_uri)
    return {"drug_name": drug_name, "gcs_uri": gcs_uri, "archive_uri": archive_uri}
