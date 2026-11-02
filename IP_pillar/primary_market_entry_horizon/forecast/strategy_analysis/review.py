"""
review.py
─────────
review_business_strategy - the business-strategy review for one company.
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional
from .assessment import _assess_patent_incentive
from .bq_storage import _write_to_bq
from .sec_edgar import _fetch_sec_10k
from .web_search import _search_supplemental, _vertex_search


# ─────────────────────────────────────────────────────────────────────────────
# Main public function
# ─────────────────────────────────────────────────────────────────────────────

def review_business_strategy(
    company: str,
    therapy_area: Optional[str] = None,
    drugs: Optional[List[str]] = None,
    write_bq: bool = True,
) -> Dict:
    drugs        = drugs or []
    therapy_area = therapy_area or "all therapeutic areas"

    print(f"\n{'═'*70}")
    print(f"  Business Strategy Review: {company}")
    print(f"  Scope: {therapy_area}")
    if drugs:
        print(f"  Drugs: {', '.join(drugs)}")
    print(f"{'═'*70}\n")

    all_sources: List[str] = []

    # Source 1 — SEC EDGAR
    print("[1/3] SEC EDGAR...")
    sec = _fetch_sec_10k(company)
    if sec:
        all_sources.append(sec)

    # Source 2 — Vertex AI Search
    print("[2/3] Vertex AI Search...")
    vas = _vertex_search(company, therapy_area)
    if vas:
        all_sources.append(vas)

    # Source 3 — Gemini web search
    print("[3/3] Gemini web search (supplemental)...")
    sup = _search_supplemental(company, therapy_area, drugs)
    if sup:
        all_sources.append(sup)

    if not all_sources:
        print("[STRATEGY] No intelligence gathered.")
        return {"error": "No intelligence gathered"}

    # Cap each source to avoid one noisy blob dominating the context
    _MAX_PER_SOURCE = 60_000
    capped = [s[:_MAX_PER_SOURCE] for s in all_sources]

    combined = "\n\n" + ("=" * 60 + "\n\n").join(capped)
    print(f"\n[STRATEGY] Combined: {len(combined)} chars from {len(all_sources)} source(s)")

    print("[STRATEGY] Running LLM assessment...")
    assessment = _assess_patent_incentive(company, therapy_area, drugs, combined)

    assessment["_metadata"] = {
        "company":      company,
        "therapy_area": therapy_area,
        "drugs":        drugs,
        "date":         datetime.now(timezone.utc).date().isoformat(),
        "sources_used": [s.split("\n")[0] for s in all_sources],
    }

    if write_bq:
        _write_to_bq(assessment)

    return assessment
