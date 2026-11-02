"""
web_search.py
─────────────
Gemini web-search fallbacks.
"""

from ....gemini_utils import gemini_generate_async
from .constants import _GEMINI_MODEL


# ─────────────────────────────────────────────
# Web search fallbacks (Gemini)
# ─────────────────────────────────────────────

async def _search_company_for_drug(drug_name: str) -> str:
    """Uses Gemini with web search to identify the innovator company for a drug."""
    prompt = (
        f'Who is the original innovator / originator pharmaceutical company '
        f'that developed the drug "{drug_name}"?\n\n'
        f'Search the web thoroughly. Return ONLY the company name. No explanation.'
    )
    try:
        response = await gemini_generate_async(
            prompt, _GEMINI_MODEL, google_search=True, temperature=0.1,
        )
        name =(response.text or "").strip().strip(".")
        print(f"[SEARCH FALLBACK] Company for '{drug_name}': {name}")
        return name or ""
    except Exception as e:
        print(f"[SEARCH FALLBACK] Company search failed for '{drug_name}': {e}")
        return ""


async def _search_innovator_patterns(drug_name: str, company_name: str) -> str:
    """Uses Gemini with web search to gather innovator patent filing behaviour."""
    prompt = f"""You are a pharmaceutical patent intelligence analyst.

Research the patent filing patterns of {company_name or "the innovator company"} for the drug "{drug_name}".

Search Espacenet, Google Patents, and any other patent databases.

Analyse:
1. How many patents does the company hold for {drug_name}? List key patent numbers if found.
2. What types of patents? (composition of matter, formulation, device, method of treatment, dosing regimen, combination, salt/polymorph, manufacturing)
3. Does the company file continuation applications aggressively?
4. Does the company build dense patent thickets or file minimally?
5. Does the company expand protection late in development (after Phase 3 / approval)?
6. What is the company's overall IP strategy characterisation?

Provide a detailed summary with specific patent numbers and filing dates where available.
"""
    try:
        response = await gemini_generate_async(
            prompt, _GEMINI_MODEL, google_search=True, temperature=0.2,
        )
        result = (response.text or "").strip()
        if result:
            print(f"[SEARCH FALLBACK] Innovator patterns for '{drug_name}': {len(result)} chars")
            return f"INNOVATOR FILING PATTERNS FOR {drug_name} (from web search):\n{result}"
        return ""
    except Exception as e:
        print(f"[SEARCH FALLBACK] Innovator patterns search failed: {e}")
        return ""


async def _search_company_analysis(company_name: str, drug_name: str) -> str:
    """Uses Gemini with web search to gather company business strategy intelligence."""
    prompt = f"""You are a pharmaceutical business analyst.

Research {company_name}'s business strategy, focusing on their drug "{drug_name}" and the broader therapeutic area.

Search the web for:
1. {company_name}'s total annual revenue and revenue from {drug_name}
2. Pipeline assets related to {drug_name} or its therapeutic class
3. Recent management statements about {drug_name} strategy
4. Competitive landscape
5. Patent expiry dates and generic/biosimilar threats
6. New indications, formulations, devices, or combinations being developed
7. Lifecycle management strategies

Provide a detailed summary with specific numbers, dates, and quotes where available.
"""
    try:
        response = await gemini_generate_async(
            prompt, _GEMINI_MODEL, google_search=True, temperature=0.2,
        )
        result = (response.text or "").strip()
        if result:
            print(f"[SEARCH FALLBACK] Company analysis for '{company_name}': {len(result)} chars")
            return f"BUSINESS STRATEGY ASSESSMENT FOR {company_name} (from web search):\n{result}"
        return ""
    except Exception as e:
        print(f"[SEARCH FALLBACK] Company analysis search failed: {e}")
        return ""
