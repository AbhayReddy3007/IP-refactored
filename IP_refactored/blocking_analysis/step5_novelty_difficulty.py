"""
step5_novelty_difficulty.py
───────────────────────────
STEP 5 — Is the claimed design novel and technically difficult? (FINAL call)

A patent is BLOCKING only if all four hold:
  1. product practices the claim        (Step 2)
  2. feature solves a technical problem (Step 3)
  3. removal blocks approval / restarts development (Step 4)
  4. the solution is genuinely novel and technically difficult (this step)

Only a HIGH-confidence BLOCKING verdict is kept; anything else → NON-BLOCKING.
"""

from pathlib import Path
from typing import Dict, Optional

from .step1_claim_classification import _call_gemini_json
from .step3_scientific_barrier import _cap_evidence


STEP5_PROMPT = """You are a senior pharmaceutical patent expert making a FINAL blocking classification.
You are a STRICT REVIEWER. Default to NON-BLOCKING unless all four conditions are explicitly met.

DRUG NAME      : {drug_name}
PATENT NUMBER  : {patent_number}
JURISDICTION   : {jurisdiction}
CLAIM CATEGORY : {claim_category}
CLAIM DETAILS  : {step1_reason}

PRIOR STEP FINDINGS:
  Step 3 (Technical Barrier) : {step3_summary}
  Step 4 (Dev. Proceed?)     : {step4_reason}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SCIENTIFIC & REGULATORY EVIDENCE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{evidence_block}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FULL PATENT DOCUMENT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{context}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

─────────────────────────────────────────────
TASK — STEP 5: NOVELTY & TECHNICAL DIFFICULTY ASSESSMENT
─────────────────────────────────────────────

Assess whether the patented feature is a genuinely innovative and technically
demanding solution, then make the FINAL blocking classification.

─────────────────────────────────────────────
PART A — NOVELTY & TECHNICAL DIFFICULTY
─────────────────────────────────────────────

Using the ENTIRE patent document — read every section including the title,
abstract, technical field, background, summary, detailed description, examples,
figures, and all numbered or unnumbered paragraphs — AND the scientific/regulatory
evidence, assess what the patent protects and whether it meets the criteria below.
Do NOT limit your reading to any section labelled "Claims". Understand the full
scope of the invention from all technical content in the document.

1. Does the patent solve a previously UNRESOLVED technical problem?
   (Not just an improvement — a problem that had no prior working solution)

2. Does prior art or the patent's background section describe FAILED ATTEMPTS
   before this solution was found?

3. Is the feature described as proprietary, strategically critical, or protected
   as a core innovation in SEC filings (10-K/20-F) or company disclosures?

4. Does implementation require COMPLEX formulation science, manufacturing
   process controls, or device engineering that is not standard industry practice?

INDICATORS OF HIGH NOVELTY / DIFFICULTY (strengthens BLOCKING):
  ✓ First-in-class solution with no prior working equivalent
  ✓ Addresses core stability, PK/PD, or manufacturability barrier
  ✓ Narrow technical pathway — very few viable alternatives
  ✓ Complex process controls or validation requirements
  ✓ Significant documented R&D investment (SEC filings, publications)
  ✓ Prior art shows failed attempts at solving the same problem

INDICATORS OF LOW NOVELTY / DIFFICULTY (strengthens NON-BLOCKING):
  ✗ Routine optimisation of known parameters
  ✗ Standard excipient selection or device use common in the field
  ✗ Broad, forgiving formulation ranges that others could replicate easily
  ✗ Common industry approach applied to a new context
  ✗ No evidence of prior failed attempts or technical struggle

─────────────────────────────────────────────
PART B — FINAL CLASSIFICATION
─────────────────────────────────────────────

A patent is BLOCKING only if ALL FOUR of the following are true:
  1. The product practices the claim (confirmed in Step 2)
  2. The feature solves a necessary technical problem (confirmed in Step 3)
  3. Removing it would prevent approval or require restarting development (confirmed in Step 4)
  4. The solution is genuinely novel and technically difficult (assessed here in Step 5)

If ANY of these four conditions is not met → NON-BLOCKING.

─────────────────────────────────────────────
STRICT DECISION RULES:
─────────────────────────────────────────────
- Steps 2, 3, 4 are already confirmed for patents reaching Step 5.
  Your job is to assess condition 4 and make the final call.
- High novelty + technical difficulty → BLOCKING
- Routine optimisation, standard practice, or low difficulty → NON-BLOCKING
- If evidence of novelty/difficulty is ambiguous or absent → NON-BLOCKING
- Confidence must be HIGH for a BLOCKING verdict. If uncertain → NON-BLOCKING.

─────────────────────────────────────────────
OUTPUT — return ONLY valid JSON, no markdown:
─────────────────────────────────────────────
{{
  "is_novel_and_difficult":   true or false,
  "novelty_signal":           "high" or "medium" or "low",
  "first_in_class":           true or false,
  "prior_failed_attempts":    true or false,
  "complex_implementation":   true or false,
  "final_tag":                "BLOCKING" or "NON-BLOCKING",
  "blocking_category":        "Composition of Matter" or "Co-formulation/formulation" or "Delivery device required for use" or "Method of treatment claimed broadly" or null,
  "confidence":               "high" or "medium" or "low",
  "reason":                   "2-3 sentences: what makes this novel/not novel, and why the final classification follows"
}}

blocking_category rules:
  - Must be one of the four exact strings above, or null
  - Set to null if final_tag is NON-BLOCKING
  - If BLOCKING, use the claim_category from Step 1 unless the evidence warrants a different category
"""


