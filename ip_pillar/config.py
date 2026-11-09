"""
config.py
─────────
Single source of truth for all configuration values (environment variables,
defaults, model names, tunables) used across the ip_pillar package.

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
# Reports go to GCS_BUCKET (unless REPORT_GCS_BUCKET overrides it), uploaded through a client for REPORT_GCS_PROJECT.
REPORT_GCS_BUCKET = os.getenv("REPORT_GCS_BUCKET") or GCS_BUCKET
REPORT_GCS_PROJECT = os.getenv("REPORT_GCS_PROJECT", "cognito-dev-380506")
REPORT_GCS_PATH   = os.getenv("REPORT_GCS_PATH", "Cognito_new/reports").strip("/")

# Dimension output payloads (JSON, saved by utils.store_payload):
#   gs://{GCS_BUCKET}/{PAYLOAD_PATH}/{drug}/{GCS_IP_SUBFOLDER}/{dimension}/output_payload.json
PAYLOAD_PATH = os.getenv("PAYLOAD_PATH", "Cognito_new/pipeline_cache").strip("/")

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

# Names of the IP dimensions. Each is the dimension's report file name (see
# REPORT_GCS_PATH) and the "dimension" recorded in the Gemini usage table (GEMINI_STATS_TABLE).
DIMENSION_PRIMARY_MARKET_ENTRY_HORIZON = os.getenv("DIMENSION_PRIMARY_MARKET_ENTRY_HORIZON", "Primary Market Entry Horizon")
DIMENSION_BLOCKING_ANALYSIS             = os.getenv("DIMENSION_BLOCKING_ANALYSIS", "Blocking Analysis")
DIMENSION_PATENT_STRENGTH              = os.getenv("DIMENSION_PATENT_STRENGTH", "Patent Strength and Invalidity Opportunity")
DIMENSION_PATENT_THICKET               = os.getenv("DIMENSION_PATENT_THICKET", "Patent Thicket and Circumvention Feasibility")
DIMENSION_GLOBAL_LAUNCH                = os.getenv("DIMENSION_GLOBAL_LAUNCH", "Global Launch Sequencing & Arbitrage")
DIMENSION_PTE_ANALYSIS                 = os.getenv("DIMENSION_PTE_ANALYSIS", "PTE Analysis")
DIMENSION_FORECAST                     = os.getenv("DIMENSION_FORECAST", "Forecast Analysis")
DIMENSION_LITIGATION                   = os.getenv("DIMENSION_LITIGATION", "Litigation Analysis")

# File names (without .pdf) of the reports that do not take their dimension's name.
REPORT_NAME_PATENT_STRENGTH = os.getenv("REPORT_NAME_PATENT_STRENGTH", "Patent Strength and Invalidity Opportunity")
REPORT_NAME_FORECAST        = os.getenv("REPORT_NAME_FORECAST", "Forecast_Report")
REPORT_NAME_LITIGATION      = os.getenv("REPORT_NAME_LITIGATION", "Litigation_Analysis")

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
INDEXER_HEARTBEAT_SECONDS = int(os.getenv("INDEXER_HEARTBEAT_SECONDS", "120"))

# Cover-page render DPI for Gemini Vision date extraction
COVER_PAGE_DPI = int(os.getenv("COVER_PAGE_DPI", "300"))

# Full-document OCR fallback (image-only / scanned PDFs)
OCR_PAGE_DPI   = int(os.getenv("OCR_PAGE_DPI", "200"))
OCR_MAX_PAGES  = int(os.getenv("OCR_MAX_PAGES", "50"))
OCR_BATCH_SIZE = int(os.getenv("OCR_BATCH_SIZE", "10"))

# ─────────────────────────────────────────────
# Blocking analysis (primary_market_entry_horizon/blocking_analysis/)
# ─────────────────────────────────────────────

# Retries for any Gemini JSON-mode call across Steps 1-5
MAX_ANALYSIS_RETRIES = int(os.getenv("MAX_ANALYSIS_RETRIES", "3"))

# Max patents analysed concurrently (Phase 1 and Phase 2 of the pipeline)
ANALYSIS_CONCURRENCY = int(os.getenv("ANALYSIS_CONCURRENCY", "5"))

# Step 3 evidence block cap (characters) passed into Steps 4/5 prompts
MAX_EVIDENCE_CHARS = int(os.getenv("MAX_EVIDENCE_CHARS", "12000"))

# Local Excel file with each drug's real-world formulation data (Step 2)
FORMULATION_EXCEL_PATH = os.getenv("FORMULATION_EXCEL_PATH", "")

# Step 2 reference data for a drug the Excel has no rows for: built from ClinicalTrials.gov + openFDA labels
# (blocking_analysis/formulation_lookup.py). Set FORMULATION_LOOKUP_ENABLED=false to turn it off.
FORMULATION_LOOKUP_ENABLED = os.getenv("FORMULATION_LOOKUP_ENABLED", "true").strip().lower() in ("1", "true", "yes")
# A drug's looked-up records are reused this long before the sources are read again.
FORMULATION_CACHE_DAYS = float(os.getenv("FORMULATION_CACHE_DAYS", "30"))
# GCS cache folder (within GCS_CACHE_PREFIX): gs://{GCS_BUCKET}/{GCS_CACHE_PREFIX}/{this}/{drug}/formulation_records.json
FORMULATION_CACHE_SUBFOLDER = os.getenv("FORMULATION_CACHE_SUBFOLDER", "formulation_cache")
# Most ClinicalTrials.gov studies shown to Gemini, and the cap on the size of what it is shown (characters).
CTGOV_MAX_STUDIES = int(os.getenv("CTGOV_MAX_STUDIES", "25"))
FORMULATION_MAX_PROMPT_CHARS = int(os.getenv("FORMULATION_MAX_PROMPT_CHARS", "60000"))
# Optional openFDA key (raises the rate limit; the lookup works without one).
OPENFDA_API_KEY = os.getenv("OPENFDA_API_KEY", "")

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
DRUG_NAME = os.getenv("DRUG_NAME", "Cotadutide,Brenipatide,Efocipegtrutide,Enicepatide,Pemvidutide,Albenatide,Pegapamodutide,Vk-2735,Liraglutide Biobetter,Ct-996")
# Drugs to leave out when DRUG_NAME is not set (comma-separated, same spelling rules as DRUG_NAME): "run every
# drug except these", e.g. the ones an earlier run already finished.
SKIP_DRUGS = os.getenv("SKIP_DRUGS", "Asc-30,Bi-3034701,Da-302168S,Aleniglipron L-Arginine,Benaglutide,Bgm-0504,Azd-9550,Azd-6234+Azd-9550,Berobenatide,Acmopatide")

# ─────────────────────────────────────────────
# Patent filter — quality gates (patent_filter.py)
# ─────────────────────────────────────────────
# Only patents whose patent_status matches this string AND whose eval_refined_confidence
# score exceeds the threshold are considered approved for indexing/analysis.
PATENT_STATUS_FILTER       = os.getenv("PATENT_STATUS_FILTER", "PDF Downloaded")
PATENT_CONFIDENCE_THRESHOLD = float(os.getenv("PATENT_CONFIDENCE_THRESHOLD", "0.3"))
# The patent_master column the threshold is applied to. Every patent_master query uses PATENT_CONFIDENCE_SQL
# (the column cast to FLOAT64, so a text-typed column still compares as a number); no other confidence column
# is read. Deliberately not an environment variable.
PATENT_CONFIDENCE_COLUMN = "eval_refined_confidence"
PATENT_CONFIDENCE_SQL    = f"SAFE_CAST({PATENT_CONFIDENCE_COLUMN} AS FLOAT64)"

# ─────────────────────────────────────────────
# AlloyDB — connection tunables (alloydb_utils.py)
# ─────────────────────────────────────────────
ALLOYDB_CONNECT_TIMEOUT = int(os.getenv("ALLOYDB_CONNECT_TIMEOUT", "30"))
ALLOYDB_RETRY_COUNT     = int(os.getenv("ALLOYDB_RETRY_COUNT", "3"))
ALLOYDB_RETRY_BACKOFF   = float(os.getenv("ALLOYDB_RETRY_BACKOFF", "2.0"))
# Connections kept open for reuse (a new connection costs ~2 s on Cloud Run -> AlloyDB; reuse costs ~0).
# Connections beyond this many idle ones are closed when released.
ALLOYDB_POOL_MAX_IDLE  = int(os.getenv("ALLOYDB_POOL_MAX_IDLE", "20"))
# An idle pooled connection older than this many seconds is tested with SELECT 1 before reuse.
ALLOYDB_POOL_TEST_AFTER_SECONDS = float(os.getenv("ALLOYDB_POOL_TEST_AFTER_SECONDS", "20"))
# How long list_collections() results are reused (collections only appear when a patent is first indexed).
ALLOYDB_COLLECTIONS_CACHE_SECONDS = float(os.getenv("ALLOYDB_COLLECTIONS_CACHE_SECONDS", "120"))
# Worker threads for the blocking calls (AlloyDB, GCS) the async code hands to asyncio.to_thread.
# asyncio's default is min(32, cpu + 4) = 6 on a 2-vCPU Cloud Run task, which would cap INDEXER_CONCURRENCY.
THREAD_POOL_WORKERS = int(os.getenv("THREAD_POOL_WORKERS", "32"))
# How long the list of patent folders in GCS (patent_filter) is reused within one process.
PATENT_FOLDERS_CACHE_SECONDS = float(os.getenv("PATENT_FOLDERS_CACHE_SECONDS", "300"))

# ─────────────────────────────────────────────
# Indexer — Gemini upload + date-extraction tunables (indexer.py)
# ─────────────────────────────────────────────
# How long (seconds) to wait for Gemini to finish processing an uploaded PDF
# before proceeding anyway.
GEMINI_UPLOAD_MAX_WAIT_SECONDS = int(os.getenv("GEMINI_UPLOAD_MAX_WAIT_SECONDS", "60"))
# Maximum PDF size (bytes) passed as a raw byte payload for date extraction;
# larger PDFs are trimmed to the first 2 pages before sending.
DATE_EXTRACTION_PDF_MAX_BYTES = int(os.getenv("DATE_EXTRACTION_PDF_MAX_BYTES", str(2 * 1024 * 1024)))

# ─────────────────────────────────────────────
# Forecasted LOE merge pipeline
# (primary_market_entry_horizon/score_calculation/merge_forecasted_loe.py, scoring.py; bq_utils.py)
# ─────────────────────────────────────────────

# JSON snapshot of each output table:
#   gs://{GCS_BUCKET}/{GCS_CACHE_PREFIX}/{GCS_JSON_EXPORT}/{table name}/{drug}_{YYYYMMDD_HHMMSS}.json
GCS_JSON_EXPORT = os.getenv("GCS_JSON_EXPORT", "loe_json_exports")

# primary_market_entry_horizon() also runs forecast() for the drug, between publishing glp_patent_loe
# (its input) and building glp_master_loe (which merges the new forecasted_loe rows). Set to false to skip it.
RUN_FORECAST = os.getenv("RUN_FORECAST", "true").strip().lower() not in ("0", "false", "no", "off")

# true (default): a drug that already has rows in forecasted_loe reuses them and forecast() is not run; a drug with no
# rows still runs it. false: forecast() runs on every horizon run. REINDEX also forces it to run.
SKIP_FORECAST = os.getenv("SKIP_FORECAST", "true").strip().lower() not in ("0", "false", "no", "off")

# Which IP dimension(s) run_ip_dimension runs:
#   all                              — all five, in the order below
#   primary_market_entry_horizon     — only that dimension
#   patent_strength                  — only that dimension (needs the drug's glp_master_loe rows)
#   patent_thicket_and_circumvention — only that dimension (needs the drug's glp_master_loe rows)
#   global_launch_sequencing_and_geographic_arbitrage — only that dimension (needs the drug's glp_master_loe rows)
#   litigations                      — only that dimension (reads patent_master)
IP_DIMENSION = os.getenv("IP_DIMENSION", "all").strip().lower()

# True = ignore caches / checkpoints and redo everything (re-index, re-analyse, re-score). The five dimension
# functions take only the drug name, so this is read from here; run_ip_dimension --reindex sets it.
REINDEX = os.getenv("REINDEX", "false").strip().lower() in ("1", "true", "yes")

# Rationale of a drug that has no patents for a dimension to assess (patent_strength, patent_thicket_and_circumvention).
NO_QUALIFYING_PATENTS_RATIONALE = (
    "No qualifying patents were found based on the currently assessment. "
    "This analysis will be updated if additional relevant patent information becomes available."
)

# When True, forecasted_loe rows for Marketed/Pre-registration drugs are kept
# (real LOE data does NOT supersede the forecast).
# Default False: real data supersedes forecasts for Marketed/Pre-reg drugs.
NO_MARKETED_SKIP = os.getenv("NO_MARKETED_SKIP", "false").lower() in ("1", "true", "yes")

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
# eval_refined_confidence > 0.3 — so indexing, analysis and the output tables are all
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

# patent_thicket_and_circumvention():
#   Circumvention_Table         — design-around strategies, one row per strategy per claim category.
#   Patent_Thicket_Score_Table  — thicket score, one row per jurisdiction plus an average row per drug.
BQ_CIRCUMVENTION_TABLE  = os.getenv("BQ_CIRCUMVENTION_TABLE", "cognito-prod-394707.cognito_prod_datamart.Circumvention_Table")
BQ_THICKET_SCORE_TABLE  = os.getenv("BQ_THICKET_SCORE_TABLE", "cognito-prod-394707.cognito_prod_datamart.Patent_Thicket_Score_Table")

# Global launch sequencing and geographic arbitrage: per-patent expiries and per-jurisdiction arbitrage.
BQ_LAUNCH_PATENTS_TABLE = os.getenv("BQ_LAUNCH_PATENTS_TABLE", "cognito-prod-394707.cognito_prod_datamart.shortlisted_secondary_patents_table")
BQ_ARBITRAGE_TABLE      = os.getenv("BQ_ARBITRAGE_TABLE", "cognito-prod-394707.cognito_prod_datamart.arbitrage_summary_table")

# forecast(): reads glp_patent_loe, writes forecasted_loe (FORECASTED_LOE_TABLE above), which the
# next primary_market_entry_horizon run merges into glp_master_loe.
#   forecast_s3          — patent layering + filing-stage table (steps 1-3), one row per patent
#   filing_pattern_table — innovator filing patterns (step 4), one row per innovator
#   company_analysis_table — the company's business-strategy / patent-expansion incentive (step 5)
# Retries (after the first attempt) of a drug's innovator filing-pattern analysis in forecast().
FORECAST_MAX_RETRIES = int(os.getenv("FORECAST_MAX_RETRIES", "1"))
# Drug -> parent company (columns cleaned_generic_name, Parent_Company_Name, Drug_Geography) used by forecast();
# defaults to the drug list view. If the lookup fails the company is found by a Gemini web search.
DRUG_COMPANY_TABLE = os.getenv("DRUG_COMPANY_TABLE") or os.getenv("BQ_TABLE_NAME") or DRUG_LIST_VIEW
BQ_FORECAST_S3_TABLE      = os.getenv("BQ_FORECAST_S3_TABLE", "cognito-prod-394707.cognito_prod_datamart.forecast_s3")
BQ_FILING_PATTERN_TABLE   = os.getenv("BQ_FILING_PATTERN_TABLE", "cognito-prod-394707.cognito_prod_datamart.filing_pattern_table")
BQ_COMPANY_ANALYSIS_TABLE = os.getenv("BQ_COMPANY_ANALYSIS_TABLE", "cognito-prod-394707.cognito_prod_datamart.company_analysis_table")

# litigations(): patent litigation and court cases found per patent, one row per case (a summary-only row for a
# drug with none).
BQ_LITIGATION_TABLE = os.getenv("BQ_LITIGATION_TABLE", "cognito-prod-394707.cognito_prod_datamart.litigation_analysis_table")

# Per-run Gemini usage (one row per model per drug).
GEMINI_STATS_TABLE = os.getenv("GEMINI_STATS_TABLE", f"{PROJECT_ID}.{BQ_DATASET_ID}.gemini_stats")

# Dimension scores (bare table name): one row per drug per scored IP dimension (see dim_scores.py).
DIM_SCORES_TABLE = os.getenv("DIM_SCORES_TABLE", "dim_scores_table")
