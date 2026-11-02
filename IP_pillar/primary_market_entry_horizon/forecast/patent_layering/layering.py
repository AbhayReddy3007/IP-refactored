"""
layering.py
───────────
Step 2: patent layering (AlloyDB patent text + Gemini).
"""

import json
from typing import Dict, List, Optional
import pandas as pd
from ....indexer import get_or_create_collection
from ....gemini_utils import gemini_generate_async
from ....alloydb_utils import query_collection
from .constants import _GEMINI_MODEL
from .data_loading import load_loe_table
from .filing_analysis import step3_build_phase_timelines
from .insights import _generate_insights
from .phase_processing import _resolve_phase_at_filing


# ─────────────────────────────────────────────
# Step 2: Patent Layering Pattern (with ChromaDB + branching)
# ─────────────────────────────────────────────


def _get_patent_text(drug_name: str, patent_filename: str) -> str:
    """Fetches the full patent text (its AlloyDB chunks) for a given drug + filename."""
    try:
        chunks = query_collection(get_or_create_collection(drug_name), (patent_filename or "").strip())
        if not chunks:
            print(f"[STEP 2] WARNING: no patent text found for source file {patent_filename!r} - "
                  f"layering falls back to the claim analysis only")
        return "\n\n".join(chunks) if chunks else ""
    except Exception as e:
        print(f"[STEP 2] AlloyDB fetch failed for {patent_filename}: {e}")
        return ""


async def _analyse_patent_full(
    drug_name:      str,
    patent_num:     str,
    patent_filename: str,
    excel_context:  Dict,
    known_patents:  List[Dict],
) -> Dict:
    """
    Sends full patent text (from AlloyDB) + context to Gemini.
    Returns: layer, category, description, parent_patent_number.
    """
    patent_text = _get_patent_text(drug_name, patent_filename)

    if known_patents:
        known_block = "\n".join(
            f"  - {p['Patent Number']} (Layer {p['Layer']}, {p['Category']}): {p['Description']}"
            for p in known_patents
        )
    else:
        known_block = "  None yet — this may be the first patent (Layer 1 CoM)."

    excel_block = "\n".join(
        f"  {k}: {v}"
        for k, v in excel_context.items()
        if v and str(v).strip() not in ("N/A", "nan", "None", "")
    )

    patent_section = f"\nFULL PATENT TEXT:\n{patent_text[:35000]}" if patent_text else "\n(No full text available — use context only)"

    prompt = f"""You are a pharmaceutical patent analyst building a patent protection tree for {drug_name}.

PATENT TO ANALYSE:
  Patent Number: {patent_num}

ANALYSIS CONTEXT:
{excel_block}
{patent_section}

PATENTS ALREADY IDENTIFIED FOR THIS DRUG (for branching):
{known_block}

YOUR TASK:
Determine the following for patent {patent_num}:

1. LAYER — which protection layer does this patent belong to?
   Layer 1 = Composition of Matter ONLY (the core API molecule — ONLY ONE patent per drug per jurisdiction can be Layer 1. This is strictly the patent that claims the novel chemical compound itself. Salt forms, polymorphs, formulations, and all other patent types are NOT Layer 1.)
   Layer 2 = Salt/Polymorph OR Formulation (directly derived from the API — these protect how the molecule is physically formed or delivered)
   Layer 3 = Device, Method of Treatment, OR Dosage Regimen (built on top of Layer 2)
   Layer 4 = Combination therapy (combining with other metabolic agents)

   CRITICAL RULE: If the Step 1 Claim Category in the context is NOT "Composition of Matter", this patent CANNOT be Layer 1. Only a patent whose claim category is "Composition of Matter" may be assigned Layer 1.

2. DESCRIPTION — one short phrase (max 10 words) describing what this patent protects.

3. PARENT_PATENT — which already-identified patent does this one most directly build upon?
   - For Layer 1 (CoM): write "None" — it is the root patent.
   - For Layer 2 (Salt/Polymorph/Formulation): the parent MUST be the Layer 1 Composition of Matter patent.
   - For Layer 3 (Device/Method/Dosing): choose the most relevant Layer 2 patent from the known list.
   - For Layer 4 (Combination): choose the most relevant parent from the known list.
   - If no close parent exists, write the closest one or "None".

4. LAYER_REASON — explain in 1-2 sentences: (a) why this patent belongs to the assigned layer, and (b) why the chosen parent patent is the correct parent.

Respond ONLY with valid JSON, no markdown:
{{
  "layer":          <1|2|3|4>,
  "description":    "<short phrase>",
  "parent_patent":  "<patent number or None>",
  "layer_reason":   "<1-2 sentences>"
}}
"""

    try:
        response = await gemini_generate_async(prompt, _GEMINI_MODEL)
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()
        parsed = json.loads(raw)
        layer       = int(parsed.get("layer", 99))
        description  = str(parsed.get("description", "—")).strip().strip(".")
        parent       = str(parsed.get("parent_patent", "None")).strip()
        layer_reason = str(parsed.get("layer_reason", "—")).strip()

        # Enforce: only Composition of Matter can be Layer 1
        claim_cat = excel_context.get("Step 1 Claim Category", "").strip().lower()
        is_com = "composition of matter" in claim_cat or "com " in claim_cat or claim_cat == "com"

        if layer == 1 and not is_com:
            layer = 2
            layer_reason = f"[Corrected from Layer 1 → Layer 2: claim category is '{excel_context.get('Step 1 Claim Category', '')}', not Composition of Matter] " + layer_reason

        # Enforce: Formulation/Salt/Polymorph parent must be CoM patent
        is_formulation = any(
            kw in claim_cat
            for kw in ("formulation", "salt", "polymorph")
        )
        if is_formulation and layer == 2:
            com_parent = next(
                (p["Patent Number"] for p in known_patents if p.get("Layer") == 1),
                None,
            )
            if com_parent and parent != com_parent:
                parent = com_parent
                layer_reason = layer_reason.rstrip() + f" [Parent corrected to CoM patent {com_parent}]"

        return {
            "Layer":          layer,
            "Description":    description,
            "Parent Patent":  parent,
            "Layer Reason":   layer_reason,
        }
    except Exception as e:
        print(f"[STEP 2] LLM failed for {patent_num}: {e}")
        return {
            "Layer":         99,
            "Description":   "—",
            "Parent Patent": "None",
            "Layer Reason":  "—",
        }


