"""
config.py
─────────
Single source of truth for all configuration values (environment variables,
defaults, model names, tunables) used across the IP_refactored package.

Every other module imports what it needs from here instead of calling
os.getenv() directly. This keeps all configuration visible in one place and
makes env vars easy to audit / change in one spot.

Values are read from the process environment. A `.env` file (searched for
from this package's directory upwards) is loaded first if present; variables
already set in the environment win over the file, so Cloud Run job settings
are never overridden by a stray .env.
"""

import os

from dotenv import load_dotenv

load_dotenv(override=False)

# ─────────────────────────────────────────────
# Google Cloud project / auth
# ─────────────────────────────────────────────

PROJECT_ID = os.getenv("PROJECT_ID") or os.getenv("BQ_PROJECT_ID") or "cognito-prod-394707"
GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")

# ─────────────────────────────────────────────
# BigQuery
# ─────────────────────────────────────────────

# Every BigQuery table / view is defined in the "BigQuery tables" section at the
# end of this file.
BQ_DATASET_ID = os.getenv("BQ_DATASET_ID", "cognito_prod_datamart")

# ─────────────────────────────────────────────
# GCS — bucket / paths
# ─────────────────────────────────────────────

# Single canonical bucket used for everything (reports, caches, patents).
GCS_BUCKET = os.getenv("GCS_BUCKET") or os.getenv("GCS_BUCKET_NAME", "cognito-gcs")

GCS_REPORT_BASE_PATH            = os.getenv("GCS_REPORT_BASE_PATH", "reports")
GCS_MEDICAL_POTENTIAL_SUBFOLDER = os.getenv("GCS_MEDICAL_POTENTIAL_SUBFOLDER", "medical_potential")
GCS_PIPELINE_CACHE_BASE_PATH    = os.getenv("GCS_PIPELINE_CACHE_BASE_PATH", "pipeline_cache")

# Generic cache prefix used by gcp_utils' blob-cache helpers (progress
# markers, JSON caches, etc.) — defaults to the pipeline cache base path.
GCS_CACHE_PREFIX = os.getenv("GCS_CACHE_PREFIX", GCS_PIPELINE_CACHE_BASE_PATH)

# Per-drug subfolder for the IP pillar's reports.
GCS_IP_SUBFOLDER = os.getenv("GCS_IP_SUBFOLDER", "IP")

# LOE (Primary Market) PDF reports (primary_market_entry_horizon/generate_report_and_rationale/generate_report.py):
#   gs://{REPORT_GCS_BUCKET}/{REPORT_GCS_PATH}/{drug}/{GCS_IP_SUBFOLDER}/Primary Market Entry Horizon.pdf
#   gs://{REPORT_GCS_BUCKET}/{REPORT_GCS_PATH}/{drug}/{GCS_IP_SUBFOLDER}/archive/Primary Market Entry Horizon+{timestamp}.pdf
REPORT_GCS_BUCKET = os.getenv("REPORT_GCS_BUCKET", "cognito-prod") or GCS_BUCKET
REPORT_GCS_PATH   = os.getenv("REPORT_GCS_PATH", "Cognito_new/reports").strip("/")

# Patent PDFs live under gs://{GCS_BUCKET}/{GCS_PATENTS_PREFIX}/{drug_name}/*.pdf
GCS_PATENTS_PREFIX = os.getenv("GCS_PATENTS_PREFIX", "Cognito_new/Master_patent_list")

# Subfolder (within GCS_CACHE_PREFIX) where indexer progress/resume markers live
GCS_INDEXER_PROGRESS_SUBFOLDER = os.getenv("GCS_INDEXER_PROGRESS_SUBFOLDER", "indexer_progress")

# ─────────────────────────────────────────────
# AlloyDB (PostgreSQL + pgvector)
# ─────────────────────────────────────────────

ALLOYDB_HOST     = os.getenv("ALLOYDB_HOST", "")
ALLOYDB_PASSWORD = os.getenv("ALLOYDB_PASSWORD", "")
ALLOYDB_USER     = os.getenv("ALLOYDB_USER", "postgres")
ALLOYDB_DB       = os.getenv("ALLOYDB_DB", "postgres")
ALLOYDB_PORT     = int(os.getenv("ALLOYDB_PORT", "5432"))

# Embedding vector dimension stored in AlloyDB — must match
# GEMINI_EMBEDDING_MODEL's output dimensionality.
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "3072"))

# ─────────────────────────────────────────────
# Gemini / genai
# ─────────────────────────────────────────────

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY", "")

