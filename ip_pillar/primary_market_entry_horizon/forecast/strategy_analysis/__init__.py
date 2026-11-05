"""
strategy_analysis
─────────────────
Forecast step 5: the company's business strategy and incentive to expand patent protection
(SEC EDGAR + Vertex AI Search + Gemini).

    review.py        review_business_strategy
    assessment.py    prompt, parsing and the Gemini assessment
    sec_edgar.py / web_search.py / companies.py    the data sources and per-company configuration
    bq_storage.py    company_analysis_table: schema, rows and write
    reporting.py / batch.py / constants.py / strategy_analysis.py (command-line entry point)
"""

from .review import review_business_strategy
from .bq_storage import _BQ_SCHEMA, _assessment_to_bq_row
from .reporting import print_assessment, export_to_excel

__all__ = ['review_business_strategy', '_BQ_SCHEMA', '_assessment_to_bq_row', 'print_assessment', 'export_to_excel']
