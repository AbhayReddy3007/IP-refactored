"""
step1_claim_classification.py
─────────────────────────────
STEP 1 — Classify the patent's primary claim into one of 7 categories.

  Composition of Matter → orchestrator applies primary-CoM routing (BLOCKING)
  Anything else         → continues to Step 2

Also hosts `_call_gemini_json`, the shared Gemini JSON-mode helper that
Steps 2-5 import from here.
"""

import asyncio
import json
import random
import re
from typing import Dict, Optional

from google.genai import types

from .. import config
from .. import gcp_utils

gemini_client = gcp_utils.get_gemini_client()

# All 7 claim categories used across all steps
CLAIM_CATEGORIES = [
    "Composition of Matter",
    "Salt/Polymorph",
    "Formulation",
    "Manufacturing Process",
    "Method of Treatment",
    "Device",
    "Dosage Regimen",
]


# ─────────────────────────────────────────────
# Gemini call helper (shared by all steps)
# ─────────────────────────────────────────────

async def _call_gemini_json(prompt: str, filename: str, step: str) -> Optional[Dict]:
    """
    Calls Gemini 2.5 Flash with JSON response mode.
    Retries up to config.MAX_ANALYSIS_RETRIES times (see config.py) on truncation or empty response.
    On truncation retry, appends a concise-output instruction to reduce token usage.
    Used by every step in the pipeline.
    """
    _CONCISE_SUFFIX = (
        "\n\nIMPORTANT: Your previous response was truncated. "
        "Return ONLY the JSON object. Keep every string field under 60 words. "
        "Do NOT include any explanation outside the JSON."
    )

    for attempt in range(1, config.MAX_ANALYSIS_RETRIES + 1):
        try:
            # On retry after truncation, ask for a more concise response
            current_prompt = prompt if attempt == 1 else prompt + _CONCISE_SUFFIX

            response = await gemini_client.aio.models.generate_content(
                model=config.GEMINI_TEXT_MODEL,
                contents=current_prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                    max_output_tokens=8192,
                ),
            )

            raw_text = response.text if response.text else ""

            try:
                finish_str = str(response.candidates[0].finish_reason)
                print(f"[{step}] finish_reason for {filename} (attempt {attempt}): {finish_str}")
                if finish_str in ("MAX_TOKENS", "2"):
                    print(f"[WARNING] Response truncated for {filename} (attempt {attempt}) — retrying with concise prompt")
                    if attempt < config.MAX_ANALYSIS_RETRIES:
                        await asyncio.sleep(2 + random.uniform(0, 1))
                        continue
                elif finish_str in ("SAFETY", "3"):
                    print(f"[ERROR] Safety filter blocked response for {filename}.")
                    return None
            except (IndexError, AttributeError):
                pass

            if not raw_text.strip():
                print(f"[ERROR] Empty response for {filename} (attempt {attempt})")
                if attempt < config.MAX_ANALYSIS_RETRIES:
                    await asyncio.sleep(2)
                    continue
                return None

            clean = raw_text.strip()
            if "```" in clean:
                clean = re.sub(r"```(?:json)?", "", clean).replace("```", "").strip()

            # Catch unterminated JSON before trying to parse — sign of silent truncation
            if clean and not clean.rstrip().endswith("}"):
                print(f"[WARNING] Response appears truncated (no closing brace) for {filename} (attempt {attempt})")
                if attempt < config.MAX_ANALYSIS_RETRIES:
                    await asyncio.sleep(2 + random.uniform(0, 1))
                    continue

            print(f"[{step}] Raw response for {filename}: {clean[:300]!r}")
            return json.loads(clean)

        except json.JSONDecodeError as e:
            print(f"[ERROR] JSON parse failed for {filename} (attempt {attempt}): {e}")
            if attempt < config.MAX_ANALYSIS_RETRIES:
                await asyncio.sleep(2)
                continue
            return None

        except Exception as e:
            print(f"[ERROR] Gemini call failed for {filename} (attempt {attempt}): {e}")
            if attempt < config.MAX_ANALYSIS_RETRIES:
                await asyncio.sleep(2)
                continue
            return None

    return None


