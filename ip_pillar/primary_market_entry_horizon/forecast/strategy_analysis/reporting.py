"""
reporting.py
────────────
Console printing and Excel export.
"""

from pathlib import Path
from typing import Dict, Optional
from .... import gcp_utils


# ─────────────────────────────────────────────────────────────────────────────
# Pretty-print
# ─────────────────────────────────────────────────────────────────────────────

def print_assessment(assessment: Dict) -> None:
    if "error" in assessment:
        print(f"\n[ERROR] {assessment['error']}")
        return

    meta = assessment.get("_metadata", {})
    print(f"\n{'═'*70}")
    print(f"  BUSINESS STRATEGY ASSESSMENT — {meta.get('company','?')}")
    print(f"  Therapy Area : {meta.get('therapy_area','?')}")
    print(f"  Date         : {meta.get('date','?')}")
    print(f"{'═'*70}\n")

    _SECTIONS = [
        ("REVENUE DEPENDENCY",          "revenue_dependency"),
        ("PIPELINE DEPTH",              "pipeline_depth"),
        ("STRATEGIC PRIORITY",          "strategic_priority"),
        ("INDICATION EXPANSION",        "indication_expansion"),
        ("LIFECYCLE MANAGEMENT",        "lifecycle_management"),
        ("COMPETITIVE PRESSURE",        "competitive_pressure"),
        ("PATENT EXPANSION LIKELIHOOD", "patent_expansion_likelihood"),
    ]
    for title, key in _SECTIONS:
        data = assessment.get(key, {})
        if not data:
            continue
        print(f"  {title}: {data.get('rating','—')}")
        for k, v in data.items():
            if k == "rating":
                continue
            if isinstance(v, list):
                print(f"    {k}: {', '.join(str(x) for x in v)}")
            elif v:
                print(f"    {k}: {v}")
        print()

    overall = assessment.get("overall_assessment", "")
    if overall:
        print(f"  {'─'*66}")
        print(f"  OVERALL: {overall}")
        print(f"  {'─'*66}")


# ─────────────────────────────────────────────────────────────────────────────
# Optional Excel export
# ─────────────────────────────────────────────────────────────────────────────

def export_to_excel(assessment: Dict, output_path: str) -> Optional[str]:
    try:
        import pandas as pd
    except ImportError:
        print("[EXPORT] pandas not installed — skipping Excel export")
        return None

    if "error" in assessment:
        return None

    meta = assessment.get("_metadata", {})
    rows = [
        {"Section": "Company",      "Content": meta.get("company", "")},
        {"Section": "Therapy Area", "Content": meta.get("therapy_area", "")},
        {"Section": "Drugs",        "Content": ", ".join(meta.get("drugs", []))},
        {"Section": "Date",         "Content": meta.get("date", "")},
        {"Section": "Sources",      "Content": "; ".join(meta.get("sources_used", []))},
        {"Section": "", "Content": ""},
    ]

    _SECTIONS = [
        ("revenue_dependency",         "REVENUE DEPENDENCY"),
        ("pipeline_depth",             "PIPELINE DEPTH"),
        ("strategic_priority",         "STRATEGIC PRIORITY"),
        ("indication_expansion",       "INDICATION EXPANSION"),
        ("lifecycle_management",       "LIFECYCLE MANAGEMENT"),
        ("competitive_pressure",       "COMPETITIVE PRESSURE"),
        ("patent_expansion_likelihood","PATENT EXPANSION LIKELIHOOD"),
    ]
    for key, title in _SECTIONS:
        data = assessment.get(key, {})
        if not data:
            continue
        rows.append({"Section": title, "Content": f"Rating: {data.get('rating','—')}"})
        for k, v in data.items():
            if k == "rating":
                continue
            rows.append({"Section": f"  {k}", "Content": (
                ", ".join(str(x) for x in v) if isinstance(v, list) else str(v)
            )})
        rows.append({"Section": "", "Content": ""})

    overall = assessment.get("overall_assessment", "")
    if overall:
        rows.append({"Section": "OVERALL ASSESSMENT", "Content": overall})

    try:
        import io as _io

        gcs_cache = gcp_utils

        df = pd.DataFrame(rows)
        buf = _io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Business Strategy")
            ws = writer.sheets["Business Strategy"]
            ws.freeze_panes = "A2"
            for col in ws.columns:
                width = max((len(str(c.value or "")) for c in col), default=10)
                ws.column_dimensions[col[0].column_letter].width = min(width + 4, 120)
        buf.seek(0)

        filename = Path(output_path).name
        uri = gcs_cache.write_bytes(
            "company_analysis",
            filename,
            buf.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        print(f"[EXPORT] Saved to GCS: {uri}")
        return uri
    except Exception as e:
        print(f"[EXPORT] Failed: {e}")
        return None
