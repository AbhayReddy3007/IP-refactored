"""
analysis.py
───────────
analyze_innovator_filing_patterns - the innovator filing pattern for one drug.
"""
from .constants import _GEMINI_MODEL, _REGISTRIES
from .gemini_search import _gemini_call_with_search, _parse_json_response



# ─────────────────────────────────────────────
# Core analysis (per drug)
# ─────────────────────────────────────────────

async def analyze_innovator_filing_patterns(drug_name: str) -> dict:
    """Analyse innovator patent filing patterns for a single drug.

    Returns a dict shaped like:
        {
          "innovators": [
            {"company": str, "characterization": str,
             "confidence": float, "rationale": str},
            ...
          ],
          "combined_characterization": str,
          "combined_confidence": float,
          "combined_rationale": str,
        }
    Returns an empty dict on failure.
    """
    print(f"\n{'='*80}")
    print(f"  INNOVATOR FILING PATTERN ANALYSIS — {drug_name}")
    print(f"{'='*80}")
    print(f"  Model: {_GEMINI_MODEL}")

    registry_urls = "\n".join(f"- {r['url']}" for r in _REGISTRIES)

    prompt = f"""You are a pharmaceutical patent intelligence analyst. Analyze the patent filing patterns for the innovator company behind "{drug_name}".

TASK:
1. Identify ALL innovator companies for {drug_name} — include co-developers, original discoverers, and licensors that hold patents on this drug. Many drugs have 2+ innovators (e.g., co-development agreements, in-licensed compounds).
2. For EACH innovator: search Espacenet, Google Patents, and SEC EDGAR to analyze their patent filing behavior independently.
3. Look at each company's patterns across their broader portfolio (not just {drug_name}).

DATA SOURCES (search ALL for complete data):
- https://worldwide.espacenet.com/
- https://patents.google.com/
- https://www.sec.gov/cgi-bin/browse-edgar
{registry_urls}

ANALYZE - For each innovator, evaluate whether they typically:

1. FILES MULTIPLE CONTINUATION APPLICATIONS:
   - Count continuation-in-part (CIP) and continuation applications
   - Look for chains of related applications spanning many years
   - Check if they file continuations near patent expiry to extend protection
   - Identify "submarine" continuation strategies (delayed prosecution)

2. EXPANDS PROTECTION LATE IN DEVELOPMENT:
   - Look for patents filed after Phase 3 trials or FDA approval
   - Identify late-filed method-of-use patents for new indications
   - Check for dosing regimen patents filed years after original compound patent
   - Look for "evergreening" tactics near original patent expiry

3. BUILDS DENSE PATENT THICKETS:
   - Count total patents covering a single drug (>20 = dense thicket)
   - Identify overlapping claims across multiple patents
   - Look for blocking patents that prevent generic entry from multiple angles
   - Check for picket fence strategies (many narrow patents vs few broad ones)

4. FILES MULTIPLE FORMULATION VARIATIONS:
   - Count polymorph/crystal form patents
   - Identify extended-release, controlled-release, or modified-release patents
   - Look for combination product patents (fixed-dose combinations)
   - Check for device/delivery system patents (auto-injectors, pens, patches)
   - Identify pediatric formulation patents
   - Look for salt form and prodrug patents

5. OTHER PATTERNS:
   - Patent family sizes across their portfolio
   - Geographic filing strategy (US-only vs worldwide)
   - Historical behavior consistency across other products

OUTPUT:
For EACH innovator, provide a SHORT 1-LINE TITLE characterizing their IP filing strategy.
Maximum 5 words, ideally 3-4 words. Be specific and situational based on evidence found.

Examples (create your own based on evidence):
- "Dense lifecycle patent thickets"
- "Formulation-focused secondary patents"
- "Minimal single composition patent"
- "Heavy continuation near expiry"
- "Broad method-of-use defense"
- "Late-stage polymorph evergreening"
- "Extensive formulation/device patents"

Also provide a COMBINED characterization that reflects the overall IP barrier landscape created by ALL innovators together.
If strategies differ, the combined characterization should reflect the most aggressive / highest-barrier strategy (since generics must clear all thickets).

Return as JSON:

{{
  "innovators": [
    {{
      "company": "",
      "characterization": "",
      "confidence": 0.0,
      "rationale": ""
    }}
  ],
  "combined_characterization": "",
  "combined_confidence": 0.0,
  "combined_rationale": ""
}}

IMPORTANT:
- If only one innovator exists, "innovators" array will have one entry and combined_characterization = that entry's characterization.
- characterization: SHORT 1-LINE TITLE (max 5 words, ideally 3-4) - not a paragraph
- confidence per innovator: How well the evidence SUPPORTS THE SPECIFIC CHARACTERIZATION YOU CHOSE (not just how many patents you found).
  Score based on these 3 factors:

  HARD CEILING RULES (apply first, before scoring):
  - If you could NOT find specific patent numbers for this drug → max score is 0.5
  - If the drug is early-stage (Phase 1/2) with limited filing history → max score is 0.6
  - If you are inferring strategy from company reputation, not verified filings → max score is 0.4
  - If the drug is obscure or has sparse patent data online → max score is 0.5

  Then score these 3 factors (within the ceiling):

  1. PATTERN CONSISTENCY (0–0.4): Do the patents found all point to the same strategy, or are they mixed?
     - All patents clearly show the same pattern → 0.4
     - Mostly consistent with some outliers → 0.2–0.3
     - Mixed/conflicting signals → 0.1

  2. EVIDENCE DIRECTNESS (0–0.4): Did you find patents that directly confirm the strategy you named?
     - Found and verified specific patent numbers (e.g., US10XXXXXX) → 0.4
     - Found descriptions/summaries but not verified patent numbers → 0.2
     - Inferred from general company reputation only → 0.0–0.1

  3. PORTFOLIO BREADTH (0–0.2): Did the pattern hold across multiple products (not just {drug_name})?
     - Pattern confirmed across 3+ products with evidence → 0.2
     - Confirmed for {drug_name} only → 0.1
     - Could not verify across portfolio → 0.0

  Add the 3 factors, then apply the ceiling. 0.8+ requires: specific patent numbers found + consistent pattern + multi-product evidence.
  NEVER assign 1.0 — maximum allowed score is 0.9.

- combined_confidence: average of all innovator confidence scores, then apply the same ceiling rules above.

- rationale: AFTER calculating confidence using the rules above, add a plain English explanation of the IP strategy. Do NOT include patent numbers or scoring breakdowns.
"""

    print("  Analyzing filing patterns...")
    response = await _gemini_call_with_search(prompt)
    data = _parse_json_response(response)

    if not data:
        print("  Failed to analyze filing patterns")
        return {}

    return data