# ─────────────────────────────────────────────
# STEP 1 — Claim classification
# ─────────────────────────────────────────────
#
# Classifies the patent's primary claim into one of 7 categories.
# If Composition of Matter → BLOCKING immediately.
# Otherwise → pass to Step 2 (pending).

STEP1_PROMPT = """You are a pharmaceutical patent expert.

SOURCE FILE: {filename}
PATENT NUMBER: {patent_number_hint}
JURISDICTION: {jurisdiction_hint}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FULL PATENT DOCUMENT:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{context}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

─────────────────────────────────────────────
TASK — STEP 1: CLASSIFY THE PRIMARY CLAIM
─────────────────────────────────────────────

Read the ENTIRE patent document thoroughly — every section including the title,
abstract, technical field, background, summary, detailed description, examples,
figures, tables, sequences, and any numbered or unnumbered paragraphs.
Do NOT search only for a "Claims" section. The document may not have one, or it
may be formatted differently. Instead, understand what the patent is PROTECTING
by reading all of the technical content — what compound, formulation, method,
device, or process is described as the invention throughout the document.
Identify the SINGLE best category that describes what this patent primarily protects.

CATEGORY DEFINITIONS:

1. Composition of Matter  ← STRICT DEFINITION — read carefully
   - The claim covers THE BASE MOLECULE ITSELF — its exact chemical structure,
     molecular formula, or the specific compound as a pure chemical entity
   - There can only be ONE true Composition of Matter patent per drug per
     jurisdiction — it is the foundational patent on the molecule itself
   - A claim is CoM ONLY if a competitor cannot make the molecule at all
     without infringing — because the patent owns the molecule itself
   - Examples: "A compound of formula [structure]...",
     "The peptide having the amino acid sequence...",
     "A GLP-1 receptor agonist having the structure..."
   - NOT CoM: salts of the molecule → use Salt/Polymorph
   - NOT CoM: polymorphs or crystalline forms → use Salt/Polymorph
   - NOT CoM: formulations containing the molecule → use Formulation
   - NOT CoM: methods of using the molecule → use Method of Treatment
   - NOT CoM: esters, prodrugs, or derivatives unless they ARE the drug itself
   - RULE: If the claim adds ANY qualifier beyond the bare molecule
     (a specific salt, a specific crystal form, a composition, a method),
     it is NOT Composition of Matter.

2. Salt/Polymorph
   - The claim covers a specific salt form, ester, prodrug, polymorph,
     crystalline form, amorphous form, or hydrate of the molecule
   - The molecule itself is NOT claimed — only a specific physical/chemical
     variant of it
   - Examples: "The sodium salt of...", "Crystalline Form A of...",
     "A polymorph of X characterised by X-ray diffraction peaks at...",
     "The acetate ester of...", "A co-crystal comprising..."

3. Formulation
   - The claim covers a pharmaceutical composition, formulation, or combination
   - Examples: "A pharmaceutical composition comprising X and excipient Y...",
     "A fixed-dose combination of X and Z...",
     "A sustained-release formulation comprising..."

4. Manufacturing Process
   - The claim covers a process or method of making or synthesizing the compound
   - Examples: "A process for preparing compound X comprising the steps of...",
     "A method of synthesizing..."

5. Method of Treatment
   - The claim covers a method of using the drug to treat a disease or condition
   - Examples: "A method of treating type 2 diabetes comprising administering...",
     "Use of compound X for treatment of obesity..."

6. Device
   - The claim covers a delivery device, drug-device combination, or administration system
   - Examples: "An injection pen for administering...", "An inhaler device comprising...",
     "A transdermal patch system..."

7. Dosage Regimen
   - The claim covers a specific dosing schedule, dose amount, or frequency of administration
   - Examples: "A method comprising administering X at a dose of Y mg once weekly...",
     "A dosing regimen comprising an initial dose of..."

─────────────────────────────────────────────
CLASSIFICATION RULES:
─────────────────────────────────────────────
- Determine what the patent PRIMARILY protects by reading the full document.
- Use the title, abstract, summary, detailed description, and examples together
  to understand the core invention — do not rely on any single section.
- Choose the category that best describes the BROADEST protection the patent
  appears to offer based on all technical content in the document.
- If the document primarily describes a bare molecule by its chemical structure,
  molecular formula, or amino acid sequence — classify as "Composition of Matter".
- CoM TEST: "Does the document primarily describe and protect a compound defined
  by its chemical structure, molecular formula, or amino acid sequence, without
  restricting it to a specific salt form, polymorph, or formulation?"
  If YES → Composition of Matter.
- Salt/Polymorph ONLY if the document focuses on a specific physical/chemical
  variant (named salt, crystal form, hydrate) rather than the bare molecule.
- Formulation ONLY if the document centres on a pharmaceutical composition or
  mixture, not the molecule itself.
- Do NOT downgrade a genuine compound patent to Salt/Polymorph or Formulation
  just because the document also describes such variants.

─────────────────────────────────────────────
ALSO EXTRACT:
─────────────────────────────────────────────
- patent_number: read from the document cover page or header
- jurisdiction:  two-letter office code (US, EP, WO, GB, JP, CN, AU, CA)
- pte:           Patent Term Extension in months (look for "Patent Term Extension",
                 "PTE", "35 U.S.C. 156", "SPC"). Convert years to months if needed. null if absent.
- pediatric_exclusivity: true ONLY if the document explicitly states pediatric
                 exclusivity is granted (look for "BPCA", "6-month exclusivity",
                 "pediatric extension"). false otherwise.

─────────────────────────────────────────────
OUTPUT — return ONLY valid JSON, no markdown:
─────────────────────────────────────────────
{{
  "patent_number":            "read from document, or '{patent_number_hint}' as fallback",
  "jurisdiction":             "two-letter code, or '{jurisdiction_hint}' as fallback",
  "claim_category":           "exactly one of the 7 categories below",
  "is_composition_of_matter": true ONLY if claim_category is "Composition of Matter" AND the claim covers the bare molecule itself with no salt/polymorph/formulation qualifiers — false otherwise,
  "reason":                   "1-2 sentences: what the primary claim covers and why you chose this category",
  "pte":                      number of months as integer or null,
  "pediatric_exclusivity":    true or false
}}

claim_category must be EXACTLY one of:
  "Composition of Matter"
  "Salt/Polymorph"
  "Formulation"
  "Manufacturing Process"
  "Method of Treatment"
  "Device"
  "Dosage Regimen"
"""


