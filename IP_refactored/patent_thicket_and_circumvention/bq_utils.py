"""
bq_utils.py
───────────
The BigQuery writes of the patent thicket / circumvention dimension (the glp_master_loe read and the
replace-a-drug's-rows step are shared: IP_refactored/bq_utils.py).

Writes (each run replaces only that drug's rows, so re-runs never duplicate):
    write_circumvention_to_bq(...)    design-around strategies, one row per strategy per claim category
                                      (config.BQ_CIRCUMVENTION_TABLE)
    write_score_to_bq(...)            thicket score, one row per jurisdiction plus an average row
                                      (config.BQ_THICKET_SCORE_TABLE)

Written rows follow the table's existing column types; columns the table lacks are added.
All BigQuery access goes through IP_refactored.gcp_helpers / gcp_utils; table IDs come from IP_refactored.config.
"""

import logging

from google.cloud import bigquery

from .. import config
from ..bq_utils import now_iso, write_drug_rows
from .scoring.scoring import THICKET_SCORE_LABELS

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Schemas (used when the table does not exist yet)
# ─────────────────────────────────────────────

CIRCUMVENTION_SCHEMA = [
    *[bigquery.SchemaField(name, "STRING") for name in (
        "Drug_Name", "Patent_Category", "Patents", "Overall_Difficulty", "Key_Claim_Limitations",
        "White_Space_Opportunities", "FDA_Precedents", "Orange_Book_Gaps", "Literature_Alternatives",
        "Regulatory_Viability", "Summary", "Analysis_Date", "Strategy", "Rationale", "Feasibility",
        "Regulatory_Pathway", "Prior_Art_Support", "updated_at")],
    bigquery.SchemaField("Num_Patents", "INTEGER"),
    bigquery.SchemaField("created_at", "TIMESTAMP"),
]

SCORE_SCHEMA = [
    *[bigquery.SchemaField(name, "STRING") for name in (
        "Drug_Name", "Jurisdiction", "Active_Categories", "Density_Interpretation",
        "Diversity_Interpretation", "Score_Label", "Rationale", "updated_at")],
    *[bigquery.SchemaField(name, "INTEGER") for name in (
        "Combined_Total", "Forecasted_Total", "Active_Technology_Areas", "Density_Score",
        "Diversity_Score", "Base_Score")],
    *[bigquery.SchemaField(name, "FLOAT64") for name in ("Adjusted_Count", "Validation_Pct", "Score")],
    bigquery.SchemaField("created_at", "TIMESTAMP"),
]


# ─────────────────────────────────────────────
# Row builders
# ─────────────────────────────────────────────

def _join(values) -> str:
    return "; ".join(str(v) for v in values or [])


# ─────────────────────────────────────────────
# Write — Circumvention_Table
# ─────────────────────────────────────────────

def build_circumvention_rows(drug: str, circ_data: dict, updated_at: str) -> list:
    """One row per design-around strategy (or one placeholder row if a category has none)."""
    rows = []
    analysis_date = circ_data.get("analysis_date", "")
    for category, result in circ_data.get("results_by_category", {}).items():
        common = {
            "Drug_Name":                 drug,
            "Patent_Category":           category,
            "Patents":                   ", ".join(str(x) for x in result.get("patent_numbers", [])),
            "Num_Patents":               int(result.get("patent_count", 0)),
            "Overall_Difficulty":        result.get("overall_circumvention_difficulty", "N/A"),
            "Key_Claim_Limitations":     _join(result.get("key_claim_limitations")),
            "White_Space_Opportunities": _join(result.get("white_space_opportunities")),
            "FDA_Precedents":            _join(result.get("fda_precedents")),
            "Orange_Book_Gaps":          _join(result.get("orange_book_gaps")),
            "Literature_Alternatives":   _join(result.get("literature_alternatives")),
            "Regulatory_Viability":      result.get("regulatory_viability", ""),
            "Summary":                   result.get("summary", ""),
            "Analysis_Date":             analysis_date,
            "updated_at":                updated_at,
        }
        strategies = result.get("design_around_strategies") or []
        if not strategies:
            rows.append({**common, "Strategy": "No strategies identified", "Rationale": "",
                         "Feasibility": "", "Regulatory_Pathway": "", "Prior_Art_Support": ""})
        for strat in strategies:
            rows.append({
                **common,
                "Strategy":           strat.get("strategy", ""),
                "Rationale":          strat.get("rationale", ""),
                "Feasibility":        strat.get("feasibility", ""),
                "Regulatory_Pathway": strat.get("regulatory_pathway", ""),
                "Prior_Art_Support":  strat.get("prior_art_support", ""),
            })
    return rows


def write_circumvention_to_bq(drug: str, circ_data: dict) -> None:
    """Replace the drug's rows in config.BQ_CIRCUMVENTION_TABLE with the new analysis."""
    rows = build_circumvention_rows(drug, circ_data, now_iso())
    write_drug_rows(rows, config.BQ_CIRCUMVENTION_TABLE, CIRCUMVENTION_SCHEMA, drug, drug_column="Drug_Name")


# ─────────────────────────────────────────────
# Write — Patent_Thicket_Score_Table
# ─────────────────────────────────────────────

def build_score_rows(drug_score: dict, updated_at: str, rationale: str = "") -> list:
    """One row per jurisdiction, plus a 'Final Score (Average)' row for the drug; each carries the drug's rationale."""
    drug = drug_score["drug"]
    rows = [{
        "Drug_Name":                drug,
        "Jurisdiction":             jd["jurisdiction"],
        "Combined_Total":           jd["combined_total"],
        "Forecasted_Total":         jd.get("forecasted_total", 0),
        "Adjusted_Count":           jd["adjusted_count"],
        "Active_Technology_Areas":  jd["active_areas"],
        "Active_Categories":        jd["active_categories"],
        "Density_Interpretation":   jd["density_label"],
        "Diversity_Interpretation": jd["diversity_label"],
        "Density_Score":            jd["density_score"],
        "Diversity_Score":          jd["diversity_score"],
        "Base_Score":               jd["base_score"],
        "Validation_Pct":           jd["validation_pct"],
        "Score":                    jd["final_score"],
        "Score_Label":              jd["final_label"],
        "Rationale":                rationale,  # the drug's summary, repeated on each of its rows
        "updated_at":               updated_at,
    } for jd in drug_score.get("jurisdiction_scores", [])]

    avg_final = drug_score["avg_final_score"]
    rows.append({
        "Drug_Name":                drug,
        "Jurisdiction":             "Final Score (Average)",
        "Active_Categories":        "",
        "Density_Interpretation":   "",
        "Diversity_Interpretation": "",
        "Score":                    avg_final,
        "Score_Label":              THICKET_SCORE_LABELS.get(max(1, min(5, round(avg_final))), ""),
        "Rationale":                rationale,
        "updated_at":               updated_at,
    })
    return rows


def write_score_to_bq(drug_score: dict, rationale: str = "") -> None:
    """Replace the drug's rows in config.BQ_THICKET_SCORE_TABLE with the new scores."""
    rows = build_score_rows(drug_score, now_iso(), rationale)
    write_drug_rows(rows, config.BQ_THICKET_SCORE_TABLE, SCORE_SCHEMA, drug_score["drug"], drug_column="Drug_Name")
