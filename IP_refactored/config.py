"""
config.py
─────────
Single source of truth for all configuration values (environment variables,
defaults, model names, tunables) used across the IP_refactored package.

Every other module imports what it needs from here instead of calling
os.getenv() directly. This keeps all configuration visible in one place and
makes env vars easy to audit / change in one spot.
"""

import os

# ─────────────────────────────────────────────
# Google Cloud project / auth
# ─────────────────────────────────────────────

PROJECT_ID = os.getenv("PROJECT_ID") or os.getenv("BQ_PROJECT_ID") or "cognito-prod-394707"
GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")

# ─────────────────────────────────────────────
# BigQuery
# ─────────────────────────────────────────────

BQ_PROJECT_ID    = os.getenv("BQ_PROJECT_ID", PROJECT_ID)
BQ_DATASET_ID    = os.getenv("BQ_DATASET_ID", "cognito_prod_datamart")
DIM_SCORES_TABLE = os.getenv("DIM_SCORES_TABLE", "dimension_scores")

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

GEMINI_FILE_SIZE_LIMIT_MB = int(os.getenv("GEMINI_FILE_SIZE_LIMIT_MB", "2000"))
MAX_UPLOAD_RETRIES        = int(os.getenv("MAX_UPLOAD_RETRIES", "3"))
MAX_EMBED_RETRIES         = int(os.getenv("MAX_EMBED_RETRIES", "3"))

# ─────────────────────────────────────────────
# Chunking / indexing tunables
# ─────────────────────────────────────────────

CHUNK_SIZE_CHARS = int(os.getenv("CHUNK_SIZE_CHARS", "2000"))
OVERLAP_CHARS    = int(os.getenv("OVERLAP_CHARS", "400"))

# Max concurrent patents processed in parallel per drug
INDEXER_CONCURRENCY = int(os.getenv("INDEXER_CONCURRENCY", "10"))

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
# Primary market entry horizon — phase fetching (phase_fetcher.py)
# ─────────────────────────────────────────────

# Fully-qualified (project.dataset.table) BigQuery table for clinical-trial
# phase data. Lives in a separate GCP project from everything else
# (cognito-dev-380506.data_mart), so it's given fully-qualified rather than
# combined with BQ_PROJECT_ID/BQ_DATASET_ID. phase_fetcher.py detects a
# fully-qualified value (2 dots) and uses it as-is; a bare table name is
# still combined with BQ_PROJECT_ID/BQ_DATASET_ID for backward compatibility.
CLINICAL_EFFICACY_TABLE = os.getenv("CLINICAL_EFFICACY_TABLE", "cognito-dev-380506.data_mart.clinical_efficacy_glp1")

# Secondary phase source ("the drug list table") — the same BigQuery view
# chunking/drug_list.py resolves the GLP-1 drug list from (DRUG_LIST_VIEW).
# Bare table/view name: phase_fetcher.py looks it up in the SAME
# project/dataset as CLINICAL_EFFICACY_TABLE above (cognito-dev-380506.data_mart)
# whenever that table is fully-qualified, since this view lives alongside it.
# (DRUG_LIST_VIEW itself is defined further down this file, as
# "vw_drug_details_full" — kept as a literal default here since it's used
# before that point in the file.)
DRUG_DETAILS_TABLE = os.getenv("DRUG_DETAILS_TABLE", "vw_drug_details_full")

# Local Excel bundled in the image as a fallback when BigQuery phase data is
# missing for a drug/jurisdiction. Defaults to a file living next to this
# module in primary_market_entry_horizon/.
PHASE_FALLBACK_EXCEL = os.getenv("PHASE_FALLBACK_EXCEL", "")

# ─────────────────────────────────────────────
# Primary market entry horizon — approval date fetching (approval_date_fetcher.py)
# ─────────────────────────────────────────────

BQ_BRANDS_TABLE = os.getenv("BQ_BRANDS_TABLE", "")

# ─────────────────────────────────────────────
# Primary market entry horizon — Excel export (excel_exporter.py)
# ─────────────────────────────────────────────

# GCS location (gs://{GCS_BUCKET}/{GCS_EXCEL}/...) where per-drug and
# combined Excel reports are written. This is the single output location
# dimension_1.py writes its Excel deliverable to.
GCS_EXCEL = os.getenv("GCS_EXCEL", "patent_exports")

# ─────────────────────────────────────────────
# Cloud Run Jobs — sharding (dimension_1.py)
# ─────────────────────────────────────────────
# Provided automatically by Cloud Run Jobs for each task in the job; default
# to a single task when run locally / outside Cloud Run.
CLOUD_RUN_TASK_INDEX = int(os.getenv("CLOUD_RUN_TASK_INDEX", "0"))
CLOUD_RUN_TASK_COUNT = int(os.getenv("CLOUD_RUN_TASK_COUNT", "1"))

# ─────────────────────────────────────────────
# Drug selection (dimension_1.py)
# ─────────────────────────────────────────────
# Optional. When set, dimension_1.py runs ONLY for the drug(s) named here
# instead of discovering every drug folder under GCS_PATENTS_PREFIX.
# Accepts a single drug name, or multiple drug names separated by commas,
# e.g. "Semaglutide" or "Semaglutide, Tirzepatide, Liraglutide".
# Leave unset / empty to process every drug found in GCS (the default,
# sharded-discovery behaviour).
DRUG_NAME = os.getenv("DRUG_NAME", "")

# ─────────────────────────────────────────────
# Patent-master filter (chunking/patent_filter.py)
# ─────────────────────────────────────────────
# Fully-qualified BigQuery table (project.dataset.table) listing every
# patent known for each molecule, with its download/review status and a
# confidence score. patent_filter() only returns GCS PDFs whose patent
# number has a row here with patent_status = 'PDF Downloaded' and
# confidence > 0.3 — so indexing, analysis, and the Excel output are all
# restricted to this approved set, for either a single molecule or (when
# no molecule is given) every molecule in the table.
PATENT_MASTER_TABLE = os.getenv("PATENT_MASTER_TABLE", "cognito-dev-380506.stage.patent_master")

# ─────────────────────────────────────────────
# Drug-list discovery via BigQuery (chunking/drug_list.py)
# ─────────────────────────────────────────────
# BigQuery view queried to resolve the target drug list (by
# cleaned_generic_name) for a given mechanism-of-action / target filter —
# e.g. every GLP-1 drug — instead of relying on GCS folder discovery or a
# manually-set DRUG_NAME. Lives in the same project/dataset as everything
# else (PROJECT_ID / BQ_DATASET_ID above).
DRUG_LIST_VIEW = os.getenv("DRUG_LIST_VIEW", "vw_drug_details_full")