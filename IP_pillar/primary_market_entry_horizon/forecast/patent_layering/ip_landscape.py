"""
ip_landscape.py
───────────────
Step 1: IP landscape - classifies each patent into the six categories.
"""

from typing import Dict, List, Optional
import pandas as pd
from ....gemini_utils import gemini_generate_async
from .constants import _FORECAST_COLS, _GEMINI_MODEL
from .data_loading import load_loe_table


# ─────────────────────────────────────────────
# LLM classification — per patent row (Step 1)
# ─────────────────────────────────────────────

_COMBO_KEYWORDS = (
    "combination", "co-administer", "co-therapy", "combined with",
    "together with", "adjunct", "metabolic agent", "dual therapy",
)


_LOE_CATEGORY_TO_FORECAST = {
    "composition of matter":  "API Patent",
    "salt/polymorph":         "API Patent",
    "formulation":            "Formulation",
    "manufacturing process":  "Formulation",
    "method of treatment":    "Method",
    "device":                 "Device",
    "dosage regimen":         "Dosing",
}


def _classify_patent_from_loe(patent_info: Dict) -> Dict[str, str]:
    """
    Deterministic Step 1 classification using the existing
    `Step 1 Claim Category` field from loe_table (produced by the
    upstream pipeline). No LLM call required.

    Returns a dict like {"API Patent": "✓ (Composition of Matter)", ...}
    where keys are a subset of _FORECAST_COLS.

    Mapping (loe -> forecast bucket):
      Composition of Matter  -> API Patent
      Salt/Polymorph         -> API Patent
      Formulation            -> Formulation
      Manufacturing Process  -> Formulation
      Method of Treatment    -> Method
      Device                 -> Device
      Dosage Regimen         -> Dosing

    Combination is not a loe category — it is inferred from the
    Step 1 Claim Category / Reason text only when an explicit co-therapy
    keyword is present (matches the prior rule in _classify_patent).
    """
    result: Dict[str, str] = {}

    raw_cat = str(patent_info.get("Step 1 Claim Category", "")).strip()
    if raw_cat and raw_cat.lower() not in ("n/a", "nan", "none", ""):
        bucket = _LOE_CATEGORY_TO_FORECAST.get(raw_cat.lower())
        if bucket:
            result[bucket] = f"✓ ({raw_cat})"

    # Combination: only mark when an explicit co-therapy signal appears
    # in Step 1 Claim Category or Reason. Mirrors the prior LLM rule.
    combo_text = " ".join(
        str(patent_info.get(k, "")) for k in ("Step 1 Claim Category", "Reason")
    ).lower()
    if any(kw in combo_text for kw in _COMBO_KEYWORDS):
        result["Combination"] = "✓ (co-administration with another agent)"

    patent_num = patent_info.get("Patent Number", "")
    print(f"[STEP 1]   {patent_num} → {list(result.keys()) or 'no match'} "
          f"(loe category: {raw_cat or '—'})")
    return result


async def _classify_patent(drug_name: str, patent_info: Dict) -> Dict[str, str]:
    """
    DEPRECATED: previously sent each patent to Gemini for 6-category
    classification. Now delegates to the deterministic mapping based on
    the `Step 1 Claim Category` already present in loe_table.

    Kept as an async wrapper so existing call sites continue to work.
    The `drug_name` argument is unused but retained for signature
    compatibility.
    """
    del drug_name  # unused
    return _classify_patent_from_loe(patent_info)


async def _lookup_innovator(drug_name: str) -> str:
    """Uses Gemini web search to find the innovator/originator company for a drug."""
    prompt = f"""Who is the original innovator / originator pharmaceutical company that developed and holds the primary patents for the drug "{drug_name}"?

Search the web and return ONLY the company name. No explanation, no extra text. Just the company name.
Examples: "Novo Nordisk", "Eli Lilly", "AstraZeneca"
"""
    try:
        response = await gemini_generate_async(
            prompt, _GEMINI_MODEL, google_search=True, temperature=0.1,
        )
        name =(response.text or "").strip().strip(".")
        print(f"[STEP 1] Innovator for '{drug_name}': {name}")
        return name or "Unknown"
    except Exception as e:
        print(f"[STEP 1] Innovator lookup failed for '{drug_name}': {e}")
        return "Unknown"


async def step1_ip_landscape(drug_filter: Optional[set] = None, loe_df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """
    Step 1: IP Landscape of All Drugs.

    Reads from BigQuery loe_table. For each drug:
      1. Filters patents belonging to that drug.
      2. Sends each patent's analysis info to Gemini for 6-category classification.
      3. Aggregates into one row per drug.

    Args:
        drug_filter: Optional set of drug names. When provided, only these
            drugs are processed (case-insensitive). When None, all drugs in
            loe_table are processed. Used by the orchestrator to shard
            step3 across Cloud Run tasks.
    """
    df = loe_df if loe_df is not None else load_loe_table(drug_filter)
    if df.empty:
        return pd.DataFrame()

    all_drugs = sorted(df["Drug Name"].dropna().unique().tolist())

    if drug_filter:
        wanted = {d.strip().lower() for d in drug_filter if d and str(d).strip()}
        before = len(all_drugs)
        all_drugs = [d for d in all_drugs if d.lower() in wanted]
        print(f"[STEP 1] Shard filter applied: {before} → {len(all_drugs)} drug(s)")

    print(f"[STEP 1] Drugs to process: {all_drugs}\n")

    if not all_drugs:
        print("[STEP 1] No drugs found.")
        return pd.DataFrame()

    _CONTEXT_COLS = [
        "Patent Number", "Jurisdiction", "Tag",
        "Step 1 Claim Category", "Reason",
        "Step 2 Matched Elements", "Step 3 Technical Barrier",
        "Step 3 Evidence Summary", "Step 5 Novelty Signal", "Step 5 Reason",
    ]
    context_cols = [c for c in _CONTEXT_COLS if c in df.columns]

    results = []

    for drug_name in all_drugs:
        drug_df = df[df["Drug Name"] == drug_name]
        print(f"[STEP 1] ── {drug_name} — {len(drug_df)} patent row(s) ──")

        innovator = await _lookup_innovator(drug_name)

        category_entries: Dict[str, List[str]] = {col: [] for col in _FORECAST_COLS}

        for _, row in drug_df.iterrows():
            patent_num = str(row.get("Patent Number", "")).strip()
            if not patent_num or patent_num in ("N/A", "nan", "None", ""):
                continue

            patent_info = {
                col: str(row.get(col, "")).strip()
                for col in context_cols
            }

            classification = await _classify_patent(drug_name, patent_info)

            for col, label in classification.items():
                if label not in category_entries[col]:
                    category_entries[col].append(label)

        row_out = {"Drug Name": drug_name, "Innovator": innovator}
        for col in _FORECAST_COLS:
            entries      = category_entries[col]
            row_out[col] = "\n".join(entries) if entries else "—"

        results.append(row_out)
        print(
            f"[STEP 1] {drug_name} summary: "
            + " | ".join(
                f"{col}={len(category_entries[col])}"
                for col in _FORECAST_COLS
            ) + "\n"
        )

    result_df = pd.DataFrame(results, columns=["Drug Name", "Innovator"] + _FORECAST_COLS)
    print(f"[STEP 1] Complete — {len(result_df)} drug(s).")
    return result_df
