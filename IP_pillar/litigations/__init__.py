"""
litigations
───────────
IP dimension 5: the patent litigation and court cases found for a drug's patents (ANDA Paragraph IV, IPR at PTAB,
EPO oppositions, India, compounding-pharmacy suits). Reads patent_master (patents with confidence above
the threshold) and writes the litigation table.

    litigation_analysis.py   litigation_analysis(drug) — the whole pipeline for one drug
    bq_utils.py              the litigation table: earlier rows read, this run's rows merged in and written
                             (shared ones: IP_pillar/bq_utils.py)
    constants.py             search sizes, temperatures and the case types

    retrieval/   what goes in
        patents.py          the drug's patent numbers and filing dates (patent_master, confidence > threshold)
        marketed.py         whether the drug is Marketed (brand names are looked up only then)
    search/      how the cases are found
        steps.py            innovator and brand names, then the grouped litigation search (Gemini + Google Search)
        results.py          case-type normalisation, de-duplication, grouping, summary
        prompts.py          the Gemini prompts
        json_utils.py       tolerant parsing of Gemini's JSON replies
    generate_report/   written on every run
        generate_report.py  the 2-page PDF report (Gemini + Google Search) from the drug's rows, uploaded to GCS

A patent already in the litigation table is not searched again; a patent with no cases gets a row with no case,
so it is not searched again either.
"""

from .litigation_analysis import litigation_analysis

__all__ = ["litigation_analysis"]
