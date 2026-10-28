"""
patent_strength
───────────────
IP dimension: legal robustness ("strength") of each blocking, granted patent.
Reads glp_master_loe (the output of primary_market_entry_horizon) and writes
patent_strength_table + patent_strength_country_score_table.

    patent_strength_analysis.py   patent_strength(drug) — the whole pipeline for one drug, plus its run summaries

    retrieval/   what goes in
        shortlist.py   which glp_master_loe patents get scored
        chunks.py      AlloyDB chunk retrieval + per-sub-factor re-ranking
    scoring/     how patents are scored
        scoring.py     Gemini sub-factor scoring (gemini_utils), weighted + country-wise scores
        prompts.py     the sub-factor prompts
        pipeline.py    score cache + concurrent scoring
        constants.py   weights, labels, keyword lists
    storage/     where state and results are kept
        bq_utils.py    BigQuery reads / writes
        checkpoint.py  per-drug GCS checkpoint
"""

from .patent_strength_analysis import patent_strength

__all__ = ["patent_strength"]
