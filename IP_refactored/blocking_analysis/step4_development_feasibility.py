"""
step4_development_feasibility.py
────────────────────────────────
STEP 4 — Can development proceed without this feature?

  Marketed drugs  → would removing the feature cause FDA/EMA approval failure?
  Clinical drugs  → would removing it require new Phase I / PK-PD bridging?

Reuses the evidence block gathered in Step 3 (no new fetching).

  is_blocking_indicator = True  → continue to Step 5
  is_blocking_indicator = False → NON-BLOCKING
"""

from pathlib import Path
from typing import Dict, Optional

from .step1_claim_classification import _call_gemini_json
from .step3_scientific_barrier import _cap_evidence


# ─────────────────────────────────────────────
# STEP 4 — Can development proceed without this feature?
# ─────────────────────────────────────────────

STEP4_PROMPT_MARKETED = """You are a pharmaceutical regulatory expert performing Freedom-to-Operate analysis.
You are a STRICT REVIEWER. Your default position is NON-BLOCKING unless evidence is explicit and direct.

DRUG NAME      : {drug_name}
PATENT NUMBER  : {patent_number}
JURISDICTION   : {jurisdiction}
CLAIM CATEGORY : {claim_category}
CLAIM DETAILS  : {step1_reason}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SCIENTIFIC EVIDENCE (gathered in Step 3)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{evidence_block}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PATENT DOCUMENT CHUNKS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{context}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

─────────────────────────────────────────────
TASK — STEP 4: CAN DEVELOPMENT PROCEED WITHOUT THIS FEATURE?
─────────────────────────────────────────────

This drug is COMMERCIALLY MARKETED. A generic/biosimilar developer needs to know
whether they can omit or substitute the patented feature and still obtain regulatory approval.

CENTRAL QUESTION:
Would removing or substituting the patented feature cause failure to meet
FDA/EMA regulatory requirements for approval of a generic or biosimilar?

BLOCKING INDICATOR — answer YES only if ALL of the following are true:
1. The FDA or EMA review documents explicitly state this specific feature was
   required for approval (not merely used or preferred)
2. Removing the feature would cause the product to fail a specific regulatory
   standard (stability, bioavailability specification, safety requirement)
3. No approved alternative approach exists that achieves the same regulatory outcome

NOT a blocking indicator if:
- The feature was used in the approved product but not explicitly mandated
- An alternative formulation/device/process could achieve the same regulatory outcome
- The requirement is based on labelling preference rather than regulatory standard
- The evidence only shows the feature improves quality without being required

─────────────────────────────────────────────
STRICT DECISION RULES:
─────────────────────────────────────────────
1. Only explicit regulatory language stating the feature is REQUIRED → YES
2. Implied necessity, strong preference, or common practice → NO
3. If uncertain → NO

OUTPUT — return ONLY valid JSON, no markdown:
{{
  "is_blocking_indicator": true or false,
  "regulatory_failure_if_removed": true or false,
  "confidence": "high" or "medium" or "low",
  "reason": "1-2 sentences: specifically what regulatory requirement would fail and why, or why removal is feasible"
}}
"""


STEP4_PROMPT_CLINICAL = """You are a pharmaceutical development expert performing Freedom-to-Operate analysis.
You are a STRICT REVIEWER. Your default position is NON-BLOCKING unless evidence is explicit and direct.

DRUG NAME      : {drug_name}
PATENT NUMBER  : {patent_number}
JURISDICTION   : {jurisdiction}
CLAIM CATEGORY : {claim_category}
CLAIM DETAILS  : {step1_reason}
CLINICAL PHASE : {drug_phase}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SCIENTIFIC EVIDENCE (gathered in Step 3)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{evidence_block}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PATENT DOCUMENT CHUNKS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{context}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

─────────────────────────────────────────────
TASK — STEP 4: CAN DEVELOPMENT PROCEED WITHOUT THIS FEATURE?
─────────────────────────────────────────────

This drug is in CLINICAL DEVELOPMENT ({drug_phase}). A competing developer needs to know
whether they can omit or substitute the patented feature and proceed without
major redevelopment.

CENTRAL QUESTION:
Would removing or substituting the patented feature require the competitor to
conduct new Phase I studies or PK/PD bridging studies before continuing development?

EVIDENCE TO EXAMINE:
1. Consistency of formulation across clinical phases — has this feature been
   present from Phase 1 through current phase without change? (suggests it is
   integral to the development programme, not just optimised later)
2. Any published statements that this configuration is optimised, required, or
   cannot be substituted without affecting pharmacokinetics or safety
3. Whether substituting the feature would produce a meaningfully different
   PK/PD profile requiring new bridging data

BLOCKING INDICATOR — answer YES only if:
- Published evidence shows the feature has been consistent across ALL phases
  (indicating it is integral, not a late optimisation)
  AND
- Removing it would produce a different PK/PD profile that would require new
  Phase I or bridging studies before Phase 2/3 could proceed

NOT a blocking indicator if:
- The feature was introduced after Phase 1 (optimisation, not necessity)
- An alternative could be substituted with only formulation development work
  (no new clinical studies required)
- The evidence only shows the feature is preferred or convenient
- Publications describe it as one of several viable approaches

─────────────────────────────────────────────
STRICT DECISION RULES:
─────────────────────────────────────────────
1. Consistent across all phases + new bridging studies required → YES
2. Late-stage optimisation, or bridging not required → NO
3. If uncertain → NO

OUTPUT — return ONLY valid JSON, no markdown:
{{
  "is_blocking_indicator": true or false,
  "bridging_studies_required": true or false,
  "formulation_consistent_across_phases": true or false,
  "confidence": "high" or "medium" or "low",
  "reason": "1-2 sentences: specifically what evidence supports or refutes the need for new clinical studies"
}}
"""


