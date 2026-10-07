"""
gcp_utils.py
────────────
Shared Google Cloud helpers for BigQuery, GCS, and Gemini (genai) operations.

This is the ONLY module in the package that should talk to Google Cloud /
Google AI directly. Every other module (including everything under
chunking/) should call into the functions here instead of instantiating
its own bigquery.Client / storage.Client / genai.Client.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from google.cloud import bigquery, storage
from google.oauth2 import service_account

from .config import (
    BQ_DATASET_ID,
    DIM_SCORES_TABLE,
    GCS_BUCKET,
    GCS_CACHE_PREFIX,
    GCS_MEDICAL_POTENTIAL_SUBFOLDER,
    GCS_PIPELINE_CACHE_BASE_PATH,
    GCS_REPORT_BASE_PATH,
    GOOGLE_API_KEY,
    GOOGLE_APPLICATION_CREDENTIALS,
    PROJECT_ID,
)

logger = logging.getLogger(__name__)
PILLAR_SUBFOLDER = GCS_MEDICAL_POTENTIAL_SUBFOLDER


# ─────────────────────────────────────────────
# Client helpers
# ─────────────────────────────────────────────

def get_bq_client() -> bigquery.Client:
    """Return an authenticated BigQuery client.

    Uses the configured service-account file when present; otherwise falls back
    to Application Default Credentials.
    """
    credentials_path = GOOGLE_APPLICATION_CREDENTIALS or "service.json"
    if credentials_path and os.path.exists(credentials_path):
        credentials = service_account.Credentials.from_service_account_file(credentials_path)
        return bigquery.Client(project=PROJECT_ID, credentials=credentials)
    return bigquery.Client(project=PROJECT_ID)


def get_gcs_client() -> storage.Client:
    """Return an authenticated GCS client.

    Uses the configured service-account file when present; otherwise falls back
    to Application Default Credentials.
    """
    credentials_path = GOOGLE_APPLICATION_CREDENTIALS
    if credentials_path and Path(credentials_path).exists():
        return storage.Client.from_service_account_json(credentials_path)
    return storage.Client(project=PROJECT_ID)


class _LazyGenaiClient:
    """Lazily instantiate the real genai.Client on first attribute access.

    Importing this module (or anything that imports it) no longer requires
    GOOGLE_API_KEY / GEMINI_API_KEY to be present — the key is only required
    the first time the client is actually used.
    """

    _client = None

    def _resolve(self):
        if _LazyGenaiClient._client is None:
            if not GOOGLE_API_KEY:
                raise RuntimeError(
                    "GOOGLE_API_KEY or GEMINI_API_KEY must be set (see config.py) "
                    "to use the Gemini client."
                )
            from google import genai
            _LazyGenaiClient._client = genai.Client(api_key=GOOGLE_API_KEY)
        return _LazyGenaiClient._client

    def __getattr__(self, name):
        return getattr(self._resolve(), name)


_gemini_client_singleton = _LazyGenaiClient()


def get_gemini_client():
    """Return the shared (lazily-instantiated) Gemini genai client."""
    return _gemini_client_singleton


# ─────────────────────────────────────────────
# Generic GCS JSON / bytes cache helpers
# ─────────────────────────────────────────────
#
# Used by any module that needs to persist small JSON documents or files to
# GCS (progress markers, result caches, checkpoints, exports) without each
# module reimplementing its own blob read/write/retry logic.
#
# Layout: gs://{GCS_BUCKET}/{GCS_CACHE_PREFIX}/{subfolder}/[{drug_name}/]{filename}

import re as _re
import time as _time


def _cache_blob_path(subfolder: str, *parts: str) -> str:
    safe_parts = [_re.sub(r"[^a-zA-Z0-9_+.\-]", "_", p) for p in parts]
    return "/".join([GCS_CACHE_PREFIX, subfolder] + safe_parts)


def write_json(
    subfolder: str,
    filename: str,
    data: dict,
    drug_name: Optional[str] = None,
    retries: int = 4,
    backoff: float = 2.0,
) -> str:
    """Write a JSON dict to GCS with retry for transient errors (429/5xx)."""
    parts = [drug_name, filename] if drug_name else [filename]
    blob_name = _cache_blob_path(subfolder, *parts)
    content = json.dumps(data, indent=2, default=str)
    uri = f"gs://{GCS_BUCKET}/{blob_name}"

    bucket = get_gcs_client().bucket(GCS_BUCKET)
    for attempt in range(retries):
        try:
            bucket.blob(blob_name).upload_from_string(content, content_type="application/json")
            return uri
        except Exception as e:
            is_retryable = any(code in str(e) for code in ("429", "500", "503"))
            if is_retryable and attempt < retries - 1:
                _time.sleep(backoff * (2 ** attempt))
            else:
                raise
    return uri


def read_json(subfolder: str, filename: str, drug_name: Optional[str] = None) -> Optional[dict]:
    """Read a JSON dict from GCS. Returns None if not found."""
    parts = [drug_name, filename] if drug_name else [filename]
    blob_name = _cache_blob_path(subfolder, *parts)
    blob = get_gcs_client().bucket(GCS_BUCKET).blob(blob_name)
    if not blob.exists():
        return None
    try:
        return json.loads(blob.download_as_text(encoding="utf-8"))
    except Exception:
        return None


def blob_exists(subfolder: str, filename: str, drug_name: Optional[str] = None) -> bool:
    """Check if a cache blob exists."""
    parts = [drug_name, filename] if drug_name else [filename]
    blob_name = _cache_blob_path(subfolder, *parts)
    return get_gcs_client().bucket(GCS_BUCKET).blob(blob_name).exists()


def delete_blob(subfolder: str, filename: str, drug_name: Optional[str] = None) -> bool:
    """Delete a single cache blob. Returns True if it existed and was deleted."""
    parts = [drug_name, filename] if drug_name else [filename]
    blob_name = _cache_blob_path(subfolder, *parts)
    blob = get_gcs_client().bucket(GCS_BUCKET).blob(blob_name)
    if blob.exists():
        blob.delete()
        return True
    return False


def list_blob_names(subfolder: str, drug_name: Optional[str] = None, suffix: Optional[str] = None) -> List[str]:
    """List blob (file) names under a cache subfolder, optionally within a drug subdir."""
    parts = [drug_name] if drug_name else []
    prefix = _cache_blob_path(subfolder, *parts) + "/"
    blobs = get_gcs_client().list_blobs(GCS_BUCKET, prefix=prefix)
    names = []
    for b in blobs:
        name = b.name.split("/")[-1]
        if suffix and not name.endswith(suffix):
            continue
        if name:
            names.append(name)
    return names


def list_blobs_with_prefix(prefix: str):
    """List raw GCS blob objects under *prefix* in the configured bucket."""
    return list(get_gcs_client().list_blobs(GCS_BUCKET, prefix=prefix))


def write_bytes(
    subfolder: str,
    filename: str,
    data: bytes,
    content_type: str = "application/octet-stream",
    drug_name: Optional[str] = None,
    retries: int = 4,
    backoff: float = 2.0,
) -> str:
    """Write raw bytes (e.g. an Excel/PDF file) to GCS with retry. Returns the GCS URI."""
    parts = [drug_name, filename] if drug_name else [filename]
    blob_name = _cache_blob_path(subfolder, *parts)
    uri = f"gs://{GCS_BUCKET}/{blob_name}"

    bucket = get_gcs_client().bucket(GCS_BUCKET)
    for attempt in range(retries):
        try:
            bucket.blob(blob_name).upload_from_string(data, content_type=content_type)
            return uri
        except Exception as e:
            is_retryable = any(code in str(e) for code in ("429", "500", "503"))
            if is_retryable and attempt < retries - 1:
                _time.sleep(backoff * (2 ** attempt))
            else:
                raise
    return uri


def read_bytes(subfolder: str, filename: str, drug_name: Optional[str] = None) -> Optional[bytes]:
    """Read raw bytes from GCS. Returns None if not found."""
    parts = [drug_name, filename] if drug_name else [filename]
    blob_name = _cache_blob_path(subfolder, *parts)
    blob = get_gcs_client().bucket(GCS_BUCKET).blob(blob_name)
    if not blob.exists():
        return None
    return blob.download_as_bytes()


# ─────────────────────────────────────────────
# Report / payload upload (GCS)
# ─────────────────────────────────────────────

def upload_dimension_report_pdf_to_gcs(
    pdf_bytes: bytes,
    molecule_name: str,
    dimension_name: str,
) -> tuple[str | None, str | None]:
    """Upload a dimension report PDF to GCS and return the main and archived GCS URIs.

    Writes two copies:
    - the current report at the dimension path
    - an archived copy with a timestamp suffix
    """
    try:
        molecule = (molecule_name or "").strip()
        if not molecule:
            raise ValueError("molecule_name must be provided")

        client = get_gcs_client()
        bucket = client.bucket(GCS_BUCKET)

        pillar_subfolder = PILLAR_SUBFOLDER
        main_gcs_path = f"{GCS_REPORT_BASE_PATH}/{molecule}/{pillar_subfolder}/{dimension_name}.pdf"
        main_blob = bucket.blob(main_gcs_path)
        main_blob.upload_from_string(pdf_bytes, content_type="application/pdf")
        main_gcs_uri = f"gs://{GCS_BUCKET}/{main_gcs_path}"
        logger.info("[REPORT_UPLOAD] PDF uploaded to GCS for dimension '%s': %s", dimension_name, main_gcs_uri)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archived_gcs_path = (
            f"{GCS_REPORT_BASE_PATH}/{molecule}/{pillar_subfolder}/archived/{dimension_name}_{timestamp}.pdf"
        )
        archived_blob = bucket.blob(archived_gcs_path)
        archived_blob.upload_from_string(pdf_bytes, content_type="application/pdf")
        logger.info(
            "[REPORT_UPLOAD] Archived PDF uploaded to GCS for dimension '%s': gs://%s/%s",
            dimension_name,
            GCS_BUCKET,
            archived_gcs_path,
        )

        archive_gcs_uri = f"gs://{GCS_BUCKET}/{archived_gcs_path}"

        return main_gcs_uri, archive_gcs_uri
    except Exception as exc:
        logger.warning("[REPORT_UPLOAD] Failed to upload PDF to GCS for dimension '%s': %s", dimension_name, exc)
        return None, None


def upload_dimension_payload_cache_to_gcs(
    payload: dict,
    molecule_name: str,
    dimension_name: str,
) -> str | None:
    """Upload a final dimension payload to GCS and return the main cache URI.

    Writes two copies:
    - the current payload at the dimension path
    - an archived copy with a timestamp suffix
    """
    try:
        molecule = (molecule_name or "").strip()
        pillar = (GCS_MEDICAL_POTENTIAL_SUBFOLDER or "").strip()
        dimension = (dimension_name or "").strip()
        if not molecule:
            raise ValueError("molecule_name must be provided")
        if not pillar:
            raise ValueError("GCS_MEDICAL_POTENTIAL_SUBFOLDER must be configured")
        if not dimension:
            raise ValueError("dimension_name must be provided")

        client = get_gcs_client()
        bucket = client.bucket(GCS_BUCKET)

        cache_root = GCS_PIPELINE_CACHE_BASE_PATH
        main_gcs_path = f"{cache_root}/{molecule}/{pillar}/{dimension}/output_payload.json"
        payload_bytes = json.dumps(payload, indent=2, default=str).encode("utf-8")

        main_blob = bucket.blob(main_gcs_path)
        main_blob.upload_from_string(payload_bytes, content_type="application/json")
        main_gcs_uri = f"gs://{GCS_BUCKET}/{main_gcs_path}"
        logger.info(
            "[CACHE_UPLOAD] Final payload uploaded to GCS for molecule '%s', pillar '%s', dimension '%s': %s",
            molecule,
            pillar,
            dimension,
            main_gcs_uri,
        )

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archived_gcs_path = f"{cache_root}/{molecule}/{pillar}/{dimension}/archived/output_payload_{timestamp}.json"
        archived_blob = bucket.blob(archived_gcs_path)
        archived_blob.upload_from_string(payload_bytes, content_type="application/json")
        logger.info(
            "[CACHE_UPLOAD] Archived final payload uploaded to GCS for molecule '%s', pillar '%s', dimension '%s': gs://%s/%s",
            molecule,
            pillar,
            dimension,
            GCS_BUCKET,
            archived_gcs_path,
        )

        return main_gcs_uri
    except Exception as exc:
        logger.warning(
            "[CACHE_UPLOAD] Failed to upload final payload to GCS for molecule '%s', pillar '%s', dimension '%s': %s",
            molecule_name,
            GCS_MEDICAL_POTENTIAL_SUBFOLDER,
            dimension_name,
            exc,
        )
        return None


# ─────────────────────────────────────────────
# BigQuery — dimension score upload
# ─────────────────────────────────────────────

DIM_SCORES_SCHEMA: list[bigquery.SchemaField] = [
    bigquery.SchemaField("product", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("pillar", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("dimension", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("score", "FLOAT", mode="NULLABLE"),
    bigquery.SchemaField("rationale", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("timestamp", "TIMESTAMP", mode="NULLABLE"),
]


def append_dimension_score_to_bigquery(
    molecule_name: str,
    dimension_name: str,
    score: float | int | None,
    pillar_name: str = "Medical Potential",
    rationale: str | None = None,
) -> None:
    """Append one dimension-score row to the configured BigQuery table.

    The payload is normalized to the shared dim-scores schema and written in
    append mode, creating the table if it does not already exist.
    """
    table_id = f"{PROJECT_ID}.{BQ_DATASET_ID}.{DIM_SCORES_TABLE}"
    row = {
        "product": (molecule_name or "").strip() or None,
        "pillar": (pillar_name or "").strip() or None,
        "dimension": (dimension_name or "").strip() or None,
        "score": float(score) if score is not None else None,
        "rationale": (rationale or "").strip() or None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    client = get_bq_client()
    job_config = bigquery.LoadJobConfig(
        schema=DIM_SCORES_SCHEMA,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        create_disposition=bigquery.CreateDisposition.CREATE_IF_NEEDED,
    )

    load_job = client.load_table_from_json([row], table_id, job_config=job_config)
    load_job.result()
    logger.info(
        "[DIM_SCORE] Appended dimension score for molecule '%s', pillar '%s', dimension '%s' to %s",
        row["product"],
        row["pillar"],
        row["dimension"],
        table_id,
    )