async def step2_patent_layering(drug_filter: Optional[set] = None, loe_df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """
    Step 2: Patent Layering Pattern with ChromaDB + LLM branching.

    Reads from BigQuery loe_table. For each drug + jurisdiction:
      1. Reads each patent's full text from ChromaDB.
      2. Sends full text + context to Gemini.
      3. LLM assigns: layer, category, description, parent patent.
      4. Builds the branching tree sorted by layer then filing date.

    Args:
        drug_filter: see step1_ip_landscape.

    Returns a SINGLE concatenated DataFrame (forecast_s3) with all drugs,
    with columns:
        Drug Name | Jurisdiction | Layer | Layer Reason | Parent Patent |
        Category | Patent Number | Filing Date | Phase at Filing |
        Duration from Parent | Approval Date | Description | Insights
    """
    df = loe_df if loe_df is not None else load_loe_table(drug_filter)
    if df.empty:
        return pd.DataFrame()

    all_drugs = sorted(df["Drug Name"].dropna().unique().tolist())

    if drug_filter:
        wanted = {d.strip().lower() for d in drug_filter if d and str(d).strip()}
        before = len(all_drugs)
        all_drugs = [d for d in all_drugs if d.lower() in wanted]
        print(f"[STEP 2] Shard filter applied: {before} → {len(all_drugs)} drug(s)")

    print(f"[STEP 2] Drugs to process: {all_drugs}\n")

    if not all_drugs:
        print("[STEP 2] No drugs found.")
        return pd.DataFrame()

    # Load phase timeline via Step 3 (owns phase processing)
    phase_timelines = await step3_build_phase_timelines(all_drugs)
    if phase_timelines:
        print(f"[STEP 2] Phase timeline loaded from Step 3 — will resolve Phase at Filing per patent")
    else:
        print(f"[STEP 2] No phase timeline available — 'Phase at Filing' will be empty")

    _CONTEXT_COLS = [
        "Patent Number", "Jurisdiction", "Filing Date", "Source File",
        "Step 1 Claim Category", "Reason",
        "Step 3 Evidence Summary", "Step 5 Reason",
    ]
    context_cols = [c for c in _CONTEXT_COLS if c in df.columns]

    all_drug_rows: List[Dict] = []   # single list for all drugs

    for drug_name in all_drugs:
        drug_df   = df[df["Drug Name"] == drug_name].copy()
        canonical = drug_name.strip().lower().replace(" ", "_")

        # Extract approval date per jurisdiction
        approval_dates: Dict[str, str] = {}
        for jur in ["US", "EP"]:
            jur_rows = drug_df[
                drug_df["Jurisdiction"].astype(str).str.upper().str.strip() == jur
            ]
            if "Approval Date" in jur_rows.columns:
                dates = jur_rows["Approval Date"].dropna().astype(str).str.strip()
                dates = dates[~dates.isin(["N/A", "nan", "None", "Unknown", "NaT", ""])]
                if not dates.empty:
                    raw = dates.iloc[0]
                    if " 00:00:00" in raw:
                        raw = raw.replace(" 00:00:00", "")
                    approval_dates[jur] = raw
            if jur in approval_dates:
                print(f"[STEP 2] {drug_name} | {jur} Approval Date: {approval_dates[jur]}")

        print(f"\n[STEP 2] ══ {drug_name} — {len(drug_df)} patent row(s) ══")

        drug_rows: List[Dict] = []

        for jurisdiction in ["US", "EP"]:
            jur_df = drug_df[
                drug_df["Jurisdiction"].astype(str).str.upper().str.strip() == jurisdiction
            ]
            print(f"\n[STEP 2]   [{jurisdiction}] — {len(jur_df)} patent(s)")

            if jur_df.empty:
                continue

            jur_df = jur_df.copy()
            jur_df["_date_sort"] = pd.to_datetime(
                jur_df["Filing Date"].astype(str), errors="coerce"
            )
            jur_df = jur_df.sort_values("_date_sort", na_position="last")

            known_patents: List[Dict] = []

            for _, row in jur_df.iterrows():
                patent_num = str(row.get("Patent Number", "")).strip()
                if not patent_num or patent_num in ("N/A", "nan", "None", ""):
                    continue

                filing_date = str(row.get("Filing Date", "")).strip()
                if " 00:00:00" in filing_date:
                    filing_date = filing_date.replace(" 00:00:00", "")
                if filing_date in ("nan", "None", "Unknown", "NaT", ""):
                    filing_date = "Unknown"

                source_file = str(row.get("Source File", "")).strip()

                row_context = {c: str(row.get(c, "")).strip() for c in context_cols}

                print(f"[STEP 2]     Analysing {patent_num} ({filing_date})...")
                result = await _analyse_patent_full(
                    drug_name        = drug_name,
                    patent_num       = patent_num,
                    patent_filename  = source_file,
                    excel_context    = row_context,
                    known_patents    = known_patents,
                )

                claim_category = str(row.get("Step 1 Claim Category", "")).strip()
                if not claim_category or claim_category in ("N/A", "nan", "None", ""):
                    claim_category = "Unknown"

                row_out = {
                    "Drug Name":        drug_name,
                    "Jurisdiction":     jurisdiction,
                    "Layer":            result["Layer"],
                    "Layer Reason":     result["Layer Reason"],
                    "Parent Patent":    result["Parent Patent"],
                    "Category":         claim_category,
                    "Patent Number":    patent_num,
                    "Filing Date":      filing_date,
                    "Phase at Filing":  _resolve_phase_at_filing(phase_timelines, drug_name, filing_date),
                    "Approval Date":    approval_dates.get(jurisdiction, "—"),
                    "Description":      result["Description"],
                }
                drug_rows.append(row_out)
                known_patents.append({
                    "Patent Number": patent_num,
                    "Layer":         result["Layer"],
                    "Category":      claim_category,
                    "Description":   result["Description"],
                })
                print(
                    f"[STEP 2]     → Layer {result['Layer']} | {claim_category} "
                    f"| Parent: {result['Parent Patent']} | {result['Description']}"
                    f" | Phase: {row_out['Phase at Filing']}"
                )

        if not drug_rows:
            print(f"[STEP 2] No rows for '{drug_name}'")
            continue

        # Build temporary per-drug DF for duration calculation and insights
        drug_result_df = pd.DataFrame(drug_rows)

        # Sort: jurisdiction, then layer, then filing date
        drug_result_df["_date_sort"] = pd.to_datetime(
            drug_result_df["Filing Date"], errors="coerce"
        )
        drug_result_df = drug_result_df.sort_values(
            by=["Jurisdiction", "Layer", "_date_sort"], na_position="last"
        ).reset_index(drop=True)

        # Duration: time between parent filing date and this patent
        date_lookup: Dict[str, pd.Timestamp] = {}
        for _, r in drug_result_df.iterrows():
            parsed = pd.to_datetime(r["Filing Date"], errors="coerce")
            if pd.notna(parsed):
                date_lookup[r["Patent Number"]] = parsed

        durations = []
        for _, r in drug_result_df.iterrows():
            parent = str(r["Parent Patent"]).strip()
            if parent in ("None", "—", "", "nan") or parent not in date_lookup:
                durations.append("—")
                continue
            current_dt = date_lookup.get(r["Patent Number"])
            parent_dt  = date_lookup.get(parent)
            if current_dt is None or parent_dt is None:
                durations.append("—")
                continue
            delta_days   = (current_dt - parent_dt).days
            delta_months = round(delta_days / 30.44)
            delta_years  = delta_days / 365.25
            if abs(delta_years) >= 1:
                durations.append(f"{delta_years:+.1f} yrs")
            else:
                durations.append(f"{delta_months:+d} months")

        drug_result_df.insert(
            drug_result_df.columns.get_loc("Phase at Filing") + 1,
            "Duration from Parent",
            durations,
        )
        drug_result_df = drug_result_df.drop(columns=["_date_sort"])

        # Insights: LLM summary for this drug
        print(f"[STEP 2] Generating insights for '{drug_name}'...")
        insights = await _generate_insights(drug_name, drug_result_df)
        drug_result_df["Insights"] = ""
        if not drug_result_df.empty:
            drug_result_df.at[drug_result_df.index[0], "Insights"] = insights

        all_drug_rows.append(drug_result_df)
        print(f"\n[STEP 2] {drug_name}: {len(drug_result_df)} patent(s) layered.")

    if not all_drug_rows:
        print("[STEP 2] No data produced.")
        return pd.DataFrame()

    # Concatenate all drugs into a single table
    forecast_s3 = pd.concat(all_drug_rows, ignore_index=True)
    print(f"\n[STEP 2] Complete — forecast_s3 has {len(forecast_s3)} rows across {forecast_s3['Drug Name'].nunique()} drug(s).")
    return forecast_s3
