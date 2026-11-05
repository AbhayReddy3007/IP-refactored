"""Gemini prompt templates for the circumvention analysis."""

# ── Circumvention Prompts ─────────────────────────────────────────────────────

CIRCUMVENTION_PROMPT = """
You are a pharmaceutical patent attorney specializing in 505(b)(2) regulatory strategy
and patent design-around analysis.

Below are the most relevant excerpts from non-blocking patent(s) in the "{claim_category}"
category for the drug "{drug}":

--- PATENT EXCERPTS ---
{chunks}
--- END EXCERPTS ---

Patents in this category: {patent_numbers}

Drug: {drug}

Objective: Identify potential non-infringing product designs that avoid the innovator's
patent claims. A design-around avoids infringement by omitting at least one key claim
limitation from each relevant patent.

Evaluation Steps:
1. Identify the key claim limitations in the independent claims for each patent.
2. For the "{claim_category}" category, evaluate whether alternative solutions exist
   that omit at least one key limitation.
3. Consider known approaches from FDA Drugs@FDA approval packages, FDA Orange Book
   listed patents, and scientific literature.
4. Assess whether a non-infringing product modification could still be regulatorily
   viable under a 505(b)(2) pathway.

For context, here are example design-around strategies by category:
- Formulation: Alternative excipient or stabilization chemistry; device-based mixing prior to injection
- API Ratio / Dosing: Different clinically justified ratio outside scope of dosing claims
- Delivery Device: Mechanically distinct injector design avoiding utility or design patent claims
- Manufacturing: Different synthesis pathway (process patents are often narrow)
- Method of Use: New therapeutic indication (may obtain separate exclusivity)

Respond ONLY with a valid JSON object — no markdown, no explanation outside the JSON:
{{
  "claim_category": "{claim_category}",
  "key_claim_limitations": ["<limitation 1>", "<limitation 2>", "<limitation 3>"],
  "design_around_strategies": [
    {{
      "strategy": "<concise description of the design-around approach>",
      "rationale": "<why this avoids infringement — which claim limitation is omitted>",
      "feasibility": "High / Medium / Low",
      "regulatory_pathway": "<how this could work under 505(b)(2) or other pathway>",
      "prior_art_support": "<any known precedent from FDA approvals, Orange Book, or literature>"
    }}
  ],
  "white_space_opportunities": ["<opportunity 1>", "<opportunity 2>"],
  "overall_circumvention_difficulty": "Easy / Moderate / Difficult",
  "summary": "<2-3 sentence overall assessment of circumvention potential for this category>"
}}
"""

CIRCUMVENTION_SEARCH_PROMPT = """
You are a pharmaceutical patent analyst. Search for 505(b)(2) design-around opportunities
for the drug "{drug}" in the "{claim_category}" patent category.

Specifically search for:
1. FDA Drugs@FDA: alternative formulations, dosing regimens, delivery devices, or new indications
   for {drug} or similar molecules
2. FDA Orange Book: listed patents and expiry dates to identify product features NOT protected
3. Scientific literature: {drug} + {claim_category_lower} alternatives, stability studies,
   delivery innovations

Return JSON:
{{
  "fda_precedents": ["<relevant FDA approval or product that demonstrates an alternative approach>"],
  "orange_book_gaps": ["<product feature or design element not covered by listed patents>"],
  "literature_alternatives": ["<published alternative approach from scientific literature>"],
  "regulatory_viability": "<assessment of whether non-infringing modifications are regulatorily feasible>"
}}

If no information found for a field, return an empty list for that field.
"""

# ── Online Circumvention Prompt (used when no AlloyDB chunks are available) ───
# Combines the chunk-based and search-based prompts into a single grounded call
# so we still produce a full, consistent result when local patent text is absent.

CIRCUMVENTION_ONLINE_PROMPT = """
You are a pharmaceutical patent attorney specialising in 505(b)(2) regulatory
strategy and patent design-around analysis.

No patent text is available in the local database for the "{claim_category}"
category patents of "{drug}" (patent numbers: {patent_numbers}).

Using publicly available information — FDA Drugs@FDA, the FDA Orange Book,
scientific literature, and patent databases (e.g. USPTO, Espacenet) — perform
a full circumvention analysis for this category.

Evaluation Steps:
1. Identify the typical key claim limitations found in "{claim_category}" patents
   for drugs like "{drug}" based on public patent databases.
2. Evaluate whether alternative solutions exist that omit at least one key limitation.
3. Consider known approaches from FDA Drugs@FDA approval packages, FDA Orange Book
   listed patents, and scientific literature.
4. Assess whether a non-infringing product modification could still be regulatorily
   viable under a 505(b)(2) pathway.

Return ONLY a valid JSON object — no markdown, no explanation outside the JSON:
{{
  "claim_category": "{claim_category}",
  "key_claim_limitations": ["<limitation 1>", "<limitation 2>", "<limitation 3>"],
  "design_around_strategies": [
    {{
      "strategy": "<concise description of the design-around approach>",
      "rationale": "<why this avoids infringement — which claim limitation is omitted>",
      "feasibility": "High / Medium / Low",
      "regulatory_pathway": "<how this could work under 505(b)(2) or other pathway>",
      "prior_art_support": "<any known precedent from FDA approvals, Orange Book, or literature>"
    }}
  ],
  "white_space_opportunities": ["<opportunity 1>", "<opportunity 2>"],
  "overall_circumvention_difficulty": "Easy / Moderate / Difficult",
  "fda_precedents": ["<relevant FDA approval or product demonstrating an alternative approach>"],
  "orange_book_gaps": ["<product feature or design element not covered by listed patents>"],
  "literature_alternatives": ["<published alternative approach from scientific literature>"],
  "regulatory_viability": "<assessment of whether non-infringing modifications are regulatorily feasible>",
  "summary": "<2-3 sentence overall assessment of circumvention potential for this category, noting that analysis is based on public sources as patent text was unavailable in the local database>"
}}
"""

