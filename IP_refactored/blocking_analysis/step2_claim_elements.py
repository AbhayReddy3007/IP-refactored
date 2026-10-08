"""
step2_claim_elements.py
───────────────────────
STEP 2 — Claim element matching.

Reads the drug's real-world formulation data from a local Excel file (one row
per trial/source, many rows per drug) and checks whether the patent covers any
of 5 elements: Active Ingredient & Form, Formulation Details, Route of
Administration, Device Description, Combination Tech/Process.

  ANY element present  → continue to Step 3
  NONE present         → NON-BLOCKING

The Excel is loaded once at startup (`load_formulation_excel`) and cached in
memory. Drug rows are matched on the Molecule column (case-insensitive).
"""

from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from .. import config
from .step1_claim_classification import _call_gemini_json


# The 5 Step 2 columns we care about from the Excel
_STEP2_COLUMNS = [
    "Active Ingredient & Form",
    "Formulation Details",
    "Route of Administration",
    "Device Description",
    "Combination Tech/Process",
]


# ─────────────────────────────────────────────
# Excel loader (loaded once, cached in memory)
# ─────────────────────────────────────────────

_FORMULATION_DF: Optional[pd.DataFrame] = None


def load_formulation_excel(path: Optional[str] = None) -> None:
    """
    Load the formulation Excel file into memory once at startup.
    Call this once before running any analysis (e.g. at process start-up).

    Args:
        path: Absolute path to the Excel file.
              Falls back to config.FORMULATION_EXCEL_PATH.
    """
    global _FORMULATION_DF

    excel_path = path or config.FORMULATION_EXCEL_PATH
    if not excel_path:
        print("[FORMULATION EXCEL] WARNING: No path provided and "
              "FORMULATION_EXCEL_PATH not set — Step 2 will run without Excel data.")
        return

    try:
        _FORMULATION_DF = pd.read_excel(excel_path, dtype=str)
        # Normalise column names: strip whitespace
        _FORMULATION_DF.columns = _FORMULATION_DF.columns.str.strip()
        print(f"[FORMULATION EXCEL] Loaded {len(_FORMULATION_DF)} rows from: {excel_path}")
        print(f"[FORMULATION EXCEL] Columns: {list(_FORMULATION_DF.columns)}")
    except Exception as e:
        print(f"[FORMULATION EXCEL] ERROR loading '{excel_path}': {e}")
        _FORMULATION_DF = None


def get_drug_rows(drug_name: str) -> List[Dict]:
    """
    Return all rows from the cached Excel where Molecule matches drug_name
    (case-insensitive, strips whitespace).

    Only returns the 5 Step 2 columns plus Trial ID and Phase for context.
    Empty / NaN cells are dropped from each row dict.

    Returns an empty list if the Excel was not loaded or no rows match.
    """
    if _FORMULATION_DF is None:
        return []

    if "Molecule" not in _FORMULATION_DF.columns:
        print("[FORMULATION EXCEL] WARNING: 'Molecule' column not found in Excel.")
        return []

    mask = _FORMULATION_DF["Molecule"].str.strip().str.lower() == drug_name.strip().lower()
    matched = _FORMULATION_DF[mask]

    if matched.empty:
        print(f"[FORMULATION EXCEL] No rows found for drug: '{drug_name}'")
        return []

    print(f"[FORMULATION EXCEL] Found {len(matched)} row(s) for '{drug_name}'")

    # Keep context columns + the 5 Step 2 columns
    keep_cols = ["Trial ID", "Phase"] + _STEP2_COLUMNS
    available = [c for c in keep_cols if c in matched.columns]
    subset = matched[available]

    rows = []
    for _, row in subset.iterrows():
        # Drop empty / NaN cells from each row
        row_dict = {
            k: v for k, v in row.items()
            if pd.notna(v) and str(v).strip().lower() not in ("", "nan", "none", "n/a")
        }
        if row_dict:
            rows.append(row_dict)

    return rows