GEMINI_TEXT_MODEL      = os.getenv("GEMINI_TEXT_MODEL", "gemini-2.5-flash")
GEMINI_EMBEDDING_MODEL = os.getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001")

# Every Gemini call goes through gemini_utils (Vertex AI, ADC + PROJECT_ID).
GEMINI_LOCATION           = os.getenv("GEMINI_LOCATION", "global")
GEMINI_EMBEDDING_LOCATION = os.getenv("GEMINI_EMBEDDING_LOCATION", "us-central1")
# "flex" tries the flex tier first and falls back to standard; empty = standard only.
GEMINI_SERVICE_TIER       = os.getenv("GEMINI_SERVICE_TIER", "")
# Optional thinking level (MINIMAL / LOW / MEDIUM / HIGH). Leave empty for models
# without thinking levels (e.g. gemini-2.5-flash).
GEMINI_THINKING_LEVEL     = os.getenv("GEMINI_THINKING_LEVEL", "")

# Dimension name recorded in the per-run Gemini usage table (GEMINI_STATS_TABLE).
GEMINI_STATS_DIMENSION   = os.getenv("GEMINI_STATS_DIMENSION", "Primary Market Entry Horizon")

# PDFs are sent inline to Gemini (Vertex has no Files API), so this caps the PDF size.
GEMINI_FILE_SIZE_LIMIT_MB = int(os.getenv("GEMINI_FILE_SIZE_LIMIT_MB", "50"))
MAX_UPLOAD_RETRIES        = int(os.getenv("MAX_UPLOAD_RETRIES", "3"))
MAX_EMBED_RETRIES         = int(os.getenv("MAX_EMBED_RETRIES", "3"))

# ─────────────────────────────────────────────
# Chunking / indexing tunables
# ─────────────────────────────────────────────

CHUNK_SIZE_CHARS = int(os.getenv("CHUNK_SIZE_CHARS", "2000"))
OVERLAP_CHARS    = int(os.getenv("OVERLAP_CHARS", "400"))

# Max concurrent patents processed in parallel per drug
INDEXER_CONCURRENCY = int(os.getenv("INDEXER_CONCURRENCY", "10"))

# How often (seconds) the indexer logs a heartbeat — a snapshot of which
# patents are currently in flight and how long each has been running, so a
# long run never goes silent even while every patent is still mid-step
# (e.g. waiting on a slow Gemini call).
INDEXER_HEARTBEAT_SECONDS = int(os.getenv("INDEXER_HEARTBEAT_SECONDS", "30"))

# Cover-page render DPI for Gemini Vision date extraction
COVER_PAGE_DPI = int(os.getenv("COVER_PAGE_DPI", "300"))

# Full-document OCR fallback (image-only / scanned PDFs)
OCR_PAGE_DPI   = int(os.getenv("OCR_PAGE_DPI", "200"))
OCR_MAX_PAGES  = int(os.getenv("OCR_MAX_PAGES", "50"))
OCR_BATCH_SIZE = int(os.getenv("OCR_BATCH_SIZE", "10"))

# ─────────────────────────────────────────────
# Blocking analysis (blocking_analysis/)
# ─────────────────────────────────────────────

# Retries for any Gemini JSON-mode call across Steps 1-5
MAX_ANALYSIS_RETRIES = int(os.getenv("MAX_ANALYSIS_RETRIES", "3"))

# Max patents analysed concurrently (Phase 1 and Phase 2 of the pipeline)
ANALYSIS_CONCURRENCY = int(os.getenv("ANALYSIS_CONCURRENCY", "5"))

# Step 3 evidence block cap (characters) passed into Steps 4/5 prompts
MAX_EVIDENCE_CHARS = int(os.getenv("MAX_EVIDENCE_CHARS", "12000"))

# Local Excel file with each drug's real-world formulation data (Step 2)
FORMULATION_EXCEL_PATH = os.getenv("FORMULATION_EXCEL_PATH", "")

# Subfolder (within GCS_CACHE_PREFIX) where per-patent blocking-analysis
# results are cached, so re-runs only analyse NEW patents.
GCS_ANALYSIS_CACHE_SUBFOLDER = os.getenv("GCS_ANALYSIS_CACHE_SUBFOLDER", "analysis_cache")

# PubMed / Europe PMC evidence gathering (Step 3)
PUBMED_EMAIL   = os.getenv("PUBMED_EMAIL", "patent_analysis@example.com")
NCBI_API_KEY   = os.getenv("NCBI_API_KEY", "")

