"""Geographic arbitrage scoring (Dimension IV)."""
import logging
from datetime import date

import pandas as pd

from .constants import KEEP_CLAIM_CATEGORIES

logger = logging.getLogger(__name__)


def determine_protection_gap(categories_present: set[str]) -> str:
    """Key protection gap — data-driven (no Gemini)."""
    present = categories_present & KEEP_CLAIM_CATEGORIES
    absent = KEEP_CLAIM_CATEGORIES - present

    if not present:
        return "No blocking patents; all categories absent"
    if not absent:
        return "Full protection; all categories covered"

    present_str = ", ".join(sorted(present))
    absent_str = ", ".join(sorted(absent))
    return f"{present_str} present; {absent_str} absent"


def compute_arbitrage_score(loe_year: int, gap_vs_longest: int, current_year: int) -> int:
    """Per-country score, 1–5."""
    if loe_year == current_year:
        return 5
    if gap_vs_longest >= 5:
        return 4
    if gap_vs_longest >= 3:
        return 3
    if gap_vs_longest == 2:
        return 2
    return 1


def get_dimension_iv_rating(avg_score: float) -> str:
    if avg_score <= 1.0:
        return "FAIL — No viable geographic arbitrage"
    elif avg_score <= 2.0:
        return "Limited Geographic Arbitrage Opportunity"
    elif avg_score <= 3.0:
        return "Moderate Geographic Arbitrage Opportunity"
    elif avg_score <= 4.0:
        return "Strong Geographic Arbitrage Opportunity"
    else:
        return "Very Strong Geographic Arbitrage Opportunity"


_SIGNALS = {
    5: "Immediate opportunity",
    4: "Strong arbitrage",
    3: "Meaningful arbitrage",
    2: "Limited arbitrage",
    1: "No arbitrage",
}


def compute_arbitrage(shortlisted: pd.DataFrame, full_filtered: pd.DataFrame) -> pd.DataFrame:
    current_year = date.today().year

    cats_lookup: dict[tuple, set] = {}
    for _, row in full_filtered.iterrows():
        key = (row["Drug Name"], row["Jurisdiction"])
        cat = str(row.get("Step 1 Claim Category", "")).strip().title()
        if cat in KEEP_CLAIM_CATEGORIES:
            cats_lookup.setdefault(key, set()).add(cat)

    drugs = shortlisted["Drug Name"].unique()
    arb_rows = []

    for drug in sorted(drugs):
        drug_df = shortlisted[shortlisted["Drug Name"] == drug].copy()

        drug_df["_adj_dt"] = pd.to_datetime(drug_df["Adjusted Expiry (with PTE)"], errors="coerce")
        loe_by_jur = drug_df.groupby("Jurisdiction")["_adj_dt"].max().dropna()

        if loe_by_jur.empty:
            continue

        loe_years = loe_by_jur.dt.year
        us_loe = loe_years.get("US")
        max_loe = int(loe_years.max())
        max_loe_jur = loe_years.idxmax()
        sorted_jurs = loe_years.sort_values(ascending=True)
        prev_loe = None
        country_scores = {}

        for launch_order, (jur, loe_year) in enumerate(sorted_jurs.items(), start=1):
            if jur == "US":
                gap_vs_us = "N/A"
            elif us_loe is not None:
                gap_vs_us = int(us_loe) - int(loe_year)
            else:
                gap_vs_us = "US data missing"

            if jur == max_loe_jur:
                gap_vs_longest = "N/A (longest)"
            else:
                gap_vs_longest = max_loe - int(loe_year)

            if jur == "US":
                ctc_gap = "N/A"
            elif prev_loe is None:
                ctc_gap = 0
            else:
                ctc_gap = int(loe_year) - int(prev_loe)

            cats_present = cats_lookup.get((drug, jur), set())
            gap_desc = determine_protection_gap(cats_present)

            if jur == "US":
                score = "N/A"
                signal = "Reference market"
            else:
                gap_for_scoring = 0 if gap_vs_longest == "N/A (longest)" else int(gap_vs_longest)
                score = compute_arbitrage_score(
                    loe_year=int(loe_year),
                    gap_vs_longest=gap_for_scoring,
                    current_year=current_year,
                )
                country_scores[jur] = score
                signal = _SIGNALS[score]

            arb_rows.append({
                "Drug Name": drug,
                "Launch Order": launch_order,
                "Jurisdiction": jur,
                "Product LOE (Year)": int(loe_year),
                "Gap vs US (Years)": gap_vs_us,
                "Gap vs Longest LOE (Years)": gap_vs_longest,
                "Country-to-Country Arbitrage (Years)": ctc_gap,
                "Key Protection Gap": gap_desc,
                "Arbitrage Score": score,
                "Arbitrage Signal": signal,
            })

            if jur != "US":
                prev_loe = loe_year

        if country_scores:
            dim4 = round(sum(country_scores.values()) / len(country_scores), 2)
            dim4_rating = get_dimension_iv_rating(dim4)
        else:
            dim4 = "N/A"
            dim4_rating = "N/A"

        for row in arb_rows:
            if row["Drug Name"] == drug:
                row["Dimension IV Score"] = dim4
                row["Dimension IV Rating"] = dim4_rating

    return pd.DataFrame(arb_rows)


def attach_dimension_iv(shortlisted: pd.DataFrame, arbitrage: pd.DataFrame) -> tuple:
    """(shortlisted with the drug's Dimension IV score and rating merged in, score, rating).
    Score and rating are None when there is no arbitrage summary."""
    if arbitrage.empty:
        return shortlisted, None, None
    for _, row in arbitrage.iterrows():
        logger.info("[LAUNCH] #%s %s | LOE %s | score %s | %s", row["Launch Order"], row["Jurisdiction"],
                    row["Product LOE (Year)"], row["Arbitrage Score"], row["Arbitrage Signal"])
    merged = shortlisted.merge(
        arbitrage[["Drug Name", "Dimension IV Score", "Dimension IV Rating"]].drop_duplicates("Drug Name"),
        on="Drug Name", how="left")
    return merged, arbitrage["Dimension IV Score"].iloc[0], arbitrage["Dimension IV Rating"].iloc[0]
