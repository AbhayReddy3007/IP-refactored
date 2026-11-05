"""
constants.py
────────────
Project, BigQuery and HTTP settings.
"""

import os
from .... import config


# ─────────────────────────────────────────────────────────────────────────────
# Config — edit these or set via environment variables
# ─────────────────────────────────────────────────────────────────────────────

PROJECT_ID       = os.getenv("GCP_PROJECT_ID",      "cognito-prod-394707")
BQ_LOCATION      = os.getenv("BQ_LOCATION",          "asia-south1")
BQ_PROJECT_ID    = os.getenv("BQ_PROJECT_ID",        "cognito-prod-394707")
BQ_DATASET_ID    = os.getenv("BQ_DATASET_ID",        "cognito_prod_datamart")
BQ_TABLE_ID      = os.getenv("BQ_TABLE_ID",          "company_analysis_table")
CREDENTIALS_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")

GEMINI_MODEL    = config.GEMINI_TEXT_MODEL

_HTTP_HEADERS = {
    "User-Agent": "PatentPipeline/1.0 (patent.analysis@example.com)",
    "Accept":     "text/html,application/json",
}
_HTTP_DELAY = 0.5   # SEC EDGAR polite rate-limit