async def _run_step5(
    filename:     str,
    context:      str,
    step1_result: Dict,
    step2_result: Dict,
    step3_result: Dict,
    step4_result: Dict,
    drug_name:    str,
) -> Optional[Dict]:
    """
    Step 5: Is the claimed design novel and technically difficult?

    Assesses whether the patented feature is a genuinely innovative and
    technically demanding solution vs. a routine improvement.

    Sources used:
    - Patent specification (RAG context)
    - Scientific/regulatory evidence gathered in Step 3 (reused)

    Final classification:
    - BLOCKING if all four conditions met: product practices claim,
      feature solves necessary technical problem, removal prevents approval
      or requires restarting development, AND is highly novel/difficult.
    - NON-BLOCKING otherwise.
    """
    patent_number  = step1_result.get("patent_number", Path(filename).stem)
    jurisdiction   = step1_result.get("jurisdiction", "")
    claim_cat      = step1_result.get("claim_category", "")
    claim_reason   = step1_result.get("reason", "")
    evidence_block = step3_result.get("_evidence_block", "No evidence available from Step 3.")

    safe_context  = context.replace("{", "{{").replace("}", "}}")
    safe_evidence = _cap_evidence(evidence_block).replace("{", "{{").replace("}", "}}")

    prompt = STEP5_PROMPT.format(
        drug_name      = drug_name,
        patent_number  = patent_number,
        jurisdiction   = jurisdiction,
        claim_category = claim_cat,
        step1_reason   = claim_reason,
        step3_summary  = step3_result.get("evidence_summary", "N/A"),
        step4_reason   = step4_result.get("reason", "N/A"),
        evidence_block = safe_evidence,
        context        = safe_context,
    )

    result = await _call_gemini_json(prompt, filename, "STEP5")
    if result is None:
        return None

    # Normalise
    result["is_novel_and_difficult"] = bool(result.get("is_novel_and_difficult", False))
    result["final_tag"]              = result.get("final_tag", "NON-BLOCKING")
    result["confidence"]             = result.get("confidence", "low")
    result["reason"]                 = result.get("reason", "")

    # Confidence gate — only high confidence can yield BLOCKING
    if result["final_tag"] == "BLOCKING" and result["confidence"] != "high":
        print(
            f"[STEP 5] {filename} → Confidence gate: "
            f"final_tag=BLOCKING but confidence={result['confidence']} "
            f"→ overriding to NON-BLOCKING"
        )
        result["final_tag"] = "NON-BLOCKING"
        result["reason"] = (
            f"[Confidence gate: {result['confidence']} confidence insufficient for BLOCKING] "
            + (result.get("reason") or "")
        )

    print(
        f"[STEP 5] {filename}\n"
        f"  Novel & Difficult : {result['is_novel_and_difficult']}\n"
        f"  Novelty Signal    : {result.get('novelty_signal', '')}\n"
        f"  Confidence        : {result['confidence']}\n"
        f"  Final Tag         : {result['final_tag']}\n"
        f"  Reason            : {result.get('reason', '')[:120]}"
    )
    return result