async def _run_step4(
    filename:     str,
    context:      str,
    step1_result: Dict,
    step2_result: Dict,
    step3_result: Dict,
    drug_name:    str,
    drug_phase:   Dict[str, Optional[str]],
) -> Optional[Dict]:
    """
    Step 4: Can development proceed without this feature?

    Commercial drugs — checks whether removing the feature would cause
    regulatory failure (FDA/EMA requirement not met).

    Clinical drugs — checks whether removing the feature would require
    new Phase I or PK/PD bridging studies.

    Reuses the evidence block already gathered in Step 3 — no new fetching.
    """
    patent_number  = step1_result.get("patent_number", Path(filename).stem)
    jurisdiction   = step1_result.get("jurisdiction", "")
    claim_cat      = step1_result.get("claim_category", "")
    claim_reason   = step1_result.get("reason", "")
    evidence_block = step3_result.get("_evidence_block", "No evidence available from Step 3.")

    # Determine phase for this patent's jurisdiction
    jur_upper = jurisdiction.upper()
    if jur_upper == "US":
        phase = drug_phase.get("US")
    elif jur_upper in ("EP", "EU"):
        phase = drug_phase.get("EP")
    else:
        phase = drug_phase.get("US") or drug_phase.get("EP")

    is_marketed = (phase or "").lower() == "marketed"
    print(f"[STEP 4] {filename} — Phase: {phase} | Marketed: {is_marketed}")

    safe_context  = context.replace("{", "{{").replace("}", "}}")
    safe_evidence = _cap_evidence(evidence_block).replace("{", "{{").replace("}", "}}")

    if is_marketed:
        prompt = STEP4_PROMPT_MARKETED.format(
            drug_name      = drug_name,
            patent_number  = patent_number,
            jurisdiction   = jurisdiction,
            claim_category = claim_cat,
            step1_reason   = claim_reason,
            evidence_block = safe_evidence,
            context        = safe_context,
        )
    else:
        prompt = STEP4_PROMPT_CLINICAL.format(
            drug_name      = drug_name,
            patent_number  = patent_number,
            jurisdiction   = jurisdiction,
            claim_category = claim_cat,
            step1_reason   = claim_reason,
            drug_phase     = phase or "Unknown",
            evidence_block = safe_evidence,
            context        = safe_context,
        )

    result = await _call_gemini_json(prompt, filename, "STEP4")
    if result is None:
        return None

    # Normalise
    result["is_blocking_indicator"] = bool(result.get("is_blocking_indicator", False))
    result["confidence"]            = result.get("confidence", "low")
    result["reason"]                = result.get("reason", "")

    # Confidence gate — only high confidence passes as blocking indicator
    if result["is_blocking_indicator"] and result["confidence"] != "high":
        print(
            f"[STEP 4] {filename} → Confidence gate: "
            f"is_blocking_indicator=True but confidence={result['confidence']} "
            f"→ overriding to False"
        )
        result["is_blocking_indicator"] = False
        result["reason"] = (
            f"[Confidence gate: {result['confidence']} confidence insufficient] "
            + (result.get("reason") or "")
        )

    verdict = "BLOCKING INDICATOR" if result["is_blocking_indicator"] else "NON-BLOCKING"
    print(
        f"[STEP 4] {filename}\n"
        f"  Blocking Indicator : {result['is_blocking_indicator']} ({result['confidence']} confidence)\n"
        f"  Reason             : {result.get('reason', '')[:120]}\n"
        f"  Verdict            : {verdict}"
    )
    return result
