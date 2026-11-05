"""
insights.py
───────────
Insight generation (drug and innovator level).
"""

import re
from typing import List
import pandas as pd
from ....gemini_utils import gemini_generate_async
from .constants import _GEMINI_MODEL


# ─────────────────────────────────────────────
# Insights generation
# ─────────────────────────────────────────────

async def _generate_insights(drug_name: str, df: pd.DataFrame) -> str:
    """Asks Gemini to generate insights about the patent layering strategy for a drug."""
    summary_lines = []
    for _, row in df.iterrows():
        phase_info = f" | Phase at Filing: {row['Phase at Filing']}" if row.get('Phase at Filing', '—') != '—' else ""
        summary_lines.append(
            f"  [{row['Jurisdiction']}] Layer {row['Layer']} | {row['Category']} | "
            f"{row['Patent Number']} | Filed: {row['Filing Date']}{phase_info} | "
            f"Duration from parent: {row['Duration from Parent']} | "
            f"Parent: {row['Parent Patent']} | {row['Description']}"
        )
    summary = "\n".join(summary_lines)

    prompt = f"""You are a pharmaceutical patent strategy analyst.

Drug: {drug_name}

Below is the full patent layering tree for {drug_name}, showing how IP protection was built up over time:

{summary}

Based on this layering pattern, provide strategic insights covering:
1. How quickly the innovator built protection around the core API (Layer 1)
2. Which layers have the most patents and why
3. Notable gaps in the protection strategy (e.g. missing layers in one jurisdiction)
4. The time gaps between layers — what does the filing timeline reveal about their strategy?
5. Any asymmetry between US and EP patent estates
6. PHASE-ALIGNED FILING STRATEGY: Analyse the "Phase at Filing" data for each patent.

Write 5-8 concise bullet points. Be specific — reference actual patent numbers, dates, phases, and durations where relevant.
Respond with plain text bullet points only, no headers, no markdown.
"""
    try:
        response = await gemini_generate_async(prompt, _GEMINI_MODEL)
        return response.text.strip()
    except Exception as e:
        print(f"[STEP 2] Insights LLM failed for {drug_name}: {e}")
        return "—"


