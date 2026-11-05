"""
forecast_generation.py
──────────────────────
Patent forecast for one drug (Gemini).
"""

import json
from typing import Dict, Optional
import pandas as pd
from ....gemini_utils import gemini_generate_async
from .constants import _CURRENT_DATE, _CURRENT_YEAR, _GEMINI_MODEL
from .post_processing import _validate_forecast


# ─────────────────────────────────────────────
# Generate patent forecast for a drug (Gemini)
# ─────────────────────────────────────────────

_DISPLAY_COLUMNS = {
    "patent_number": "Patent Number", "category": "Category", "layer": "Layer", "filing_date": "Filing Date",
    "phase_at_filing": "Phase at Filing", "description": "Description", "jurisdiction": "Jurisdiction",
    "insights": "Insights",
}


async def _generate_drug_forecast(
    drug_name: str,
    company_name: str,
    patent_data: Optional[pd.DataFrame],
    innovator_patterns: str,
    company_analysis: str,
) -> Dict:
    """Uses Gemini to synthesise all inputs and produce a patent filing forecast."""
    # forecast_s3 is stored in snake_case; the summary below reads the display names
    if patent_data is not None:
        patent_data = patent_data.rename(columns=_DISPLAY_COLUMNS)

    # Build patent summary from Step 2/3 data
    patent_summary_lines = []
    if patent_data is not None and not patent_data.empty:
        patent_summary_lines.append(f"EXISTING PATENT PORTFOLIO ({len(patent_data)} patents):")
        if "Category" in patent_data.columns:
            cats = patent_data["Category"].value_counts()
            patent_summary_lines.append(f"  Categories: {dict(cats)}")
        if "Layer" in patent_data.columns:
            layers = patent_data["Layer"].value_counts()
            patent_summary_lines.append(f"  Layers: {dict(layers)}")
        if "Phase at Filing" in patent_data.columns:
            phases = patent_data["Phase at Filing"].value_counts()
            patent_summary_lines.append(f"  Phase at Filing: {dict(phases)}")
        if "Jurisdiction" in patent_data.columns:
            juris = patent_data["Jurisdiction"].unique().tolist()
            patent_summary_lines.append(f"  Jurisdictions: {juris}")
        if "Filing Date" in patent_data.columns:
            dates = pd.to_datetime(patent_data["Filing Date"], errors="coerce").dropna()
            if not dates.empty:
                patent_summary_lines.append(
                    f"  Filing range: {dates.min().strftime('%Y')} — {dates.max().strftime('%Y')}")
        if "Insights" in patent_data.columns:
            insights = patent_data["Insights"].dropna().astype(str)
            insights = insights[insights.str.strip() != ""]
            if not insights.empty:
                patent_summary_lines.append(f"\n  LAYERING INSIGHTS:\n  {insights.iloc[0][:2000]}")

        cols_to_show = [c for c in ["Patent Number", "Category", "Layer", "Filing Date",
                                     "Phase at Filing", "Description"] if c in patent_data.columns]
        if cols_to_show:
            patent_summary_lines.append(f"\n  PATENT DETAILS (first 30):")
            for _, row in patent_data.head(30).iterrows():
                line = " | ".join(f"{c}: {row[c]}" for c in cols_to_show)
                patent_summary_lines.append(f"    {line}")

    patent_summary = ("\n".join(patent_summary_lines)
                      if patent_summary_lines
                      else "No existing patent data available.")

    prompt = f"""You are a senior pharmaceutical patent strategy forecaster with access to web search.

TODAY'S DATE: {_CURRENT_DATE}
CURRENT YEAR: {_CURRENT_YEAR}

DRUG: {drug_name}
COMPANY: {company_name or "Unknown — search the web to identify the innovator company"}

You have 3 sources of intelligence below. IF ANY SOURCE IS MISSING OR SAYS "No data available", YOU MUST USE WEB SEARCH to find that information yourself.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SOURCE 1: EXISTING PATENT PORTFOLIO (from Step 2/3 analysis)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{patent_summary}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SOURCE 2: INNOVATOR FILING BEHAVIOUR (from Step 4)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{innovator_patterns or "No innovator filing pattern data available. SEARCH THE WEB for patent filing behaviour of the innovator company for this drug."}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SOURCE 3: COMPANY BUSINESS STRATEGY (from Step 5)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{company_analysis or "No company analysis available. SEARCH THE WEB for business strategy, revenue, pipeline, and competitive landscape of the innovator company."}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TASK: Generate a COMPLETE PATENT FILING FORECAST for {drug_name} covering ALL patent types in ALL jurisdictions.

CRITICAL DATE RULE: Today is {_CURRENT_DATE}. This is a FORECAST of FUTURE patent filings.
- ALL filing_window values MUST start at {_CURRENT_YEAR} or later.
- If a patent type was already filed, forecast the NEXT expected filing starting from {_CURRENT_YEAR} or later.
- If no future filing is expected, still include with likelihood "Low" and filing_window starting at {_CURRENT_YEAR}.

PER-JURISDICTION PHASE REQUIREMENT:
For EACH jurisdiction, determine the drug's current regulatory/development phase IN THAT SPECIFIC JURISDICTION by searching the web.

Consider ALL of the following:
- FROM STEP 1 (IP Landscape): Patent categories already covered vs. missing
- FROM STEP 2 (Patent Layering): Patent protection tree structure and gaps
- FROM STEP 3 (Filing Pattern by Phase): Typical filing phase for each patent type
- FROM STEP 4 (Innovator Filing Behaviour): Dense thickets vs. minimal filing
- FROM STEP 5 (Business Strategy): Commercial importance, revenue dependency, lifecycle management
- SCORING CONTEXT: controlling_patent_expiry = lower_filing_year + 20; years_to_entry = expiry - current_year

PATENT TYPES (exactly 6):
  Device, Dosage Regimen, Formulation, Manufacturing Process, Method of treatment, Salt/Polymorph

JURISDICTIONS (exactly 12):
  CN, IN, BR, AU, RU, US, CA, JP, MX, TW, KR, EP

You MUST produce 6 × 12 = 72 entries.

For EACH entry provide:
- patent_type: exactly one of the 6 labels
- jurisdiction: exactly one of the 12 codes
- drug_phase_in_jurisdiction: simple phase label
- likelihood: Very High / High / Moderate / Low
- filing_window: year range starting at {_CURRENT_YEAR} or later
- rationale: 2-3 sentences with jurisdiction-specific reasoning
- strategic_purpose: business goal in this jurisdiction
- estimated_patents: integer (minimum 1)

Respond with JSON:
{{
  "drug": "{drug_name}",
  "company": "<company name>",
  "current_phase": "<most advanced phase globally>",
  "drug_class": "<e.g., GLP-1 receptor agonist peptide>",
  "existing_patent_summary": "<1-2 sentence summary>",
  "forecast": [
    {{
      "patent_type": "<type>",
      "jurisdiction": "<code>",
      "drug_phase_in_jurisdiction": "<phase>",
      "likelihood": "<level>",
      "filing_window": "<year range>",
      "rationale": "<2-3 sentences>",
      "strategic_purpose": "<goal>",
      "estimated_patents": <integer>
    }}
  ],
  "portfolio_gaps": ["<gap 1>", "<gap 2>"],
  "risk_assessment": "<2-3 sentences>",
  "overall_forecast": "<3-5 sentence executive summary>"
}}

CRITICAL RULES:
- 72 entries (6 types × 12 jurisdictions). No skipping.
- filing_window MUST start at {_CURRENT_YEAR} or later.
- estimated_patents MUST be >= 1.
- "company" field MUST contain the actual company name, never "Unknown".
"""

    try:
        response = await gemini_generate_async(
            prompt, _GEMINI_MODEL, google_search=True, temperature=0.2,
        )
        raw =(response.text or "").strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()
        forecast = json.loads(raw)
        forecast = _validate_forecast(forecast)
        return forecast

    except Exception as e:
        print(f"[FORECAST] Gemini forecast failed for {drug_name}: {e}")
        return {"drug": drug_name, "error": str(e)}
