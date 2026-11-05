"""
assessment.py
─────────────
Assessment prompt, parsing and the Gemini assessment.
"""

import json
from typing import Dict, List, Optional
from ....gemini_utils import gemini_generate
from .constants import GEMINI_MODEL


# ─────────────────────────────────────────────────────────────────────────────
# LLM assessment
# ─────────────────────────────────────────────────────────────────────────────

def _build_assessment_prompt(
    company: str,
    therapy_area: str,
    drugs: List[str],
    combined: str,
) -> str:
    drug_list       = ", ".join(drugs) if drugs else ""
    is_company_wide = therapy_area == "all therapeutic areas"

    if is_company_wide:
        scope_desc  = f"across {company}'s entire portfolio"
        rev_instr   = (
            f"Exact total revenue + breakdown by every therapeutic area. "
            f"Top 5 products by revenue. YoY growth per segment."
        )
        pipe_instr  = f"All pipeline assets grouped by therapy area."
        strat_instr = f"Overall strategic direction, priority therapy areas, pivots."
        ind_instr   = f"New indications across all therapy areas grouped by product."
        lc_instr    = f"Lifecycle strategies across all key products."
        comp_instr  = f"Competitive threats + patent cliffs across the entire portfolio."
        pat_instr   = f"Patent expansion across the entire company — by therapy area."
    elif drug_list:
        scope_desc  = f"focused on {drug_list} in {therapy_area}"
        rev_instr   = f"Exact revenue per drug: {drug_list}. % of total. YoY growth."
        pipe_instr  = f"All pipeline in {therapy_area} or related to {drug_list}."
        strat_instr = f"Management quotes specifically about {drug_list}."
        ind_instr   = f"Every new indication being pursued for {drug_list}."
        lc_instr    = f"All lifecycle strategies for {drug_list}."
        comp_instr  = f"Direct competitors to {drug_list}. Patent expiry dates."
        pat_instr   = f"Patent expansion specifically for {drug_list}."
    else:
        scope_desc  = f"focused on {company}'s {therapy_area} portfolio"
        rev_instr   = (
            f"Exact {therapy_area} segment revenue. Individual product figures. "
            f"% of total. YoY growth."
        )
        pipe_instr  = f"All pipeline in {therapy_area}."
        strat_instr = f"Management quotes about {therapy_area} as a strategic priority."
        ind_instr   = f"All new indications across {company}'s {therapy_area} portfolio."
        lc_instr    = f"Lifecycle strategies across all {therapy_area} products."
        comp_instr  = f"All competitors in {therapy_area}. Patent expiry timeline."
        pat_instr   = f"Patent expansion across the entire {therapy_area} portfolio."

    return f"""You are a senior pharmaceutical patent strategy analyst.

Company: {company}
Scope: {scope_desc}

Intelligence gathered from SEC filings, Vertex AI Search, and web search:

{combined[:90_000]}

Provide a detailed structured assessment {scope_desc}.

RULES:
- Every field must have substantive content. Never leave anything blank or write "N/A".
- Use exact numbers, dates, drug names and quotes from the source material.
- If exact figures are unavailable, provide a reasoned estimate clearly labelled as estimated.

1. REVENUE DEPENDENCY        : {rev_instr}   Rating: Critical >40% | High 20-40% | Moderate 10-20% | Low <10%
2. PIPELINE DEPTH            : {pipe_instr}  Rating: Deep 5+ | Moderate 3-4 | Shallow 1-2 | None
3. STRATEGIC PRIORITY        : {strat_instr} Rating: Core Franchise | Growth Driver | Maintained | De-prioritised
4. INDICATION EXPANSION      : {ind_instr}   Rating: Aggressive | Moderate | Minimal | None
5. LIFECYCLE MANAGEMENT      : {lc_instr}    Rating: Active | Planned | Minimal | None
6. COMPETITIVE PRESSURE      : {comp_instr}  Rating: Imminent <2yr | Near-term 2-5yr | Distant >5yr | None visible
7. PATENT EXPANSION LIKELIHOOD: {pat_instr}  Rating: Very High | High | Moderate | Low
   Assess each type: formulation | device | dosing regimen | combination | method of treatment | manufacturing
8. OVERALL ASSESSMENT: 5-7 sentence executive summary of commercial incentive for patent expansion.

Respond ONLY with valid JSON (no markdown fences):
{{
  "revenue_dependency": {{
    "rating": "<Critical|High|Moderate|Low>",
    "total_company_revenue": "<figure + currency + year>",
    "therapy_area_revenue": "<figure or segment breakdown>",
    "percentage": "<X% of total>",
    "growth_rate": "<YoY %>",
    "drug_breakdown": "<revenue per drug/segment>",
    "details": "<detailed analysis with specific numbers>"
  }},
  "pipeline_depth": {{
    "rating": "<Deep|Moderate|Shallow|None>",
    "assets": ["<name — mechanism — stage — indication — milestone>"],
    "recent_acquisitions": "<acquisitions>",
    "details": "<detailed analysis>"
  }},
  "strategic_priority": {{
    "rating": "<Core Franchise|Growth Driver|Maintained|De-prioritised>",
    "key_quotes": ["<exact quote — source — date>"],
    "restructuring": "<organisational changes>",
    "details": "<detailed analysis>"
  }},
  "indication_expansion": {{
    "rating": "<Aggressive|Moderate|Minimal|None>",
    "planned_indications": ["<indication — drug — stage — trial — timeline>"],
    "patent_implications": "<which indications could generate new patents>",
    "details": "<detailed analysis>"
  }},
  "lifecycle_management": {{
    "rating": "<Active|Planned|Minimal|None>",
    "strategies": ["<strategy type — product — stage — evidence>"],
    "new_formulations": "<formulation programmes>",
    "new_devices": "<device programmes>",
    "new_dosing": "<dosing programmes>",
    "combinations": "<combination programmes>",
    "details": "<detailed analysis>"
  }},
  "competitive_pressure": {{
    "rating": "<Imminent|Near-term|Distant|None visible>",
    "competitors": ["<product — company — status — market share>"],
    "patent_expiry_dates": "<known expiry dates>",
    "biosimilar_threats": "<biosimilar/generic threats>",
    "defense_strategy": "<company's competitive strategy>",
    "details": "<detailed analysis>"
  }},
  "patent_expansion_likelihood": {{
    "rating": "<Very High|High|Moderate|Low>",
    "formulation_patents": "<detailed assessment with evidence>",
    "device_patents": "<detailed assessment with evidence>",
    "dosing_regimen_patents": "<detailed assessment with evidence>",
    "combination_patents": "<detailed assessment with evidence>",
    "method_of_treatment_patents": "<detailed assessment with evidence>",
    "manufacturing_patents": "<detailed assessment with evidence>"
  }},
  "overall_assessment": "<5-7 sentence executive summary>"
}}
"""


