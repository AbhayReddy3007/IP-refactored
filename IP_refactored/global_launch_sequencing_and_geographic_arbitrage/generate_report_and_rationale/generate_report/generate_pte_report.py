"""
generate_pte_report.py
──────────────────────
Regulatory Exclusivity and PTE Analysis PDF report (2 pages), built from the run's own results
(the shortlisted patents and the arbitrage summary; nothing is read back from BigQuery):

    Header · score strip (jurisdictions, PTE granted, average expiry gap, Dimension IV rating) ·
    1. Strategic Overview (Gemini) · 2. Patent Term Extension by jurisdiction and the exclusivity
    rules in scope · 3. Geographic Arbitrage and the Dimension IV rating

Gemini writes the strategic overview from the PTE and arbitrage figures (a figures-only fallback is
used if it fails). The report is uploaded next to the other IP reports, to
gs://{REPORT_GCS_BUCKET}/{REPORT_GCS_PATH}/:
    {drug}/{GCS_IP_SUBFOLDER}/PTE Analysis.pdf
    {drug}/{GCS_IP_SUBFOLDER}/archive/PTE Analysis+{timestamp}.pdf
(the file name is config.DIMENSION_PTE_ANALYSIS)

    report_for_drug(drug, shortlisted, arbitrage) -> {"gcs_uri", "archive_uri"}
"""

import logging
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd
from reportlab.platypus import HRFlowable, Paragraph, Spacer

from .... import config, report_utils as ru
from ....gemini_utils import gemini_api_call
from ....primary_market_entry_horizon.generate_report_and_rationale.generate_report import upload_report
from ...constants import EXCLUSIVITY_RULES

logger = logging.getLogger(__name__)

NARRATIVE_PROMPT = """You are a pharmaceutical patent and regulatory exclusivity strategist.
Below is a summary of the multi-jurisdictional patent analysis for the drug: {drug}.
Write a concise, insight-driven narrative (strictly 180-220 words) covering:
1. The overall patent expiry landscape and exclusivity risk.
2. PTE status and its impact on effective protection.
3. Geographic arbitrage opportunities and the weakest jurisdictions.
4. Key strategic recommendations (2-3 bullet points).

Data summary:
- Jurisdictions: {jurisdictions}
- PTE granted: {pte_granted} of {patents} shortlisted patent(s)
- Expiry gap (years) — average: {avg_gap}, min: {min_gap}, max: {max_gap}
- Dimension IV rating: {rating}

Regulatory exclusivity rules in scope:
{rules}

Rules for your response:
- Plain prose; no markdown headers. Use no bullet symbols except in the recommendations section.
- Recommendations section: start with "Recommendations:" then use "•" bullets.
- Do NOT include any JSON, code, or special formatting.
- Exactly 180-220 words. Use only the data above; do not invent facts."""


def _status(value) -> str:
    """The PTE status as shown: 'Overridden (...)' and 'Not applicable' are both shown as NA."""
    text = str(value)
    return "NA" if text.startswith("Overridden") or text == "Not applicable" else text


def _year(value) -> str:
    parsed = pd.to_datetime(value, errors="coerce")
    return str(parsed.year) if pd.notna(parsed) else "N/A"


def _summary(shortlisted: pd.DataFrame, arbitrage: pd.DataFrame) -> dict:
    """The figures the strip and the narrative are built from."""
    gaps = pd.to_numeric(shortlisted["Expiry Gap (Years)"], errors="coerce").dropna()
    return {
        "jurisdictions": sorted(shortlisted["Jurisdiction"].dropna().unique()),
        "patents": len(shortlisted),
        "pte_granted": int((shortlisted["PTE Status"] == "Granted").sum()),
        "avg_gap": f"{gaps.mean():.1f}" if not gaps.empty else "N/A",
        "min_gap": f"{gaps.min():g}" if not gaps.empty else "N/A",
        "max_gap": f"{gaps.max():g}" if not gaps.empty else "N/A",
        "rating": arbitrage["Dimension IV Rating"].iloc[0] if not arbitrage.empty else "N/A",
    }


def generate_narrative(drug: str, summary: dict) -> str:
    """The strategic overview (a figures-only fallback is used if Gemini fails)."""
    rules = "\n".join(f"  {k}: {v}" for k, v in EXCLUSIVITY_RULES.items() if k in summary["jurisdictions"])
    prompt = NARRATIVE_PROMPT.format(
        drug=drug, rules=rules or "  (none of the jurisdictions has a rule on file)",
        **{**summary, "jurisdictions": ", ".join(summary["jurisdictions"])})
    try:
        text = gemini_api_call(prompt, config.GEMINI_TEXT_MODEL, temperature=0.2).strip()
        if text:
            return text
    except Exception:
        logger.exception("[REPORT] Gemini narrative failed for %r — using the figures-only text", drug)
    return (f"{drug}: {summary['patents']} shortlisted patent(s) across {', '.join(summary['jurisdictions'])}; "
            f"PTE granted for {summary['pte_granted']}; average expiry gap {summary['avg_gap']} years; "
            f"Dimension IV rating: {summary['rating']}.")