def _compute_time_statistics(
    forecast_s3: pd.DataFrame,
    drug_names: List[str],
    innovator: str,
) -> str:
    """
    Pre-computes detailed chronological statistics from patent filing dates.
    Works on the single concatenated forecast_s3 table.
    """
    sections = []

    for drug_name in drug_names:
        df = forecast_s3[forecast_s3["Drug Name"] == drug_name].copy()
        if df.empty:
            sections.append(f"\n=== {drug_name}: No data ===")
            continue

        df["_parsed_date"] = pd.to_datetime(df["Filing Date"], errors="coerce")
        has_dates = df.dropna(subset=["_parsed_date"])

        if has_dates.empty:
            sections.append(f"\n=== {drug_name}: No parseable filing dates ===")
            continue

        sections.append(f"\n{'='*60}")
        sections.append(f"  TIME STATISTICS FOR: {drug_name}")
        sections.append(f"{'='*60}")

        for jur in ["US", "EP"]:
            jur_df = has_dates[has_dates["Jurisdiction"] == jur].sort_values("_parsed_date")
            if jur_df.empty:
                sections.append(f"\n  [{jur}] No patents with parseable dates.")
                continue

            sections.append(f"\n  [{jur}] — {len(jur_df)} patent(s)")

            sections.append(f"  Chronological order:")
            for i, (_, r) in enumerate(jur_df.iterrows(), 1):
                phase_info = f" | Phase: {r['Phase at Filing']}" if r.get('Phase at Filing', '—') != '—' else ""
                sections.append(
                    f"    {i}. {r['Patent Number']} | Filed: {r['_parsed_date'].strftime('%Y-%m-%d')}"
                    f"{phase_info} | Layer {r['Layer']} | {r['Category']} | {r['Description']}"
                )

            first_dt = jur_df["_parsed_date"].min()
            last_dt  = jur_df["_parsed_date"].max()
            span_days  = (last_dt - first_dt).days
            span_years = round(span_days / 365.25, 2)
            sections.append(f"  First filing: {first_dt.strftime('%Y-%m-%d')} ({jur_df.iloc[0]['Patent Number']})")
            sections.append(f"  Last filing:  {last_dt.strftime('%Y-%m-%d')} ({jur_df.iloc[-1]['Patent Number']})")
            sections.append(f"  Total span:   {span_days} days ({span_years} years)")

            dates_sorted   = jur_df["_parsed_date"].tolist()
            patents_sorted = jur_df["Patent Number"].tolist()
            if len(dates_sorted) >= 2:
                sections.append(f"  Consecutive filing gaps:")
                gaps = []
                for i in range(1, len(dates_sorted)):
                    gap_days  = (dates_sorted[i] - dates_sorted[i-1]).days
                    gap_years = round(gap_days / 365.25, 2)
                    gap_months = round(gap_days / 30.44, 1)
                    gaps.append(gap_days)
                    sections.append(
                        f"    {patents_sorted[i-1]} → {patents_sorted[i]}: "
                        f"{gap_days} days ({gap_years} yrs / {gap_months} months)"
                    )
                avg_gap = round(sum(gaps) / len(gaps), 1)
                min_gap = min(gaps)
                max_gap = max(gaps)
                sections.append(f"  Average gap: {avg_gap} days ({round(avg_gap/365.25, 2)} yrs)")
                sections.append(f"  Shortest gap: {min_gap} days ({round(min_gap/365.25, 2)} yrs)")
                sections.append(f"  Longest gap:  {max_gap} days ({round(max_gap/365.25, 2)} yrs)")

                dormant = [(i, g) for i, g in enumerate(gaps) if g >= 730]
                if dormant:
                    sections.append(f"  DORMANT PERIODS (2+ years):")
                    for idx, g in dormant:
                        sections.append(
                            f"    {patents_sorted[idx]} ({dates_sorted[idx].strftime('%Y-%m-%d')}) → "
                            f"{patents_sorted[idx+1]} ({dates_sorted[idx+1].strftime('%Y-%m-%d')}): "
                            f"{g} days ({round(g/365.25, 2)} yrs) — NO FILINGS"
                        )
                else:
                    sections.append(f"  No dormant periods (all gaps < 2 years).")

            com_df = jur_df[jur_df["Layer"] == 1]
            if not com_df.empty:
                com_date = com_df["_parsed_date"].min()
                sections.append(f"  Layer timing (from CoM filed {com_date.strftime('%Y-%m-%d')}):")
                for layer in sorted(jur_df["Layer"].unique()):
                    layer_df = jur_df[jur_df["Layer"] == layer].sort_values("_parsed_date")
                    first_in_layer = layer_df.iloc[0]
                    delta_days  = (first_in_layer["_parsed_date"] - com_date).days
                    delta_years = round(delta_days / 365.25, 2)
                    sections.append(
                        f"    Layer {layer}: first filed {first_in_layer['_parsed_date'].strftime('%Y-%m-%d')} "
                        f"({first_in_layer['Patent Number']}, {first_in_layer['Category']}) — "
                        f"{delta_days} days / {delta_years} yrs after CoM"
                    )

            year_counts = jur_df["_parsed_date"].dt.year.value_counts().sort_index()
            sections.append(f"  Filings per year:")
            for yr, cnt in year_counts.items():
                yr_patents = jur_df[jur_df["_parsed_date"].dt.year == yr]
                details = ", ".join(
                    f"{r['Patent Number']} ({r['Category']}"
                    + (f", {r['Phase at Filing']}" if r.get('Phase at Filing', '—') != '—' else "")
                    + ")"
                    for _, r in yr_patents.iterrows()
                )
                sections.append(f"    {yr}: {cnt} filing(s) — {details}")

            peak_year = year_counts.idxmax()
            sections.append(f"  Peak filing year: {peak_year} ({year_counts[peak_year]} filings)")

            if "Phase at Filing" in jur_df.columns:
                phase_counts = jur_df["Phase at Filing"].value_counts()
                valid_phases = phase_counts[phase_counts.index != "—"]
                if not valid_phases.empty:
                    sections.append(f"  Patents by clinical phase at time of filing:")
                    for phase, cnt in valid_phases.items():
                        phase_patents = jur_df[jur_df["Phase at Filing"] == phase]
                        patent_list = ", ".join(
                            f"{r['Patent Number']} ({r['Category']})"
                            for _, r in phase_patents.iterrows()
                        )
                        sections.append(f"    {phase}: {cnt} patent(s) — {patent_list}")

        us_dates = has_dates[has_dates["Jurisdiction"] == "US"]
        ep_dates = has_dates[has_dates["Jurisdiction"] == "EP"]
        if not us_dates.empty and not ep_dates.empty:
            us_first = us_dates["_parsed_date"].min()
            ep_first = ep_dates["_parsed_date"].min()
            lag_days = (ep_first - us_first).days
            sections.append(f"\n  US vs EP first filing lag: {lag_days} days ({round(lag_days/365.25, 2)} yrs)")
            sections.append(f"    US first: {us_first.strftime('%Y-%m-%d')} | EP first: {ep_first.strftime('%Y-%m-%d')}")

            us_span = (us_dates["_parsed_date"].max() - us_dates["_parsed_date"].min()).days
            ep_span = (ep_dates["_parsed_date"].max() - ep_dates["_parsed_date"].min()).days
            us_rate = round(len(us_dates) / max(us_span / 365.25, 0.1), 2) if us_span > 0 else len(us_dates)
            ep_rate = round(len(ep_dates) / max(ep_span / 365.25, 0.1), 2) if ep_span > 0 else len(ep_dates)
            sections.append(f"  Filing velocity: US = {us_rate} patents/year | EP = {ep_rate} patents/year")

    if len(drug_names) >= 2:
        sections.append(f"\n{'='*60}")
        sections.append(f"  CROSS-DRUG VELOCITY COMPARISON")
        sections.append(f"{'='*60}")
        for drug_name in drug_names:
            df = forecast_s3[forecast_s3["Drug Name"] == drug_name].copy()
            if df.empty:
                continue
            df["_parsed_date"] = pd.to_datetime(df["Filing Date"], errors="coerce")
            has = df.dropna(subset=["_parsed_date"])
            if has.empty:
                continue
            total    = len(has)
            first_dt = has["_parsed_date"].min()
            last_dt  = has["_parsed_date"].max()
            span     = (last_dt - first_dt).days
            rate     = round(total / max(span / 365.25, 0.1), 2) if span > 0 else total
            com_to_max_layer = "N/A"
            com = has[has["Layer"] == 1]
            if not com.empty:
                com_dt    = com["_parsed_date"].min()
                max_layer = has["Layer"].max()
                last_layer_dt = has[has["Layer"] == max_layer]["_parsed_date"].max()
                cl_days   = (last_layer_dt - com_dt).days
                com_to_max_layer = f"{cl_days} days ({round(cl_days/365.25, 2)} yrs) from Layer 1 to Layer {max_layer}"
            sections.append(
                f"  {drug_name}: {total} patents over {round(span/365.25, 2)} yrs | "
                f"{rate} patents/year | {com_to_max_layer}"
            )

    return "\n".join(sections)