async def _run_step1(
    filename:           str,
    context:            str,
    patent_number_hint: str,
    jurisdiction_hint:  str,
) -> Optional[Dict]:
    """
    Step 1: Classify the patent's primary claim into one of 7 categories.
    Returns the parsed JSON dict, or None on failure.
    """
    print(f"[STEP 1] Classifying claim for {filename}...")

    safe_context = context.replace("{", "{{").replace("}", "}}")
    prompt = STEP1_PROMPT.format(
        filename           = filename,
        patent_number_hint = patent_number_hint,
        jurisdiction_hint  = jurisdiction_hint,
        context            = safe_context,
    )

    result = await _call_gemini_json(prompt, filename, "STEP1")
    if result is None:
        return None

    # Normalise claim_category to exact spelling
    raw_category = (result.get("claim_category") or "").strip()
    matched = next(
        (c for c in CLAIM_CATEGORIES if c.lower() == raw_category.lower()),
        None,
    )
    if not matched:
        print(f"[STEP 1] Unrecognised category '{raw_category}' for {filename} — defaulting to None")
    result["claim_category"] = matched

    # Enforce consistency: category and flag must agree
    if result["claim_category"] == "Composition of Matter":
        result["is_composition_of_matter"] = True
    else:
        result["is_composition_of_matter"] = False

    print(
        f"[STEP 1] {filename} → "
        f"Category: {result['claim_category']} | "
        f"CoM: {result['is_composition_of_matter']} | "
        f"Reason: {(result.get('reason') or '')[:80]}"
    )
    return result
