"""
constants.py
────────────
Gemini model, run dates, BigQuery tables.
"""

import os
from datetime import datetime
from .... import config


# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────

_GEMINI_MODEL = config.GEMINI_TEXT_MODEL

_CURRENT_YEAR  = datetime.now().year
_CURRENT_DATE  = datetime.now().strftime("%Y-%m-%d")

BQ_PROJECT_ID      = os.getenv("BQ_PROJECT_ID", "cognito-prod-394707")
BQ_DATASET_ID      = os.getenv("BQ_DATASET_ID", "cognito_prod_datamart")
DRUG_COMPANY_TABLE = config.DRUG_COMPANY_TABLE         # drug-company mapping table
BQ_SERVICE_ACCOUNT = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")

# ── Input BQ tables ──────────────────────────────────────────────────────────
BQ_FORECAST_S3_TABLE       = config.BQ_FORECAST_S3_TABLE
BQ_FILING_PATTERN_TABLE    = config.BQ_FILING_PATTERN_TABLE
BQ_COMPANY_ANALYSIS_TABLE  = config.BQ_COMPANY_ANALYSIS_TABLE

# ── Output BQ table ─────────────────────────────────────────────────────────
BQ_OUTPUT_TABLE = config.FORECASTED_LOE_TABLE