# ─────────────────────────────────────────────
# Cloud Run Jobs — sharding (run_ip_dimension.py)
# ─────────────────────────────────────────────
# Provided automatically by Cloud Run Jobs for each task in the job; default
# to a single task when run locally / outside Cloud Run.
CLOUD_RUN_TASK_INDEX = int(os.getenv("CLOUD_RUN_TASK_INDEX", "0"))
CLOUD_RUN_TASK_COUNT = int(os.getenv("CLOUD_RUN_TASK_COUNT", "1"))

# ─────────────────────────────────────────────
# Drug selection (run_ip_dimension.py)
# ─────────────────────────────────────────────
# Optional. When set, run_ip_dimension.py runs ONLY for the drug(s) named here
# instead of discovering every drug folder under GCS_PATENTS_PREFIX.
# Accepts a single drug name, or multiple drug names separated by commas,
# e.g. "Semaglutide" or "Semaglutide, Tirzepatide, Liraglutide".
# Leave unset / empty to process every drug found in GCS (the default,
# sharded-discovery behaviour).
DRUG_NAME = os.getenv("DRUG_NAME", "")

# ─────────────────────────────────────────────
# Patent filter — quality gates (patent_filter.py)
# ─────────────────────────────────────────────
# Only patents whose patent_status matches this string AND whose confidence
# score exceeds the threshold are considered approved for indexing/analysis.
PATENT_STATUS_FILTER       = os.getenv("PATENT_STATUS_FILTER", "PDF Downloaded")
PATENT_CONFIDENCE_THRESHOLD = float(os.getenv("PATENT_CONFIDENCE_THRESHOLD", "0.3"))

# ─────────────────────────────────────────────
# AlloyDB — connection tunables (chunking/alloydb_client.py)
# ─────────────────────────────────────────────
ALLOYDB_CONNECT_TIMEOUT = int(os.getenv("ALLOYDB_CONNECT_TIMEOUT", "30"))
ALLOYDB_RETRY_COUNT     = int(os.getenv("ALLOYDB_RETRY_COUNT", "3"))
ALLOYDB_RETRY_BACKOFF   = float(os.getenv("ALLOYDB_RETRY_BACKOFF", "2.0"))

# ─────────────────────────────────────────────
# Indexer — Gemini upload + date-extraction tunables (chunking/indexer.py)
# ─────────────────────────────────────────────
# How long (seconds) to wait for Gemini to finish processing an uploaded PDF
# before proceeding anyway.
GEMINI_UPLOAD_MAX_WAIT_SECONDS = int(os.getenv("GEMINI_UPLOAD_MAX_WAIT_SECONDS", "60"))
# Maximum PDF size (bytes) passed as a raw byte payload for date extraction;
# larger PDFs are trimmed to the first 2 pages before sending.
DATE_EXTRACTION_PDF_MAX_BYTES = int(os.getenv("DATE_EXTRACTION_PDF_MAX_BYTES", str(2 * 1024 * 1024)))

# ─────────────────────────────────────────────
# Primary market entry horizon — exclusivity offsets + score thresholds
# (primary_market_entry_horizon/score_calculation/patent_scores.py)
# ─────────────────────────────────────────────
# Statutory patent term, counted from the filing date (before any PTE).
PATENT_TERM_YEARS = 20

# Years of market exclusivity added on top of the approval date per jurisdiction.
EXCLUSIVITY_OFFSET_US      = int(os.getenv("EXCLUSIVITY_OFFSET_US", "5"))
EXCLUSIVITY_OFFSET_EP      = int(os.getenv("EXCLUSIVITY_OFFSET_EP", "10"))
EXCLUSIVITY_OFFSET_DEFAULT = int(os.getenv("EXCLUSIVITY_OFFSET_DEFAULT", "8"))

# Avg-years-to-entry breakpoints for the 1–5 IP score:
#   avg <= SCORE_THRESHOLD_5 → 5   (most blocking)
#   avg <= SCORE_THRESHOLD_4 → 4
#   avg <= SCORE_THRESHOLD_3 → 3
#   avg <= SCORE_THRESHOLD_2 → 2
#   else                     → 1
SCORE_THRESHOLD_5 = int(os.getenv("SCORE_THRESHOLD_5", "6"))
SCORE_THRESHOLD_4 = int(os.getenv("SCORE_THRESHOLD_4", "8"))
SCORE_THRESHOLD_3 = int(os.getenv("SCORE_THRESHOLD_3", "11"))
SCORE_THRESHOLD_2 = int(os.getenv("SCORE_THRESHOLD_2", "13"))