def build_report(drug_name: str, shortlisted: pd.DataFrame, arbitrage: pd.DataFrame, output_path: str) -> str:
    """Write the PDF to *output_path* and return it."""
    summary = _summary(shortlisted, arbitrage)
    st = ru.styles()

    story: list = [
        Paragraph(f"{ru.esc(drug_name)} — Regulatory Exclusivity &amp; PTE Analysis", st["title"]),
        Paragraph(f"Generated {datetime.now().strftime('%B %d, %Y')}", st["meta"]),
        HRFlowable(width="100%", thickness=1.5, color=ru.NAVY, spaceAfter=5),
        ru.stat_strip([
            (str(len(summary["jurisdictions"])), "Jurisdictions"),
            (str(summary["pte_granted"]), "PTE Granted"),
            (f"{summary['avg_gap']} yrs" if summary["avg_gap"] != "N/A" else "N/A", "Avg Expiry Gap"),
            (summary["rating"].split(" Geographic")[0], "Dimension IV"),
        ], st),
    ]
    story += ru.section("1. Strategic Overview", generate_narrative(drug_name, summary), st)

    # 2. PTE by jurisdiction
    story.append(Paragraph("2. Patent Term Extension (PTE) by Jurisdiction", st["h1"]))
    story.append(ru.data_table(
        ["Jurisdiction", "Patent", "PTE Status", "PTE (months)", "Adjusted Expiry"],
        [[row["Jurisdiction"], row["Patent Number"], _status(row["PTE Status"]), row["PTE Months (Granted)"],
          _year(row["Adjusted Expiry (with PTE)"])] for _, row in shortlisted.iterrows()],
        [ru.PAGE_WIDTH * w for w in (0.15, 0.25, 0.30, 0.13, 0.17)], st, left_cols=(2,)))
    rules = [(k, v) for k, v in EXCLUSIVITY_RULES.items() if k in summary["jurisdictions"]]
    if rules:
        story += [Spacer(1, 4), Paragraph("<b>Exclusivity rules in scope</b>", st["body"])]
        story += [Paragraph(f"&bull; <b>{ru.esc(k)}</b>: {ru.esc(v)}", st["bullet"]) for k, v in rules]

    # 3. Geographic arbitrage
    if not arbitrage.empty:
        story.append(Paragraph(f"3. Geographic Arbitrage — {ru.esc(summary['rating'])}", st["h1"]))
        story.append(ru.data_table(
            ["Jurisdiction", "Product LOE", "Score", "Signal"],
            [[row["Jurisdiction"], row["Product LOE (Year)"], row["Arbitrage Score"], row["Arbitrage Signal"]]
             for _, row in arbitrage.iterrows()],
            [ru.PAGE_WIDTH * w for w in (0.20, 0.20, 0.12, 0.48)], st, score_cols=(2,), left_cols=(3,)))
        story.append(Paragraph("Score: 5 = immediate opportunity · 4 = strong · 3 = meaningful · 2 = limited · "
                               "1 = none · N/A = reference market (US)", st["small"]))

    story += [Spacer(1, 6), Paragraph(
        "Auto-generated. Exclusivity periods and PTE grants come from AI inference and should be verified against the "
        "official registers (USPTO, CNIPA, IP Australia, Rospatent, CDSCO, ANVISA). Not legal advice.", st["footer"])]

    ru.document(output_path, f"{config.DIMENSION_PTE_ANALYSIS} - {drug_name}").build(story)
    return str(Path(output_path).resolve())


def report_for_drug(drug_name: str, shortlisted: pd.DataFrame, arbitrage: pd.DataFrame) -> dict:
    """Build the report in a temporary directory and upload it. Called at the end of every
    global_launch_sequencing_and_geographic_arbitrage run. Returns {"drug_name", "gcs_uri", "archive_uri"}."""
    with tempfile.TemporaryDirectory() as tmp:
        local_path = build_report(drug_name, shortlisted, arbitrage, str(Path(tmp) / "report.pdf"))
        gcs_uri, archive_uri = upload_report(
            drug_name, Path(local_path).read_bytes(), datetime.now().strftime("%Y%m%d_%H%M%S"),
            report_name=config.DIMENSION_PTE_ANALYSIS,
        )
    logger.info("[REPORT] %s: uploaded: %s | archive: %s", drug_name, gcs_uri, archive_uri)
    return {"drug_name": drug_name, "gcs_uri": gcs_uri, "archive_uri": archive_uri}