def _parse_json_response(raw: str) -> Optional[Dict]:
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw   = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        return json.loads(raw.strip())
    except Exception:
        return None


def _na_ratio(d: Dict) -> float:
    _NA = {"n/a","na","","none","null","insufficient data","not available",
           "data not available","unknown","—","see overall assessment"}
    total = na = 0
    for k, v in d.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict):
            for _, vv in v.items():
                total += 1
                if str(vv).strip().lower() in _NA:
                    na += 1
        elif isinstance(v, str):
            total += 1
            if v.strip().lower() in _NA:
                na += 1
    return na / max(total, 1)


def _assess_patent_incentive(
    company: str,
    therapy_area: str,
    drugs: List[str],
    combined: str,
) -> Dict:
    prompt = _build_assessment_prompt(company, therapy_area, drugs, combined)
    try:
        resp   = gemini_generate(prompt, GEMINI_MODEL)
        parsed = _parse_json_response(resp.text or "")
        if parsed:
            ratio = _na_ratio(parsed)
            print(f"[STRATEGY] JSON parsed — {ratio:.0%} empty fields")
            if ratio < 0.4:
                return parsed
            print("[STRATEGY] Too many empty fields — trying direct Gemini research...")
    except Exception as e:
        print(f"[STRATEGY] Assessment failed: {e}")

    return _direct_gemini_report(company, therapy_area, drugs)


def _direct_gemini_report(company: str, therapy_area: str, drugs: List[str]) -> Dict:
    """Last-resort: ask Gemini to research the company itself with web search."""
    drug_list = ", ".join(drugs) if drugs else ""
    scope = (
        f"the entire company {company}"
        if therapy_area == "all therapeutic areas"
        else (f"{company}'s {therapy_area} portfolio, specifically {drug_list}"
              if drug_list else f"{company}'s {therapy_area} portfolio")
    )

    prompt = (
        f"You are a senior pharmaceutical patent strategy analyst. "
        f"Research {company} thoroughly using web search.\n\n"
        f"Write a comprehensive business strategy and patent expansion assessment for {scope}.\n\n"
        f"Cover: revenue, pipeline, strategic direction, indication expansion, "
        f"lifecycle management, competitive landscape, patent/IP strategy.\n"
        f"Never write 'insufficient data'. Provide estimates if exact numbers are unavailable.\n\n"
        f"Respond ONLY with valid JSON (no markdown):\n"
        f'{{"revenue_dependency":{{"rating":"","total_company_revenue":"","therapy_area_revenue":"",'
        f'"percentage":"","growth_rate":"","drug_breakdown":"","details":""}},'
        f'"pipeline_depth":{{"rating":"","assets":[],"recent_acquisitions":"","details":""}},'
        f'"strategic_priority":{{"rating":"","key_quotes":[],"restructuring":"","details":""}},'
        f'"indication_expansion":{{"rating":"","planned_indications":[],"patent_implications":"","details":""}},'
        f'"lifecycle_management":{{"rating":"","strategies":[],"new_formulations":"","new_devices":"",'
        f'"new_dosing":"","combinations":"","details":""}},'
        f'"competitive_pressure":{{"rating":"","competitors":[],"patent_expiry_dates":"",'
        f'"biosimilar_threats":"","defense_strategy":"","details":""}},'
        f'"patent_expansion_likelihood":{{"rating":"","formulation_patents":"","device_patents":"",'
        f'"dosing_regimen_patents":"","combination_patents":"","method_of_treatment_patents":"",'
        f'"manufacturing_patents":""}},"overall_assessment":""}}'
    )
    try:
        response = gemini_generate(prompt, GEMINI_MODEL, google_search=True)
        parsed   =_parse_json_response(response.text or "")
        if parsed:
            print("[STRATEGY] Direct Gemini report succeeded")
            return parsed
    except Exception as e:
        print(f"[STRATEGY] Direct Gemini report failed: {e}")

    return {
        "overall_assessment": f"Assessment generation failed for {company}.",
        "revenue_dependency":          {"rating": "—", "details": "Failed"},
        "pipeline_depth":              {"rating": "—", "details": "Failed"},
        "strategic_priority":          {"rating": "—", "details": "Failed"},
        "indication_expansion":        {"rating": "—", "details": "Failed"},
        "lifecycle_management":        {"rating": "—", "details": "Failed"},
        "competitive_pressure":        {"rating": "—", "details": "Failed"},
        "patent_expansion_likelihood": {"rating": "—", "details": "Failed"},
    }
