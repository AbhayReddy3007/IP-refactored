"""
bq_storage.py
─────────────
BigQuery table, row conversion and write for company_analysis_table.
"""

import json
from datetime import datetime, timezone
from typing import Dict
from google.cloud import bigquery
from .... import config, gcp_utils


# ─────────────────────────────────────────────────────────────────────────────
# Credentials + client initialisation
# ─────────────────────────────────────────────────────────────────────────────

class _LazyBQClient:
    """The shared BigQuery client, created on first use so importing this module needs no credentials."""
    def __getattr__(self, name):
        return getattr(gcp_utils.get_bq_client(), name)


_bq_client = _LazyBQClient()


# ─────────────────────────────────────────────────────────────────────────────
# BigQuery schema + table helpers
# ─────────────────────────────────────────────────────────────────────────────

_BQ_SCHEMA = [
    # ── metadata ─────────────────────────────────────────────────────────────
    bigquery.SchemaField("run_id",              "STRING",  description="UUID for this run"),
    bigquery.SchemaField("inserted_at",         "TIMESTAMP", description="Row insertion timestamp (UTC)"),
    bigquery.SchemaField("company",             "STRING"),
    bigquery.SchemaField("therapy_area",        "STRING"),
    bigquery.SchemaField("drugs",               "STRING",  description="Comma-separated drug list"),
    bigquery.SchemaField("analysis_date",       "DATE"),
    bigquery.SchemaField("sources_used",        "STRING",  description="Semicolon-separated source labels"),

    # ── revenue_dependency ───────────────────────────────────────────────────
    bigquery.SchemaField("rev_rating",          "STRING"),
    bigquery.SchemaField("rev_total_revenue",   "STRING"),
    bigquery.SchemaField("rev_therapy_revenue", "STRING"),
    bigquery.SchemaField("rev_percentage",      "STRING"),
    bigquery.SchemaField("rev_growth_rate",     "STRING"),
    bigquery.SchemaField("rev_drug_breakdown",  "STRING"),
    bigquery.SchemaField("rev_details",         "STRING"),

    # ── pipeline_depth ───────────────────────────────────────────────────────
    bigquery.SchemaField("pipe_rating",         "STRING"),
    bigquery.SchemaField("pipe_assets",         "STRING",  description="JSON array of asset strings"),
    bigquery.SchemaField("pipe_acquisitions",   "STRING"),
    bigquery.SchemaField("pipe_details",        "STRING"),

    # ── strategic_priority ───────────────────────────────────────────────────
    bigquery.SchemaField("strat_rating",        "STRING"),
    bigquery.SchemaField("strat_key_quotes",    "STRING",  description="JSON array of quote strings"),
    bigquery.SchemaField("strat_restructuring", "STRING"),
    bigquery.SchemaField("strat_details",       "STRING"),

    # ── indication_expansion ─────────────────────────────────────────────────
    bigquery.SchemaField("ind_rating",          "STRING"),
    bigquery.SchemaField("ind_planned",         "STRING",  description="JSON array of indication strings"),
    bigquery.SchemaField("ind_patent_impl",     "STRING"),
    bigquery.SchemaField("ind_details",         "STRING"),

    # ── lifecycle_management ─────────────────────────────────────────────────
    bigquery.SchemaField("lc_rating",           "STRING"),
    bigquery.SchemaField("lc_strategies",       "STRING",  description="JSON array of strategy strings"),
    bigquery.SchemaField("lc_new_formulations", "STRING"),
    bigquery.SchemaField("lc_new_devices",      "STRING"),
    bigquery.SchemaField("lc_new_dosing",       "STRING"),
    bigquery.SchemaField("lc_combinations",     "STRING"),
    bigquery.SchemaField("lc_details",          "STRING"),

    # ── competitive_pressure ─────────────────────────────────────────────────
    bigquery.SchemaField("comp_rating",         "STRING"),
    bigquery.SchemaField("comp_competitors",    "STRING",  description="JSON array of competitor strings"),
    bigquery.SchemaField("comp_patent_expiry",  "STRING"),
    bigquery.SchemaField("comp_biosimilar",     "STRING"),
    bigquery.SchemaField("comp_defense",        "STRING"),
    bigquery.SchemaField("comp_details",        "STRING"),

    # ── patent_expansion_likelihood ──────────────────────────────────────────
    bigquery.SchemaField("pat_rating",          "STRING"),
    bigquery.SchemaField("pat_formulation",     "STRING"),
    bigquery.SchemaField("pat_device",          "STRING"),
    bigquery.SchemaField("pat_dosing_regimen",  "STRING"),
    bigquery.SchemaField("pat_combination",     "STRING"),
    bigquery.SchemaField("pat_method_treatment","STRING"),
    bigquery.SchemaField("pat_manufacturing",   "STRING"),

    # ── overall ──────────────────────────────────────────────────────────────
    bigquery.SchemaField("overall_assessment",  "STRING"),
]


