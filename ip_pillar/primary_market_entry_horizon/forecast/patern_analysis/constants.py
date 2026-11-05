"""
constants.py
────────────
Batch, BigQuery and registry settings.
"""

from .... import config


# ✅ CONFIG
MAX_CONCURRENT_DRUGS = 3
TIMEOUT_PER_DRUG = 300
MAX_RETRIES = config.FORECAST_MAX_RETRIES
LOG_FILE = "batch_errors.log"

# ✅ BIGQUERY CONFIG
BQ_LOCATION = "asia-south1"

OUTPUT_DIR = None  # No longer used — output goes to GCS


_STEP4_SUBFOLDER = "innovator_patterns"

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
def log_error(msg):
    print(f"[STEP4 ERROR] {msg}")

# ─────────────────────────────────────────────
# Gemini configuration (for inlined analysis)
# ─────────────────────────────────────────────

_GEMINI_MODEL          = config.GEMINI_TEXT_MODEL

_REGISTRIES = [
    {"name": "ClinicalTrials.gov",          "url": "https://clinicaltrials.gov/"},
    {"name": "ChiCTR",                      "url": "https://www.chictr.org.cn/"},
    {"name": "EU Clinical Trials Register", "url": "https://www.clinicaltrialsregister.eu/"},
    {"name": "CTRI",                        "url": "https://ctri.nic.in/"},
    {"name": "JRCT",                        "url": "https://rctportal.niph.go.jp/en/"},
    {"name": "ANZCTR",                      "url": "https://www.anzctr.org.au/"},
    {"name": "CRIS",                        "url": "https://cris.nih.go.kr/"},
    {"name": "ReBEC",                       "url": "https://ensaiosclinicos.gov.br/"},
]
