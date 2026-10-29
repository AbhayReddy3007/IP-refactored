"""
patent_thicket_and_circumvention
────────────────────────────────
IP dimension: how dense and diverse the secondary-patent thicket around a drug is, and how
feasible it is to design around it (505(b)(2)). Reads glp_master_loe (the output of
primary_market_entry_horizon) and writes Circumvention_Table + Patent_Thicket_Score_Table.

    patent_thicket_circumvention_analysis.py   patent_thicket_and_circumvention(drug) — the whole pipeline for one drug
    bq_utils.py          BigQuery writes specific to this dimension (shared ones: IP_refactored/bq_utils.py)

    retrieval/   what goes in
        data_prep.py        the drug's glp_master_loe rows, cleaned, approved patents only; its NON-BLOCKING patents
        alloydb_helpers.py  patent-chunk retrieval (collections and client come from chunking)
    scoring/     how the drug is scored and analysed
        scoring.py          density / diversity / final thicket score per jurisdiction
        circumvention.py    design-around analysis per claim category (AlloyDB chunks + Gemini + Google Search)
        prompts.py          the Gemini prompts
        checkpoint.py       per-drug GCS cache of finished circumvention results
        constants.py        excluded categories, limits, concurrency
    generate_report_and_rationale/   written on every run
        generate_rationale.py   the drug's summary of the scores and the design-around analysis (not stored in BigQuery)
        generate_report.py      the PDF report, uploaded to GCS
"""

from .patent_thicket_circumvention_analysis import patent_thicket_and_circumvention

__all__ = ["patent_thicket_and_circumvention"]
