"""
filing_analysis.py
──────────────────
Step 3: filing timing vs. development milestones.
"""

import json
from typing import Dict, List
import pandas as pd
from ....gemini_utils import gemini_generate_async
from .constants import _GEMINI_MODEL
from .data_loading import load_clinical_efficacy
from .drug_aliases import _DRUG_ALIASES, _build_drug_aliases, _canonicalise_drug
from .phase_processing import _build_phase_timelines, _process_phase_input
from .submission_dates import _fetch_all_submission_dates


# ─────────────────────────────────────────────
# Step 3: Filing Timing vs. Development Milestones
# ─────────────────────────────────────────────
# Purpose: Analyse when innovators file each patent type relative to
# clinical development phases (Phase 1 → Phase 2 → Phase 3 → Marketed).
#
# This step OWNS the phase timeline construction:
#   1. Loads clinical trial data from BigQuery (clinical_efficacy)
#   2. Normalises phases (3a→3, III→3, etc.)
#   3. Drops Phase 4 (handled via Marketed). Keeps real Phase 1 / Phase 2
#      trial_start_date from clinical_efficacy when present, and only
#      derives missing ones from Phase 3 (Phase 2 = P3 − 3 yrs, Phase 1 = P3 − 4 yrs)
#   4. Fetches live approval dates for marketed drugs (approval_date_fetcher)
#   5. Fetches regulatory submission dates via Gemini Search
#   6. Filters to GCS drugs
#   7. Builds per-drug timelines and resolves "Phase at Filing" per patent
#   8. LLM analysis: determines typical filing stage per patent type

async def step3_build_phase_timelines(drug_names: List[str]) -> Dict[str, List]:
    """
    Step 3 core: Builds phase timelines from clinical_efficacy BigQuery table.

    Pipeline:
      1. Load clinical_efficacy from BigQuery
      2. Normalise phases. Keep real Phase 1/2 trial_start_date when
         present; only calculate from Phase 3 (P2=P3−3y, P1=P3−4y) when missing.
      3. Fetch approval dates for marketed drugs
      4. Fetch regulatory submission dates via Gemini Search
      5. Filter to GCS drugs
      6. Build per-drug (date, phase) timelines

    Returns: {drug_name_lower: [(date, phase_label), ...]}
    """
    clinical_df = load_clinical_efficacy()
    if clinical_df.empty:
        print("[STEP 3] No clinical data loaded from clinical_efficacy.")
        return {}

    # Build drug aliases from clinical data
    _DRUG_ALIASES.clear()  # mutated in place: drug_aliases._canonicalise_drug reads the same dict
    _DRUG_ALIASES.update(_build_drug_aliases(clinical_df))
    if _DRUG_ALIASES:
        print(f"[STEP 3 ALIASES] Built {len(_DRUG_ALIASES)} drug alias(es): {_DRUG_ALIASES}")

    # Only the requested drugs (and their aliases) are processed - approvals are fetched per marketed drug
    wanted = {d.strip().lower() for d in drug_names}
    clinical_df = clinical_df[clinical_df["Molecule"].map(
        lambda m: str(m).strip().lower() in wanted or _canonicalise_drug(str(m)).lower() in wanted)]
    if clinical_df.empty:
        print(f"[STEP 3] No clinical data for {sorted(wanted)}.")
        return {}

    # Process phase input: normalise, calculate Phase 1/2, fetch approvals
    phase_df = await _process_phase_input(clinical_df)
    if phase_df.empty:
        return {}

    # Build base timelines
    timelines = _build_phase_timelines(phase_df)

    # Fetch regulatory submission dates via Gemini Search for known drugs
    marketed_drugs = [d for d in drug_names if d.lower() in timelines]
    if marketed_drugs:
        print(f"[STEP 3 SUBMISSION] Fetching regulatory submission dates for: {marketed_drugs}")
        submission_dates = await _fetch_all_submission_dates(marketed_drugs)

        for drug, date_str in submission_dates.items():
            parsed = pd.to_datetime(date_str, errors="coerce")
            if pd.isna(parsed):
                continue
            key = drug.lower()
            if key not in timelines:
                timelines[key] = []
            existing_phases = {p for _, p in timelines[key]}
            if "After Submission" not in existing_phases:
                timelines[key].append((parsed, "After Submission"))
                print(f"[STEP 3 SUBMISSION] Injected 'After Submission' for '{drug}': {date_str}")

        # Re-sort timelines
        for key in timelines:
            timelines[key].sort(key=lambda x: x[0])

    return timelines


