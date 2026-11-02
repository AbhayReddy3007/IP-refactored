"""
forecast_helpers.py
───────────────────
The helpers forecast() calls (kept out of forecast.py, which holds only that function): the company lookup,
the three analysis steps (layering, filing pattern, business strategy), the text the forecast prompt reads,
and the PDF report.
"""

import asyncio
import logging
from typing import Dict, List

import pandas as pd

from ... import config
from .bq_utils import load_company_analysis, load_filing_pattern, write_company_analysis, write_filing_pattern
from .patent_layering import merge_to_forecast_s3, step1_ip_landscape, step2_patent_layering, step3_filing_analysis
from .patern_analysis import analyze_innovator_filing_patterns, filing_pattern_rows
from .strategy_analysis import _BQ_SCHEMA as COMPANY_ANALYSIS_SCHEMA
from .strategy_analysis import _assessment_to_bq_row, review_business_strategy
from .forecast_pipeline import _load_drug_company_map, _search_company_for_drug

logger = logging.getLogger(__name__)

THERAPY_AREA = "GLP-1"

# company_analysis_table sections: (column prefix, heading)
_COMPANY_SECTIONS = (
    ("rev", "Revenue dependency"), ("pipe", "Pipeline depth"), ("strat", "Strategic priority"),
    ("ind", "Indication expansion"), ("lc", "Lifecycle management"), ("comp", "Competitive pressure"),
)


# ─────────────────────────────────────────────
# Report and the text the forecast prompt reads
# ─────────────────────────────────────────────

def generate_report(drug: str, scored: List[Dict], patents: pd.DataFrame) -> dict:
    """Build and upload the dimension's report; {"error": ...} if it fails."""
    # Imported here so reportlab is only needed when a report is built.
    from .generate_report.generate_report import report_for_drug

    logger.info("[FORECAST] Generating the %s report ...", config.DIMENSION_FORECAST)
    try:
        report = report_for_drug(drug, scored, patents)
    except Exception as e:
        logger.exception("[FORECAST] Report failed for %r", drug)
        return {"error": str(e), "gcs_uri": None, "archive_uri": None}
    logger.info("[FORECAST] Report -> %s", report["gcs_uri"])
    return report


def innovator_text(drug: str, rows: List[Dict]) -> str:
    """The step 4 rows as the 'innovator filing behaviour' source of the forecast prompt."""
    if not rows:
        return ""
    lines = [f"INNOVATOR FILING PATTERNS FOR {drug}:"]
    for row in rows:
        lines.append(f"  {row.get('company', 'N/A')}: {row.get('characterization', 'N/A')} (confidence: {row.get('confidence', 'N/A')})")
        lines.append(f"    {row.get('rationale', 'N/A')}")
    return "\n".join(lines)


def company_text(company: str, row: Dict) -> str:
    """The step 5 row as the 'company business strategy' source of the forecast prompt."""
    if not row:
        return ""
    lines = [f"BUSINESS STRATEGY ASSESSMENT FOR {company}:"]
    for prefix, heading in _COMPANY_SECTIONS:
        lines.append(f"  {heading}: {row.get(f'{prefix}_rating') or 'N/A'}")
        details = str(row.get(f"{prefix}_details") or "").strip()
        if details and details.lower() not in ("nan", "n/a"):
            lines.append(f"    {details[:500]}")
    if row.get("pat_rating"):
        lines.append(f"  Patent expansion likelihood: {row['pat_rating']}")
    if row.get("overall_assessment"):
        lines.append(f"  Overall: {str(row['overall_assessment'])[:800]}")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# Steps
# ─────────────────────────────────────────────

async def resolve_company(drug: str) -> str:
    """The drug's parent company: the drug/company mapping in BigQuery, else a Gemini web search."""
    mapping = await asyncio.to_thread(_load_drug_company_map)
    info = mapping.get(drug.lower())
    if not info:
        norm = drug.lower().replace(" ", "").replace("-", "")
        info = next((v for k, v in mapping.items() if norm in k.replace(" ", "").replace("-", "")), None)
    if info and info.get("company"):
        return info["company"]
    logger.info("[FORECAST] %s: company not in the drug/company mapping - searching the web", drug)
    return (await _search_company_for_drug(drug)) or ""


async def layering(drug: str, loe_df: pd.DataFrame) -> pd.DataFrame:
    """Step 3: IP landscape + patent layering + filing stage, merged into forecast_s3."""
    landscape = await step1_ip_landscape(drug_filter={drug}, loe_df=loe_df)
    layering = await step2_patent_layering(drug_filter={drug}, loe_df=loe_df)
    filing_stage = await step3_filing_analysis(layering) if not layering.empty else layering
    return merge_to_forecast_s3(landscape, layering, filing_stage)


async def filing_pattern(drug: str, reset: bool) -> List[Dict]:
    """Step 4: the innovator's filing pattern; the rows of an earlier run are reused unless reset."""
    if not reset:
        cached = await asyncio.to_thread(load_filing_pattern, drug)
        if cached:
            logger.info("[FORECAST] %s: reusing %d filing-pattern row(s) from %s", drug, len(cached), config.BQ_FILING_PATTERN_TABLE)
            return cached
    result = await analyze_innovator_filing_patterns(drug)
    rows = filing_pattern_rows(drug, result) if result else []
    await asyncio.to_thread(write_filing_pattern, drug, rows)
    return rows


async def analyse_company(company: str, drug: str, reset: bool) -> Dict:
    """Step 5: the company's business strategy (shared by its drugs); an earlier run's row is reused unless reset."""
    if not company:
        return {}
    if not reset:
        cached = await asyncio.to_thread(load_company_analysis, company)
        if cached:
            logger.info("[FORECAST] %s: reusing the %s business-strategy analysis from %s", drug, company, config.BQ_COMPANY_ANALYSIS_TABLE)
            return cached
    assessment = await asyncio.to_thread(review_business_strategy, company, THERAPY_AREA, [drug], False)
    if "error" in assessment:
        logger.warning("[FORECAST] %s: no business-strategy analysis for %s (%s)", drug, company, assessment["error"])
        return {}
    row = _assessment_to_bq_row(assessment)
    try:
        await asyncio.to_thread(write_company_analysis, company, row, COMPANY_ANALYSIS_SCHEMA)
    except Exception:
        # The analysis is still used for this forecast; only its storage failed.
        logger.exception("[FORECAST] %s: could not store the %s analysis in %s", drug, company, config.BQ_COMPANY_ANALYSIS_TABLE)
    return row