def _ensure_bq_table() -> str:
    """Create BQ table if it doesn't exist. Returns fully-qualified table ID."""
    fq = config.BQ_COMPANY_ANALYSIS_TABLE
    try:
        _bq_client.get_table(fq)
        print(f"[BQ] Table exists: {fq}")
    except Exception:
        table = bigquery.Table(fq, schema=_BQ_SCHEMA)
        table.time_partitioning = bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY,
            field="analysis_date",
        )
        _bq_client.create_table(table, exists_ok=True)
        print(f"[BQ] Table created: {fq}")
    return fq


def _assessment_to_bq_row(assessment: Dict) -> Dict:
    """Flatten the nested assessment dict into a single BQ row dict."""
    import uuid

    meta  = assessment.get("_metadata", {})
    rev   = assessment.get("revenue_dependency",          {})
    pipe  = assessment.get("pipeline_depth",              {})
    strat = assessment.get("strategic_priority",          {})
    ind   = assessment.get("indication_expansion",        {})
    lc    = assessment.get("lifecycle_management",        {})
    comp  = assessment.get("competitive_pressure",        {})
    pat   = assessment.get("patent_expansion_likelihood", {})

    def _lst(v) -> str:
        """Serialise a list to JSON string."""
        if isinstance(v, list):
            return json.dumps(v, ensure_ascii=False)
        return str(v) if v else ""

    def _s(v) -> str:
        if v is None:
            return ""
        return str(v)

    today = datetime.now(timezone.utc).date().isoformat()

    return {
        # metadata
        "run_id":              str(uuid.uuid4()),
        "inserted_at":         datetime.now(timezone.utc).isoformat(),
        "company":             _s(meta.get("company")),
        "therapy_area":        _s(meta.get("therapy_area")),
        "drugs":               ", ".join(meta.get("drugs", [])),
        "analysis_date":       meta.get("date", today),
        "sources_used":        "; ".join(meta.get("sources_used", [])),

        # revenue_dependency
        "rev_rating":          _s(rev.get("rating")),
        "rev_total_revenue":   _s(rev.get("total_company_revenue")),
        "rev_therapy_revenue": _s(rev.get("therapy_area_revenue")),
        "rev_percentage":      _s(rev.get("percentage")),
        "rev_growth_rate":     _s(rev.get("growth_rate")),
        "rev_drug_breakdown":  _s(rev.get("drug_breakdown")),
        "rev_details":         _s(rev.get("details")),

        # pipeline_depth
        "pipe_rating":         _s(pipe.get("rating")),
        "pipe_assets":         _lst(pipe.get("assets")),
        "pipe_acquisitions":   _s(pipe.get("recent_acquisitions")),
        "pipe_details":        _s(pipe.get("details")),

        # strategic_priority
        "strat_rating":        _s(strat.get("rating")),
        "strat_key_quotes":    _lst(strat.get("key_quotes")),
        "strat_restructuring": _s(strat.get("restructuring")),
        "strat_details":       _s(strat.get("details")),

        # indication_expansion
        "ind_rating":          _s(ind.get("rating")),
        "ind_planned":         _lst(ind.get("planned_indications")),
        "ind_patent_impl":     _s(ind.get("patent_implications")),
        "ind_details":         _s(ind.get("details")),

        # lifecycle_management
        "lc_rating":           _s(lc.get("rating")),
        "lc_strategies":       _lst(lc.get("strategies")),
        "lc_new_formulations": _s(lc.get("new_formulations")),
        "lc_new_devices":      _s(lc.get("new_devices")),
        "lc_new_dosing":       _s(lc.get("new_dosing")),
        "lc_combinations":     _s(lc.get("combinations")),
        "lc_details":          _s(lc.get("details")),

        # competitive_pressure
        "comp_rating":         _s(comp.get("rating")),
        "comp_competitors":    _lst(comp.get("competitors")),
        "comp_patent_expiry":  _s(comp.get("patent_expiry_dates")),
        "comp_biosimilar":     _s(comp.get("biosimilar_threats")),
        "comp_defense":        _s(comp.get("defense_strategy")),
        "comp_details":        _s(comp.get("details")),

        # patent_expansion_likelihood
        "pat_rating":          _s(pat.get("rating")),
        "pat_formulation":     _s(pat.get("formulation_patents")),
        "pat_device":          _s(pat.get("device_patents")),
        "pat_dosing_regimen":  _s(pat.get("dosing_regimen_patents")),
        "pat_combination":     _s(pat.get("combination_patents")),
        "pat_method_treatment":_s(pat.get("method_of_treatment_patents")),
        "pat_manufacturing":   _s(pat.get("manufacturing_patents")),

        # overall
        "overall_assessment":  _s(assessment.get("overall_assessment")),
    }


def _write_to_bq(assessment: Dict) -> bool:
    """Insert a single assessment row into BigQuery. Returns True on success."""
    if "error" in assessment:
        print("[BQ] Skipping — assessment has errors")
        return False
    try:
        fq  = _ensure_bq_table()
        row = _assessment_to_bq_row(assessment)
        errors = _bq_client.insert_rows_json(fq, [row])
        if errors:
            print(f"[BQ] Insert errors: {errors}")
            return False
        print(f"[BQ] Row inserted → {fq}")
        return True
    except Exception as e:
        print(f"[BQ] Write failed: {e}")
        return False
