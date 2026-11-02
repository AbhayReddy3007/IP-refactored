"""
web_search.py
─────────────
Vertex AI Search and supplemental Gemini search.
"""

import os
from typing import List
from google.genai import types
from ....gemini_utils import gemini_generate
from .constants import GEMINI_MODEL


# ─────────────────────────────────────────────────────────────────────────────
# Source 2 — Vertex AI Search
# ─────────────────────────────────────────────────────────────────────────────

def _vertex_search(company: str, therapy_area: str) -> str:
    """
    Uses the Vertex AI Search grounding tool (via Gemini) to pull
    investor presentations and annual-report content.
    The grounding backend queries the public web corpus that is indexed
    by Google Search — no custom data store required.

    If you have a private Vertex AI Search data store containing your
    company documents, set VERTEX_SEARCH_ENGINE_ID and the code will
    use it instead.
    """
    engine_id = os.getenv("VERTEX_SEARCH_ENGINE_ID", "")

    queries = [
        f"{company} latest investor presentation earnings slides",
        f"{company} annual report 2024 2025 revenue pipeline strategy",
        f"{company} {therapy_area} product revenue pipeline patent strategy",
    ]

    results = []
    for q in queries:
        try:
            if engine_id:
                # ── Private data store ─────────────────────────────────────
                tool = types.Tool(
                    retrieval=types.Retrieval(
                        vertex_ai_search=types.VertexAISearch(datastore=engine_id)
                    )
                )
            else:
                # ── Google Search grounding (public web) ───────────────────
                tool = types.Tool(google_search=types.GoogleSearch())

            response = gemini_generate(
                f"Find and summarise the most relevant content about: {q}\n"
                f"Focus on revenue figures, pipeline assets, strategy statements, "
                f"and patent/IP information. Be specific with numbers and dates.",
                GEMINI_MODEL,
                tools=[tool],
            )
            text = response.text or ""
            if text.strip():
                results.append(f"QUERY: {q}\n\n{text.strip()}")
                print(f"[VERTEX SEARCH] {len(text)} chars for: {q[:60]}")
        except Exception as e:
            print(f"[VERTEX SEARCH] Failed for '{q}': {e}")

    if not results:
        return ""

    combined = "\n\n---\n\n".join(results)
    return f"SOURCE: Vertex AI Search (investor presentations + annual reports)\n\n{combined}"


# ─────────────────────────────────────────────────────────────────────────────
# Source 3 — Gemini web search (supplemental grounding)
# ─────────────────────────────────────────────────────────────────────────────

def _search_supplemental(company: str, therapy_area: str, drugs: List[str]) -> str:
    drug_list       = ", ".join(drugs) if drugs else ""
    is_company_wide = therapy_area == "all therapeutic areas"

    if is_company_wide:
        focus = (
            f"1. {company} total annual revenue breakdown by therapeutic area with exact figures.\n"
            f"2. {company} full pipeline across all therapeutic areas.\n"
            f"3. CEO / management quotes about overall strategy (last 12 months).\n"
            f"4. All patent cliffs, generic/biosimilar threats across the portfolio.\n"
            f"5. Top 10 products by revenue with growth rates."
        )
    elif drug_list:
        focus = (
            f"1. {company} revenue from {drug_list} with exact figures and YoY growth.\n"
            f"2. Pipeline assets related to {drug_list}.\n"
            f"3. CEO quotes about {drug_list} strategy.\n"
            f"4. Patent expiry dates and generic/biosimilar threats for {drug_list}.\n"
            f"5. New indications and formulations being developed for {drug_list}."
        )
    else:
        focus = (
            f"1. {company} revenue from {therapy_area} with individual product figures.\n"
            f"2. Full pipeline in {therapy_area} (all phases).\n"
            f"3. CEO / management quotes about {therapy_area} strategy.\n"
            f"4. Patent expiry timeline for {company}'s {therapy_area} portfolio.\n"
            f"5. Competitive landscape in {therapy_area}."
        )

    prompt = (
        f"You are a pharmaceutical business analyst. Search for the most recent "
        f"information about {company}.\n\nFind:\n{focus}\n\n"
        f"Use only factual sourced information. Cite the source for each finding. "
        f"Every point must have substantive content."
    )

    try:
        response = gemini_generate(prompt, GEMINI_MODEL, google_search=True)
        text     = (response.text or "").strip()
        print(f"[SEARCH] {len(text)} chars of supplemental intelligence")
        return f"SOURCE: Gemini web search (supplemental)\n\n{text}"
    except Exception as e:
        print(f"[SEARCH] Web search failed: {e}")
        return ""