async def step3_filing_analysis(forecast_s3: pd.DataFrame) -> pd.DataFrame:
    """
    Step 3 analysis: For each drug, uses the LLM to determine the typical
    clinical phase at which each patent type is strategically filed.

    Requires "Phase at Filing" column to be populated in the forecast_s3 table
    (done by step3_build_phase_timelines + _resolve_phase_at_filing in Step 2).

    Returns a single DataFrame with columns:
        Drug Name | Patent Type | Typical Filing Stage | Reason
    """
    if forecast_s3.empty:
        return pd.DataFrame()

    all_pattern_rows: List[Dict] = []

    for drug_name in forecast_s3["Drug Name"].unique():
        df = forecast_s3[forecast_s3["Drug Name"] == drug_name].copy()
        print(f"\n[STEP 3] ══ {drug_name} ══")

        if df.empty:
            continue

        valid = df[
            (df["Phase at Filing"].astype(str).str.strip() != "—") &
            (df["Phase at Filing"].astype(str).str.strip() != "")
        ].copy()

        if valid.empty:
            print(f"[STEP 3] No Phase at Filing data for '{drug_name}'")
            continue

        patent_lines = []
        for _, row in valid.iterrows():
            patent_lines.append(
                f"  {row['Patent Number']} | {row['Category']} | "
                f"Filed: {row['Filing Date']} | Phase: {row['Phase at Filing']} | "
                f"Layer {row['Layer']} | [{row['Jurisdiction']}] | {row['Description']}"
            )
        patent_listing = "\n".join(patent_lines)

        grouped = valid.groupby(["Category", "Phase at Filing"]).agg(
            Count=("Patent Number", "count"),
            Patents=("Patent Number", lambda x: ", ".join(x.astype(str))),
        ).reset_index()

        stats_lines = []
        for category in sorted(grouped["Category"].unique()):
            cat_rows = grouped[grouped["Category"] == category]
            phase_details = " | ".join(
                f"{r['Phase at Filing']}: {int(r['Count'])} ({r['Patents']})"
                for _, r in cat_rows.iterrows()
            )
            stats_lines.append(f"  {category}: {phase_details}")
        stats_block = "\n".join(stats_lines)

        categories = sorted(valid["Category"].unique().tolist())

        prompt = f"""You are a pharmaceutical patent strategy analyst.

Drug: {drug_name}

Below is every patent for {drug_name} with its category, filing date, and the clinical phase the drug was in when the patent was filed:

{patent_listing}

Here are the filing counts per patent type per clinical phase:

{stats_block}

YOUR TASK:
For each patent type below, determine the TYPICAL FILING STAGE — the clinical development phase when this type of patent is strategically expected to be filed by an innovator.

Do NOT just pick the phase with the most patents. Consider:
- The strategic purpose of this patent type
- The timing relative to clinical milestones
- Whether filings in a particular phase represent the innovator's deliberate strategy vs. opportunistic additions
- Industry norms for when this type of patent is typically filed

Patent types to analyse: {", ".join(categories)}

Respond ONLY with valid JSON, no markdown:
{{
  "patterns": [
    {{
      "patent_type": "<category name>",
      "typical_filing_stage": "<Phase 1 | Phase 2 | Phase 3 | Pre-Phase 1 | Marketed>",
      "reason": "<1 sentence explaining why>"
    }}
  ]
}}
"""
        try:
            print(f"[STEP 3] Asking LLM for filing patterns...")
            response = await gemini_generate_async(prompt, _GEMINI_MODEL)
            raw = response.text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()
            parsed = json.loads(raw)

            for item in parsed.get("patterns", []):
                patent_type   = str(item.get("patent_type", "Unknown")).strip()
                typical_stage = str(item.get("typical_filing_stage", "—")).strip()
                reason        = str(item.get("reason", "")).strip()
                all_pattern_rows.append({
                    "Drug Name":            drug_name,
                    "Patent Type":          patent_type,
                    "Typical Filing Stage": typical_stage,
                    "Reason":               reason,
                })
                print(f"[STEP 3]   {patent_type:<25} → {typical_stage:<15} | {reason}")

        except Exception as e:
            print(f"[STEP 3] LLM failed for {drug_name}: {e}")
            for category in categories:
                cat_rows = grouped[grouped["Category"] == category]
                top = cat_rows.sort_values("Count", ascending=False).iloc[0]
                all_pattern_rows.append({
                    "Drug Name":            drug_name,
                    "Patent Type":          category,
                    "Typical Filing Stage": top["Phase at Filing"],
                    "Reason":               f"Most frequent ({int(top['Count'])} patents)",
                })

    result = pd.DataFrame(all_pattern_rows)
    print(f"\n[STEP 3] Complete — {len(result)} pattern rows across {result['Drug Name'].nunique() if not result.empty else 0} drug(s).")
    return result
