"""Patent thicket scoring: density, diversity, final score and labels."""

import logging

from .constants import EXCLUDED_CATEGORIES

logger = logging.getLogger(__name__)

# ── Thicket Score ─────────────────────────────────────────────────────────────
HIGH_DOMAIN_CATEGORIES = {
    "chemistry", "device", "manufacturing", "software",
    "api", "formulation", "delivery device", "method of use",
}

THICKET_SCORE_LABELS = {
    5: "Exceptional – No meaningful secondary patent fence",
    4: "Strong – High probability of design-around",
    3: "Moderate – Requires structured strategy",
    2: "Weak – Circumvention and litigation become costly",
    1: "Poor – Dense Patent Thicket",
}


def compute_diversity_score(active_areas: int) -> int:
    """
    No. of Technology Areas | Diversity Score
    1–2                     | 5
    3                       | 3
    4–5                     | 2
    ≥6                      | 1
    0                       | 5  (no patents at all)
    """
    if active_areas <= 2:
        return 5
    if active_areas == 3:
        return 3
    if active_areas <= 5:
        return 2
    return 1


def compute_density_score(adjusted_count: float) -> int:
    """
    Total Adjusted Count | Density Score
    0–5                  | 5
    6–15                 | 3
    16–30                | 2
    >30                  | 1
    """
    if adjusted_count <= 5:
        return 5
    if adjusted_count <= 15:
        return 3
    if adjusted_count <= 30:
        return 2
    return 1


def compute_final_score(total_patents: int, combined_total: int,
                        adjusted_count: float, active_areas: int) -> dict:
    """
    Computes diversity score, density score, base score, validation %,
    and final score for a jurisdiction block.

    If diversity_score == 5, adjusted_count is reduced by 1 before
    computing the density score.

    Base Score = min(density_score, diversity_score)
    If density_score <= 2 AND diversity_score <= 2: base_score -= 1
    If validation_pct < 50: base_score += 1
    Final Score = max(1, min(5, base_score))
    """
    diversity_score = compute_diversity_score(active_areas)

    # If diversity score is 5 (low diversity), reduce adjusted count by 1
    effective_adjusted = adjusted_count - 1 if diversity_score == 5 else adjusted_count
    effective_adjusted = max(0, effective_adjusted)  # don't go negative

    density_score = compute_density_score(effective_adjusted)

    base_score = min(density_score, diversity_score)

    if density_score <= 2 and diversity_score <= 2:
        base_score -= 1

    # Validation % = (Total Number Of Patents / Total Combined) * 100
    if combined_total > 0:
        validation_pct = (total_patents / combined_total) * 100
    else:
        validation_pct = 100.0  # no patents at all

    if validation_pct < 50:
        base_score += 1

    final_score = max(1, min(5, base_score))

    return {
        "diversity_score": diversity_score,
        "density_score": density_score,
        "effective_adjusted_count": effective_adjusted,
        "base_score": base_score,
        "validation_pct": round(validation_pct, 1),
        "final_score": final_score,
        "final_label": THICKET_SCORE_LABELS[final_score],
    }


# ── Thicket thresholds ────────────────────────────────────────────────────────
def patent_count_label(n):
    if n == 0:   return "No Patents"
    if n <= 5:   return "Low Density Patent Thicket (0–5)"
    if n <= 15:  return "Moderate Density Patent Thicket (6–15)"
    if n <= 30:  return "High Density Patent Thicket (16–30)"
    return           "Dense / Multi-Layer Patent Thicket (>30)"

def diversity_label(areas):
    if areas == 0:  return "No Active Areas"
    if areas <= 2:  return "Low Diversity (1–2 areas)"
    if areas == 3:  return "Moderate Diversity (3 areas)"
    if areas <= 5:  return "High Diversity (4–5 areas)"
    return              "Dense / Multi-Domain Patent Thicket (>5 areas)"