async def _generate_innovator_insights(
    forecast_s3: pd.DataFrame,
    drug_names: List[str],
    innovator: str,
) -> pd.DataFrame:
    """
    Compares the patent layering patterns of multiple drugs from the same innovator.
    Works on the single concatenated forecast_s3 table.
    """
    summaries = []
    for drug_name in drug_names:
        df = forecast_s3[forecast_s3["Drug Name"] == drug_name]
        if df.empty:
            summaries.append(f"\n{drug_name}: No data available.")
            continue
        lines = [f"\n{drug_name}:"]
        for _, row in df.iterrows():
            phase_info = f" | Phase: {row['Phase at Filing']}" if row.get('Phase at Filing', '—') != '—' else ""
            lines.append(
                f"  [{row['Jurisdiction']}] Layer {row['Layer']} | {row['Category']} | "
                f"{row['Patent Number']} | Filed: {row['Filing Date']}{phase_info} | "
                f"Duration: {row['Duration from Parent']} | Parent: {row['Parent Patent']} | "
                f"{row['Description']}"
            )
        summaries.append("\n".join(lines))

    combined_summary = "\n".join(summaries)
    time_stats_block = _compute_time_statistics(forecast_s3, drug_names, innovator)

    prompt = f"""You are a pharmaceutical patent strategy analyst.

Innovator: {innovator}
Drugs being compared: {", ".join(drug_names)}

Below are the patent layering trees for each drug from {innovator}:

{combined_summary}

These drugs are from the same innovator. Analyse the cross-drug patent strategy and provide insights on:

1. SIMILARITIES: What is structurally similar between the patent estates of {" and ".join(drug_names)}?
2. COMMON PATTERNS: What protection layers does {innovator} consistently file across both drugs?
3. TIMING STRATEGY: How long after the CoM patent does {innovator} typically file Layer 2, Layer 3, and Layer 4 patents?
4. JURISDICTIONAL STRATEGY: Does {innovator} file US and EP patents simultaneously or stagger them?
5. EVOLUTION: How did their strategy evolve from {drug_names[0]} to {drug_names[1] if len(drug_names) > 1 else drug_names[0]}?
6. GAPS & OPPORTUNITIES: What layers are missing or weak?
7. PREDICTED NEXT MOVES: Based on this pattern, what types of patents would {innovator} likely file next?
8. TIME-BASED INSIGHTS: Below are pre-computed chronological statistics. Use ALL of these numbers exactly as given.

{time_stats_block}

Using the statistics above, write a deeply detailed chronological analysis covering:
8a. FILING TIMELINE OVERVIEW
8b. FILING CADENCE & CLUSTERING
8c. CONSECUTIVE FILING GAPS — PATTERN ANALYSIS
8d. PEAK FILING YEARS & LIFECYCLE CORRELATION
8e. FILING VELOCITY & STRATEGIC AGGRESSION
8f. LAYER BUILD-OUT TIMING — CROSS-DRUG PATTERN
8g. DORMANT PERIODS & STRATEGIC INFLECTION POINTS
8h. JURISDICTIONAL TIMING PATTERNS & FILING SEQUENCE
8i. RECURRING PATTERNS & INNOVATOR SIGNATURE
8j. PHASE-ALIGNED FILING STRATEGY

Be specific — reference actual patent numbers, exact filing dates, clinical phases, and precise durations.
Write sections 1-7 as clear paragraphs.
Write section 8 as multiple detailed paragraphs, one per sub-section (8a through 8j). Each sub-section header must appear on its own line.
"""

    try:
        print(f"[STEP 2] Generating innovator insights for {innovator} ({', '.join(drug_names)})...")
        response = await gemini_generate_async(prompt, _GEMINI_MODEL)
        raw = response.text.strip()

        rows = [{"Section": "Innovator", "Content": innovator}]
        rows.append({"Section": "Drugs Compared", "Content": ", ".join(drug_names)})
        rows.append({"Section": "", "Content": ""})
        rows.append({"Section": "── GENERAL ANALYSIS ──", "Content": ""})

        current_section = ""
        current_num     = ""
        current_lines   = []
        separator_inserted = False

        for line in raw.splitlines():
            line = line.strip().strip("*").strip()
            if not line:
                if current_section and current_lines:
                    rows.append({"Section": current_section, "Content": " ".join(current_lines)})
                    current_section = ""
                    current_num     = ""
                    current_lines   = []
                continue

            header_match = re.match(r"^(\d+[a-z]?)\.\s+([A-Za-z &'\-]+?)[\s:.]?\s*$", line)
            if not header_match:
                header_match = re.match(r"^(\d+[a-z]?)\.\s+([A-Za-z &'\-]+?):\s+(.+)$", line)
                if header_match:
                    if current_section and current_lines:
                        rows.append({"Section": current_section, "Content": " ".join(current_lines)})

                    new_num = header_match.group(1)
                    if not separator_inserted and new_num.startswith("8"):
                        rows.append({"Section": "", "Content": ""})
                        rows.append({"Section": "── TIME-BASED ANALYSIS ──", "Content": ""})
                        separator_inserted = True

                    current_num     = new_num
                    current_section = header_match.group(2).strip().upper()
                    current_lines   = [header_match.group(3).strip()]
                    continue

            if header_match:
                if current_section and current_lines:
                    rows.append({"Section": current_section, "Content": " ".join(current_lines)})

                new_num = header_match.group(1)
                if not separator_inserted and new_num.startswith("8"):
                    rows.append({"Section": "", "Content": ""})
                    rows.append({"Section": "── TIME-BASED ANALYSIS ──", "Content": ""})
                    separator_inserted = True

                current_num     = new_num
                current_section = header_match.group(2).strip().upper()
                current_lines   = []
            else:
                current_lines.append(line)

        if current_section and current_lines:
            rows.append({"Section": current_section, "Content": " ".join(current_lines)})

        result = pd.DataFrame(rows, columns=["Section", "Content"])

        content_rows = result[result["Section"].str.strip() != ""]
        if len(content_rows) < 6:
            print(f"[STEP 2] Parser only found {len(content_rows)} sections — using raw fallback")
            rows = [
                {"Section": "Innovator",      "Content": innovator},
                {"Section": "Drugs Compared", "Content": ", ".join(drug_names)},
                {"Section": "",               "Content": ""},
                {"Section": "Full Response",  "Content": raw},
            ]
            result = pd.DataFrame(rows, columns=["Section", "Content"])

        return result

    except Exception as e:
        print(f"[STEP 2] Innovator insights failed: {e}")
        return pd.DataFrame([{"Section": "Error", "Content": str(e)}])