# ─────────────────────────────────────────────
# STEP 2 — Claim element matching
# ─────────────────────────────────────────────
#
# Checks if the patent's claims cover any of these 5 elements:
#   1. Active ingredient and form
#   2. Formulation details
#   3. Route of administration
#   4. Device description
#   5. Combination tech/process
#
# Reference: the drug's known real-world profile from its FDA label.
# If the label is unavailable, Gemini checks the patent text alone.
#
# ANY element present → continue to Step 3.
# NONE present       → NON-BLOCKING.

STEP2_PROMPT = """You are a pharmaceutical patent expert performing Freedom-to-Operate analysis.
You are a STRICT REVIEWER. Your default position is NO MATCH unless the evidence is specific and direct.

PATENT FILE    : {filename}
PATENT NUMBER  : {patent_number}
JURISDICTION   : {jurisdiction}
CLAIM CATEGORY : {claim_category}  (classified in Step 1)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
THIS DRUG'S REAL-WORLD FORMULATION DATA
(Sourced from clinical trials and published sources — {row_count} record(s))
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{formulation_rows}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FULL PATENT DOCUMENT:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{context}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

─────────────────────────────────────────────
TASK — STEP 2: MATCH PATENT CLAIMS TO DRUG PROFILE
─────────────────────────────────────────────

For each of the 5 elements below, determine whether this patent specifically
and directly covers what this drug actually uses.
Read the ENTIRE document — title, abstract, background, detailed description,
examples, figures, sequences, and all numbered or unnumbered paragraphs.
Do NOT limit your reading to any section labelled "Claims". Understand what
the patent protects by reading ALL technical content in the document and
assess whether that protection covers what this drug actually uses.

─────────────────────────────────────────────
STRICT MATCHING RULES — apply to every element:
─────────────────────────────────────────────
- Read the ENTIRE document. Use the title, abstract, technical description,
  examples, and all other content to understand what the patent protects.
- Any section of the document — description, summary, examples, or numbered
  paragraphs — can inform your understanding of what is being protected.
- Background context helps you understand scope but does not itself define
  what is protected.
- The match must be SPECIFIC and DIRECT. The patent claim must explicitly
  describe the feature in a way that maps to this drug's actual profile.
- BROAD or GENERIC language does NOT match:
    "pharmaceutically acceptable salt"  → NOT a match (applies to all drugs)
    "aqueous solution"                  → NOT a match (too generic)
    "a therapeutically effective amount"→ NOT a match (applies to all drugs)
    "oral administration"               → NOT a match unless the claim specifies
                                          a feature of oral delivery unique to this drug
- The patent must claim something SPECIFIC to this drug's formulation —
  not something that would apply to any drug in the same class.
- If the patent claim is so broad it would read on thousands of drugs,
  it does NOT match for this drug specifically.
- Missing/empty fields → do not assume a match; treat as no match for that element.

─────────────────────────────────────────────
THE 5 ELEMENTS — STRICT CRITERIA:
─────────────────────────────────────────────

1. Active Ingredient & Form
   The patent must claim a SPECIFIC chemical form (a named salt, a specific
   polymorph, a specific ester or derivative) that matches exactly what this
   drug uses. Generic claims covering "any salt" or "any form" → NOT a match.

2. Formulation Details
   The patent must claim a SPECIFIC formulation feature (a named excipient,
   a specific concentration range, a specific release mechanism technology)
   that this drug demonstrably uses. Generic dosage form claims ("tablet",
   "solution", "injection") with no specifics → NOT a match.

3. Route of Administration
   Route alone is almost never a match. The patent must claim a SPECIFIC
   technical feature OF the route (a specific absorption mechanism, a specific
   device-route combination, a specific tissue target) that is unique to this
   drug's delivery. Simply claiming "subcutaneous" or "oral" → NOT a match.

4. Device Description
   The patent must claim a SPECIFIC device feature (a specific mechanism,
   a specific needle configuration, a specific reservoir design) that matches
   the device this drug actually uses. Generic claims for "a pen injector" or
   "a prefilled syringe" → NOT a match.

5. Combination Tech/Process
   The patent must claim a SPECIFIC named technology or process (e.g. SNAC
   co-formulation, a specific encapsulation process, a named absorption
   enhancer) that appears explicitly in this drug's records. General
   manufacturing process claims → NOT a match.

─────────────────────────────────────────────
PASS / FAIL DECISION:
─────────────────────────────────────────────
- any_element_present = true if AT LEAST ONE element matches under the
  strict criteria above.
- any_element_present = false ONLY if there is genuinely zero specific overlap
  between the patent's claims and this drug's actual profile across all 5 elements.
- When in doubt on any element → mark it false.

─────────────────────────────────────────────
OUTPUT — return ONLY valid JSON, no markdown:
─────────────────────────────────────────────
{{
  "elements_present": {{
    "active_ingredient_and_form": true or false,
    "formulation_details":        true or false,
    "route_of_administration":    true or false,
    "device_description":         true or false,
    "combination_tech_process":   true or false
  }},
  "any_element_present": true or false,
  "matched_elements":    ["list of element names that matched under strict criteria"],
  "reason": "1-2 sentences: specifically what matched or why nothing matched"
}}
"""