def compute_block_scores(block_df, forecasted_source_df, filtered_cats):
    """
    Compute score info for a given dataframe block (one jurisdiction or all).

    Returns (score_info, active_cat_names, combined_total, adjusted_count,
             active_areas).
    """
    category_counts = block_df.groupby("Step 1 Claim Category").size().to_dict()
    category_counts = {cat: category_counts.get(cat, 0) for cat in filtered_cats}

    if not forecasted_source_df.empty:
        forecasted_counts = (
            forecasted_source_df
            .groupby("Step 1 Claim Category")["No Of Forecasted Patents"]
            .max()
            .to_dict()
        )
    else:
        forecasted_counts = {}
    forecasted_counts = {cat: int(forecasted_counts.get(cat, 0)) for cat in filtered_cats}

    total_patents    = sum(category_counts.values())
    forecasted_total = sum(forecasted_counts.values())
    combined_total   = total_patents + forecasted_total
    adjusted_count   = total_patents + (forecasted_total * 0.5)

    active_cat_names = [cat for cat in filtered_cats if category_counts[cat] > 0]
    active_areas     = len(active_cat_names)

    score_info = compute_final_score(
        total_patents, combined_total, adjusted_count, active_areas
    )
    return (score_info, active_cat_names, combined_total, adjusted_count, active_areas)


def score_drug(drug, drug_df, df, all_categories):
    """
    Score one drug: per-jurisdiction thicket scores (WO skipped) plus a drug-level summary.

    drug_df is the drug's NON-BLOCKING rows; df is the full master frame, used for the
    drug's Type == "Forecasted" rows.
    """
    forecasted_drug_df = df[
        (df["Drug Name"] == drug) &
        (df["Type"].str.lower() == "forecasted")
    ].copy()
    logger.info(f"Drug '{drug}': {len(forecasted_drug_df)} forecasted rows found")

    filtered_cats = [c for c in all_categories if c not in EXCLUDED_CATEGORIES]

    # ── Per-Jurisdiction score computation (skip WO) ──────────────────────
    jurisdiction_scores = []

    if "Jurisdiction" in drug_df.columns:
        jurisdictions = sorted(
            j for j in drug_df["Jurisdiction"].dropna().unique()
            if j not in ("", "nan", "None") and j.upper() != "WO"
        )
        for jurisdiction in jurisdictions:
            jur_df = drug_df[drug_df["Jurisdiction"] == jurisdiction].copy()
            if "Jurisdiction" in forecasted_drug_df.columns:
                jur_forecasted_df = forecasted_drug_df[
                    forecasted_drug_df["Jurisdiction"] == jurisdiction
                ].copy()
            else:
                jur_forecasted_df = forecasted_drug_df.copy()

            j_score_info, j_cats, j_combined, j_adjusted, j_active_areas = \
                compute_block_scores(jur_df, jur_forecasted_df, filtered_cats)

            jurisdiction_scores.append({
                "drug":             drug,
                "jurisdiction":     jurisdiction,
                "combined_total":   j_combined,
                "forecasted_total": j_combined - len(jur_df),
                "adjusted_count":   j_adjusted,
                "active_areas":     j_active_areas,
                "active_categories": ", ".join(j_cats) if j_cats else "None",
                "density_label":    patent_count_label(j_score_info["effective_adjusted_count"]),
                "diversity_label":  diversity_label(j_active_areas),
                "density_score":    j_score_info["density_score"],
                "diversity_score":  j_score_info["diversity_score"],
                "base_score":       j_score_info["base_score"],
                "validation_pct":   j_score_info["validation_pct"],
                "final_score":      j_score_info["final_score"],
                "final_label":      j_score_info["final_label"],
            })

    # ── Average final score across jurisdictions ───────────────────────────
    if jurisdiction_scores:
        avg_final = round(
            sum(js["final_score"] for js in jurisdiction_scores)
            / len(jurisdiction_scores), 1
        )
    else:
        avg_final = 0

    # ── Overall counts (non-WO, for the drug summary) ─────────────────────
    non_wo_df = drug_df[drug_df["Jurisdiction"].str.upper() != "WO"] \
        if "Jurisdiction" in drug_df.columns else drug_df
    counts = non_wo_df.groupby("Step 1 Claim Category").size().to_dict()
    active_cat_names = [c for c in filtered_cats if counts.get(c, 0) > 0]

    return {
        "drug":               drug,
        "total_patents":      sum(counts.get(c, 0) for c in filtered_cats),
        "active_areas":       len(active_cat_names),
        "active_categories":  ", ".join(active_cat_names) if active_cat_names else "None",
        "combined_total":     sum(js["combined_total"] for js in jurisdiction_scores),
        "adjusted_count":     sum(js["adjusted_count"] for js in jurisdiction_scores),
        "avg_final_score":    avg_final,
        "jurisdiction_scores": jurisdiction_scores,
    }