# ─────────────────────────────────────────────
# Approval date fetcher — HTTP / API tunables
# (primary_market_entry_horizon/score_calculation/approval_date_fetcher.py)
# ─────────────────────────────────────────────
APPROVAL_HTTP_DELAY           = float(os.getenv("APPROVAL_HTTP_DELAY", "0.7"))
APPROVAL_HTTP_TIMEOUT         = int(os.getenv("APPROVAL_HTTP_TIMEOUT", "15"))
APPROVAL_DOMAIN_CHECK_TIMEOUT = float(os.getenv("APPROVAL_DOMAIN_CHECK_TIMEOUT", "3.0"))
FDA_API_BRAND_SEARCH_LIMIT    = int(os.getenv("FDA_API_BRAND_SEARCH_LIMIT", "10"))
FDA_API_APPROVAL_SEARCH_LIMIT = int(os.getenv("FDA_API_APPROVAL_SEARCH_LIMIT", "5"))

# ─────────────────────────────────────────────
# Forecasted LOE merge pipeline
# (primary_market_entry_horizon/score_calculation/merge_forecasted_loe.py, scoring.py; bq_utils.py)
# ─────────────────────────────────────────────

# JSON snapshot of each output table:
#   gs://{GCS_BUCKET}/{GCS_CACHE_PREFIX}/{GCS_JSON_EXPORT}/{table name}/{drug}_{YYYYMMDD_HHMMSS}.json
GCS_JSON_EXPORT = os.getenv("GCS_JSON_EXPORT", "loe_json_exports")

# Which IP dimension(s) run_ip_dimension runs:
#   all (default)                — primary_market_entry_horizon, then patent_strength
#   primary_market_entry_horizon — only that dimension
#   patent_strength              — only that dimension (needs the drug's glp_master_loe rows)
IP_DIMENSION = os.getenv("IP_DIMENSION", "all").strip().lower()

# When True, forecasted_loe rows for Marketed/Pre-registration drugs are kept
# (real LOE data does NOT supersede the forecast).
# Default False: real data supersedes forecasts for Marketed/Pre-reg drugs.
NO_MARKETED_SKIP = os.getenv("NO_MARKETED_SKIP", "false").lower() in ("1", "true", "yes")

# ── Column config for the merge pipeline ─────────────────────────────────────
# These are structured constants — too complex for env vars, edit in-place.

# Columns to drop from the combined DataFrame before writing to BQ.
DROP_COLUMNS: list = ["rationale"]

# Maps normalised(forecasted_loe column) → normalised(target column).
# Add entries here when auto-alignment fails because a column name in
# forecasted_loe is too different from the matching patent-table column name.
MANUAL_COLUMN_ALIASES: dict = {
    "filingdatelower": "filingdate",
    "globalphase":     "phase",
}

# Canonical output column definitions for glp_master_loe (in output order).
# Each entry: (canonical_name, [alternative_token_lists]).
# Each alternative is a list of lowercase tokens that must ALL appear in the
# normalised column name, or a tuple (required_tokens, exclude_tokens).
# Columns not found in the merged DataFrame are filled with blank (NA).
FINAL_COLUMNS: list = [
    ("Drug Name",                       [["drug"]]),
    ("Jurisdiction",                    [["jurisdiction"]]),
    ("Tag",                             [["tag"]]),
    ("Type",                            [["type"]]),
    ("Filing Date",                     [["filing", "date"]]),
    ("Patent Number",                   [["patent", "number"]]),
    ("No Of Forecasted Patents",        [["noofforecastedpatents"],
                                          ["numberofforecastedpatents"],
                                          ["forecastedpatents"],
                                          ["noofforecasted"]]),
    ("Years to Entry",                  [(["years", "entry"], ["avg"])]),
    ("Avg Years to Entry (US & EP)",    [["avgyears", "usep"], ["avgyears"]]),
    ("IP Dimension 1 Score",            [["ipdimension"]]),
    ("Step 1 Claim Category",           [["step1claimcategory"], ["step", "claim", "category"]]),
    ("Grant Date",                      [["grant", "date"]]),
    ("PTE (months)",                    [["pte"]]),
    ("Pediatric Exclusivity",           [["pediatric"]]),
    ("Controlling Patent Expiry Year",  [["controlling", "expiry"]]),
    ("Exclusivity Year",                [["exclusivity", "year"]]),
    ("Approval Date",                   [["approval", "date"]]),
    ("Est. Approval Year",              [["est", "approval", "year"]]),
    ("Phase",                           [["phase"]]),
    ("Strategic Purpose",               [["strategic", "purpose"]]),
    ("Risk Assessment",                 [["risk", "assessment"]]),
    ("Overall Forecast",                [["overall", "forecast"]]),
    ("Portfolio Gaps",                  [["portfolio", "gaps"]]),
    ("Existing Patent Summary",         [["existing", "patent", "summary"]]),
    ("Reason",                          [["reason"]]),
]