def _format_rows_for_prompt(rows: List[Dict]) -> str:
    """
    Format a list of Excel row dicts into a readable block for the Gemini prompt.
    Each row is numbered and only non-empty fields are shown.
    """
    if not rows:
        return "No formulation records available — assess from patent claims alone."

    lines = []
    for i, row in enumerate(rows, start=1):
        lines.append(f"Record {i}:")
        for key, val in row.items():
            lines.append(f"  {key}: {val}")
        lines.append("")  # blank line between records

    return "\n".join(lines).strip()


async def _run_step2(
    filename:     str,
    context:      str,
    step1_result: Dict,
    drug_rows:    List[Dict],
) -> Optional[Dict]:
    """
    Step 2: Check if the patent's claims cover any of the 5 formulation elements.

    Args:
        filename:     Patent PDF filename
        context:      RAG context string (same as Step 1)
        step1_result: Output dict from _run_step1
        drug_rows:    All rows from the formulation Excel for this drug.
                      Empty list if no Excel data available.

    Returns:
        Parsed JSON dict with elements_present, any_element_present, matched_elements, reason.
        Returns None on Gemini failure.
    """
    print(f"[STEP 2] Checking claim elements for {filename} "
          f"({len(drug_rows)} formulation record(s))...")

    formatted_rows = _format_rows_for_prompt(drug_rows)
    safe_context   = context.replace("{", "{{").replace("}", "}}")

    prompt = STEP2_PROMPT.format(
        filename       = filename,
        patent_number  = step1_result.get("patent_number", Path(filename).stem),
        jurisdiction   = step1_result.get("jurisdiction", ""),
        claim_category = step1_result.get("claim_category", ""),
        row_count      = len(drug_rows),
        formulation_rows = formatted_rows,
        context        = safe_context,
    )

    result = await _call_gemini_json(prompt, filename, "STEP2")
    if result is None:
        return None

    # Normalise
    elements = result.get("elements_present", {})
    matched  = result.get("matched_elements") or [k for k, v in elements.items() if v]
    result["matched_elements"] = matched

    # ── 1-element minimum gate ────────────────────────────────────────────────
    match_count = len(matched)
    result["any_element_present"] = match_count >= 1

    print(
        f"[STEP 2] {filename}\n"
        f"  Matched ({match_count}) : {matched}\n"
        f"  Pass gate  : {result['any_element_present']}\n"
        f"  Reason     : {(result.get('reason') or '')[:150]}"
    )
    return result
