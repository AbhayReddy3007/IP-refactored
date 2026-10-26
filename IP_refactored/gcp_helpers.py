"""
gcp_helpers.py
──────────────
Extra Google Cloud helpers built on top of gcp_utils (which is left untouched):

    call_gemini_json()   Gemini JSON-mode call with retries, shared by blocking-analysis Steps 1-5
    upload_blob()        upload bytes to an absolute GCS object path
    run_query()          BigQuery query -> DataFrame (STRING query parameters)
    table_exists()       BigQuery table existence check
    delete_rows()        BigQuery DELETE ... WHERE, returns rows deleted
    load_dataframe()     append a DataFrame to a BigQuery table with an explicit schema

Clients, credentials and settings all come from gcp_utils / config.
"""

import asyncio
import json
import logging
import random
import re
from typing import Dict, List, Optional

import pandas as pd
from google.api_core.exceptions import NotFound
from google.cloud import bigquery

from .config import GCS_BUCKET, GEMINI_TEXT_MODEL, MAX_ANALYSIS_RETRIES
from .gcp_utils import get_bq_client, get_gcs_client, get_gemini_client

logger = logging.getLogger(__name__)


async def call_gemini_json(prompt: str, filename: str, step: str) -> Optional[Dict]:
    """
    Calls Gemini 2.5 Flash with JSON response mode.
    Retries up to MAX_ANALYSIS_RETRIES times (see config.py) on truncation or empty response.
    On truncation retry, appends a concise-output instruction to reduce token usage.
    Used by every blocking-analysis step (1-5).
    """
    from google.genai import types

    _CONCISE_SUFFIX = (
        "\n\nIMPORTANT: Your previous response was truncated. "
        "Return ONLY the JSON object. Keep every string field under 60 words. "
        "Do NOT include any explanation outside the JSON."
    )

    for attempt in range(1, MAX_ANALYSIS_RETRIES + 1):
        try:
            # On retry after truncation, ask for a more concise response
            current_prompt = prompt if attempt == 1 else prompt + _CONCISE_SUFFIX

            response = await get_gemini_client().aio.models.generate_content(
                model=GEMINI_TEXT_MODEL,
                contents=current_prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                    max_output_tokens=8192,
                ),
            )

            raw_text = response.text if response.text else ""

            try:
                finish_str = str(response.candidates[0].finish_reason)
                logger.info(f"[{step}] finish_reason for {filename} (attempt {attempt}): {finish_str}")
                if finish_str in ("MAX_TOKENS", "2"):
                    logger.warning(f"[WARNING] Response truncated for {filename} (attempt {attempt}) — retrying with concise prompt")
                    if attempt < MAX_ANALYSIS_RETRIES:
                        await asyncio.sleep(2 + random.uniform(0, 1))
                        continue
                elif finish_str in ("SAFETY", "3"):
                    logger.error(f"[ERROR] Safety filter blocked response for {filename}.")
                    return None
            except (IndexError, AttributeError):
                pass

            if not raw_text.strip():
                logger.error(f"[ERROR] Empty response for {filename} (attempt {attempt})")
                if attempt < MAX_ANALYSIS_RETRIES:
                    await asyncio.sleep(2)
                    continue
                return None

            clean = raw_text.strip()
            if "```" in clean:
                clean = re.sub(r"```(?:json)?", "", clean).replace("```", "").strip()

            # Catch unterminated JSON before trying to parse — sign of silent truncation
            if clean and not clean.rstrip().endswith("}"):
                logger.warning(f"[WARNING] Response appears truncated (no closing brace) for {filename} (attempt {attempt})")
                if attempt < MAX_ANALYSIS_RETRIES:
                    await asyncio.sleep(2 + random.uniform(0, 1))
                    continue

            logger.info(f"[{step}] Raw response for {filename}: {clean[:300]!r}")
            return json.loads(clean)

        except json.JSONDecodeError as e:
            logger.error(f"[ERROR] JSON parse failed for {filename} (attempt {attempt}): {e}")
            if attempt < MAX_ANALYSIS_RETRIES:
                await asyncio.sleep(2)
                continue
            return None

        except Exception as e:
            logger.error(f"[ERROR] Gemini call failed for {filename} (attempt {attempt}): {e}")
            if attempt < MAX_ANALYSIS_RETRIES:
                await asyncio.sleep(2)
                continue
            return None

    return None


def upload_blob(blob_name: str, data: bytes, content_type: str = "application/octet-stream",
                bucket: Optional[str] = None) -> str:
    """Upload *data* to gs://{bucket or GCS_BUCKET}/{blob_name} (an absolute
    object path, unlike write_bytes which writes under the pipeline cache).
    Returns the GCS URI."""
    bucket_name = bucket or GCS_BUCKET
    get_gcs_client().bucket(bucket_name).blob(blob_name).upload_from_string(data, content_type=content_type)
    return f"gs://{bucket_name}/{blob_name}"


def _string_params(params: Optional[Dict[str, str]]) -> List[bigquery.ScalarQueryParameter]:
    return [bigquery.ScalarQueryParameter(name, "STRING", value) for name, value in (params or {}).items()]


def run_query(sql: str, params: Optional[Dict[str, str]] = None) -> pd.DataFrame:
    """Run a SQL query and return the result as a DataFrame. *params* are
    bound as STRING query parameters (reference them as @name in the SQL)."""
    job_config = bigquery.QueryJobConfig(query_parameters=_string_params(params))
    return get_bq_client().query(sql, job_config=job_config).to_dataframe()


def table_exists(table_id: str) -> bool:
    try:
        get_bq_client().get_table(table_id)
        return True
    except NotFound:
        return False


def delete_rows(table_id: str, where_sql: str, params: Optional[Dict[str, str]] = None) -> int:
    """DELETE FROM table_id WHERE where_sql. Returns the number of rows deleted
    (0 if the table doesn't exist yet)."""
    if not table_exists(table_id):
        return 0
    job_config = bigquery.QueryJobConfig(query_parameters=_string_params(params))
    job = get_bq_client().query(f"DELETE FROM `{table_id}` WHERE {where_sql}", job_config=job_config)
    job.result()
    return job.num_dml_affected_rows or 0


def load_dataframe(df: pd.DataFrame, table_id: str, schema: List[bigquery.SchemaField]) -> None:
    """Append *df* to table_id with an explicit schema, creating the table if
    needed. New columns in *schema* are added to an existing table."""
    job_config = bigquery.LoadJobConfig(
        schema=schema,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        create_disposition=bigquery.CreateDisposition.CREATE_IF_NEEDED,
        schema_update_options=[bigquery.SchemaUpdateOption.ALLOW_FIELD_ADDITION],
    )
    get_bq_client().load_table_from_dataframe(df, table_id, job_config=job_config).result()