# BigQuery column types for the output tables, keyed by friendly column name.
# Any column not listed here is written as STRING. An explicit schema keeps
# the table stable across runs (autodetect would type an all-blank column
# differently from one run to the next and break WRITE_APPEND).
BQ_COLUMN_TYPES: dict = {
    "No Of Forecasted Patents":       "INT64",
    "Years to Entry":                 "FLOAT64",
    "Avg Years to Entry (US & EP)":   "FLOAT64",
    "IP Dimension 1 Score":           "FLOAT64",
    "PTE (months)":                   "FLOAT64",
    "Controlling Patent Expiry Year": "FLOAT64",
    "Exclusivity Year":               "FLOAT64",
    "Est. Approval Year":             "FLOAT64",
    "Avg Years to Entry":             "FLOAT64",
    "Score":                          "FLOAT64",
}

# ─────────────────────────────────────────────
# BigQuery tables
# ─────────────────────────────────────────────
# Every table / view the pipeline reads or writes. Each is ONE fully-qualified
# ID (project.dataset.table) except DIM_SCORES_TABLE, a bare table name that
# gcp_utils combines with PROJECT_ID / BQ_DATASET_ID.

# ── Reads ────────────────────────────────────

# Patent-master: every patent known for each molecule, with its download/review
# status and a confidence score. patent_filter() only returns GCS PDFs whose
# patent number has a row here with patent_status = 'PDF Downloaded' and
# confidence > 0.3 — so indexing, analysis and the output tables are all
# restricted to this approved set.
PATENT_MASTER_TABLE = os.getenv("PATENT_MASTER_TABLE", "cognito-dev-380506.stage.patent_master")

# View queried to resolve the target drug list (by cleaned_generic_name) for a
# mechanism-of-action / target filter, e.g. every GLP-1 drug (drug_list.py).
DRUG_LIST_VIEW = os.getenv("DRUG_LIST_VIEW", "cognito-prod-394707.cognito_prod_datamart.vw_drug_details_full")

# Clinical-trial phase data (lives in a separate GCP project from everything else).
CLINICAL_EFFICACY_TABLE = os.getenv("CLINICAL_EFFICACY_TABLE", "cognito-dev-380506.data_mart.clinical_efficacy_glp1")

# Secondary phase source ("the drug list table"): per-drug details (phase, LOE date, geography).
DRUG_DETAILS_TABLE = os.getenv("DRUG_DETAILS_TABLE", "cognito-dev-380506.data_mart.vw_drug_details_full")

# Brands table; empty = brand lookup disabled.
BQ_BRANDS_TABLE = os.getenv("BQ_BRANDS_TABLE", "")

# Forecasted patent LOE data merged into glp_master_loe.
FORECASTED_LOE_TABLE = os.getenv(
    "FORECASTED_LOE_TABLE",
    "cognito-prod-394707.cognito_prod_datamart.forecasted_loe",
)

# ── Writes ───────────────────────────────────
# Each run replaces only that drug's rows, so re-runs and parallel Cloud Run
# tasks (one drug each) never duplicate or clobber other drugs' rows.

# primary_market_entry_horizon():
#   glp_patent_loe — blocking-analysis results only (Type="Existing") + scores.
#   glp_master_loe — patents merged with forecasted_loe (Type="Forecasted") + final scores;
#                    also the input of patent_strength().
BQ_PATENT_LOE_TABLE = os.getenv("BQ_PATENT_LOE_TABLE", "cognito-prod-394707.cognito_prod_datamart.glp_patent_loe")
BQ_MASTER_LOE_TABLE = os.getenv("BQ_MASTER_LOE_TABLE", "cognito-prod-394707.cognito_prod_datamart.glp_master_loe")

# patent_strength():
BQ_PATENT_STRENGTH_TABLE = os.getenv("BQ_PATENT_STRENGTH_TABLE", "cognito-prod-394707.cognito_prod_datamart.patent_strength_analysis_table")
BQ_COUNTRY_SCORE_TABLE   = os.getenv("BQ_COUNTRY_SCORE_TABLE", "cognito-prod-394707.cognito_prod_datamart.patent_strength_country_score_table")

# Per-run Gemini usage (one row per model per drug).
GEMINI_STATS_TABLE = os.getenv("GEMINI_STATS_TABLE", f"{PROJECT_ID}.{BQ_DATASET_ID}.gemini_stats")

# Dimension scores (bare table name).
DIM_SCORES_TABLE = os.getenv("DIM_SCORES_TABLE", "dimension_scores")
