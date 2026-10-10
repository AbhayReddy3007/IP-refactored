"""
indexer.py
──────────
Fetches patent PDFs, extracts text + filing/grant dates, chunks the text,
generates embeddings, and stores everything in AlloyDB.

Pipeline per patent:
    1. Skip if already indexed (sentinel exists in AlloyDB)
    2. Cross-collection dedup — copy from another drug's collection if found
    3. Download PDF from GCS
    4. Upload to Gemini Files API, extract full text
       (fallback chain: Gemini → PyMuPDF text layer → Gemini Vision OCR)
    5. Extract filing/grant dates from the cover page
       (fallback chain: native PDF upload → rendered-page vision)
    6. Chunk text, generate embeddings, store chunks + sentinel in AlloyDB

Patents are processed concurrently per drug, bounded by
config.INDEXER_CONCURRENCY. Progress is checkpointed to GCS (via
gcp_utils) so a crashed run can resume without redoing completed patents.

Usage:
    from IP_refactored.chunking.indexer import indexer

    result = await indexer("Semaglutide")
"""

import asyncio
import hashlib
import json
import logging
import random
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

from google.genai import types

from .. import config
from .. import gcp_utils
from .alloydb_client import alloydb_client
from .patent_filter import patent_filter
from .utils import chunk_text, clean_date, has_valid_dates, safe_collection_name

logger = logging.getLogger(__name__)

gemini_client = gcp_utils.get_gemini_client()


# ─────────────────────────────────────────────
# Timing helper — every step below logs how long it took, so a stuck step
# is visible instead of the log just going quiet.
# ─────────────────────────────────────────────

def _fmt_elapsed(start: float) -> str:
    return f"{time.monotonic() - start:.1f}s"

# ─────────────────────────────────────────────
# Prompts
# ─────────────────────────────────────────────

_TEXT_EXTRACTION_PROMPT = (
    "Extract ALL text from this patent document exactly as it appears. "
    "Include every section: cover page, patent number, all dates, "
    "inventors, assignee, claims, description, abstract. "
    "Return only plain text with no commentary or formatting."
)

_OCR_TEXT_EXTRACTION_PROMPT = (
    "This is a scanned patent document (image-only PDF with no embedded text). "
    "Please carefully OCR every page and extract ALL text exactly as it appears. "
    "Include every section: cover page, patent number, all dates, inventors, "
    "assignee, claims, description, abstract. "
    "Return only plain text with no commentary or formatting."
)

DATE_EXTRACTION_PROMPT = """You are a patent document parser. This image is the cover page of a patent or patent application (it may be a scanned image with no embedded text — use OCR to read it).

Extract ONLY these two dates:
1. Filing date: look for ANY of these labels (in any language):
   - "(22) Filed:" (US patents)
   - "(22) International Filing Date:" (WO/PCT applications)
   - "Filing Date:", "Date Filed:", "PCT Filed:"
   - Any field labelled with INID code (22)
2. Grant/Publication date: look for ANY of these labels (in any language):
   - "(45) Date of Patent:" (US granted patents)
   - "(43) International Publication Date:" (WO/PCT applications)
   - "Grant Date:", "Published:", "Publication Date:"
   - Any field labelled with INID code (43) or (45)

Rules:
- Return ONLY the dates for THIS patent/application (not cited prior art references)
- For WO/PCT, EP, BR, MX, EA, JP applications: use the publication date as grant_date (or null if not found)
- If a date is missing or unclear -> use null
- Convert any date format to YYYY-MM-DD format

Return ONLY valid JSON with no markdown, no explanation:
{
  "filing_date": "YYYY-MM-DD or null",
  "grant_date":  "YYYY-MM-DD or null"
}
"""

_OCR_DATE_PROMPT = """This is a scanned image of a patent cover page. Please carefully read ALL text in the image using OCR.

Then extract ONLY these two dates:
1. Filing date — look for "(22) Filed:" or "(22) International Filing Date:"
2. Grant/Publication date — look for "(45) Date of Patent:" or "(43) International Publication Date:"

Convert any dates you find to YYYY-MM-DD format.

Return ONLY valid JSON:
{"filing_date": "YYYY-MM-DD or null", "grant_date": "YYYY-MM-DD or null"}
"""


# ─────────────────────────────────────────────
# Progress tracking (GCS-backed, via gcp_utils) — survives container restarts
# ─────────────────────────────────────────────

def _progress_filename(drug_name: str) -> str:
    from .utils import safe_name
    return f"progress_{safe_name(drug_name)}.json"


def _load_progress(drug_name: str) -> set:
    try:
        data = gcp_utils.read_json(config.GCS_INDEXER_PROGRESS_SUBFOLDER, _progress_filename(drug_name))
        if data is not None:
            completed = set(data.get("completed", []))
            logger.info("[RESUME] Found progress for '%s': %d patent(s) already done", drug_name, len(completed))
            return completed
    except Exception:
        pass
    return set()


def _save_progress(drug_name: str, completed: set):
    try:
        gcp_utils.write_json(
            config.GCS_INDEXER_PROGRESS_SUBFOLDER, _progress_filename(drug_name), {"completed": sorted(completed)},
        )
    except Exception as e:
        logger.warning("[RESUME] Failed to save progress for '%s': %s", drug_name, e)


def _mark_completed(drug_name: str, filename: str, completed: set):
    completed.add(filename)
    _save_progress(drug_name, completed)


def _clear_progress(drug_name: str):
    try:
        if gcp_utils.delete_blob(config.GCS_INDEXER_PROGRESS_SUBFOLDER, _progress_filename(drug_name)):
            logger.info("[RESUME] Cleared progress for '%s' — batch complete", drug_name)
    except Exception:
        pass


# ─────────────────────────────────────────────
# GCS download
# ─────────────────────────────────────────────

def download_single_patent_pdf(blob_name: str, filename: str, drug_name: str) -> Optional[dict]:
    if not config.GCS_BUCKET:
        logger.error("[GCS] GCS_BUCKET not set — cannot download")
        return None
    start = time.monotonic()
    logger.info("[GCS] %s: downloading from gs://%s/%s ...", filename, config.GCS_BUCKET, blob_name)
    try:
        client = gcp_utils.get_gcs_client()
        bucket = client.bucket(config.GCS_BUCKET)
        blob = bucket.blob(blob_name)
        tmp_dir = Path(tempfile.mkdtemp(prefix=f"patents_{drug_name}_"))
        local_path = tmp_dir / filename
        blob.download_to_filename(str(local_path))
        size_mb = local_path.stat().st_size / (1024 * 1024)
        logger.info("[GCS] %s: downloaded %.1f MB in %s", filename, size_mb, _fmt_elapsed(start))
        return {"filename": filename, "path": str(local_path), "tmp_dir": str(tmp_dir)}
    except Exception as e:
        logger.error("[GCS] %s: download failed after %s: %s", filename, _fmt_elapsed(start), e)
        return None


# ─────────────────────────────────────────────
# Gemini file upload + text extraction
# ─────────────────────────────────────────────

async def upload_pdf_to_gemini(file_path: str) -> Optional[object]:
    path = Path(file_path)
    file_size_mb = path.stat().st_size / (1024 * 1024)
    if file_size_mb > config.GEMINI_FILE_SIZE_LIMIT_MB:
        logger.error("[UPLOAD] %s is %.1f MB — exceeds %d MB limit.", path.name, file_size_mb, config.GEMINI_FILE_SIZE_LIMIT_MB)
        return None

    loop = asyncio.get_running_loop()
    overall_start = time.monotonic()

    for attempt in range(1, config.MAX_UPLOAD_RETRIES + 1):
        try:
            logger.info("[UPLOAD] %s: uploading to Gemini Files API (%.1f MB, attempt %d/%d)...",
                        path.name, file_size_mb, attempt, config.MAX_UPLOAD_RETRIES)
            upload_start = time.monotonic()
            uploaded_file = await loop.run_in_executor(
                None,
                lambda: gemini_client.files.upload(file=file_path, config=dict(mime_type="application/pdf")),
            )
            logger.info("[UPLOAD] %s: upload call returned in %s (state=%s) — waiting for Gemini to finish processing...",
                        path.name, _fmt_elapsed(upload_start), uploaded_file.state)

            max_wait, wait_time = 60, 0
            while uploaded_file.state == "PROCESSING" and wait_time < max_wait:
                await asyncio.sleep(2 + random.uniform(0, 0.5))
                _file_name = uploaded_file.name
                uploaded_file = await loop.run_in_executor(None, lambda n=_file_name: gemini_client.files.get(name=n))
                wait_time += 2
                logger.info("[UPLOAD] %s: still PROCESSING after %ds (polling every ~2s, timeout at %ds)...",
                            path.name, wait_time, max_wait)

            if uploaded_file.state == "FAILED":
                logger.error("[UPLOAD] %s: Gemini reported FAILED after %s", path.name, _fmt_elapsed(overall_start))
                return None

            if uploaded_file.state == "PROCESSING":
                logger.warning("[UPLOAD] %s: still PROCESSING after %ds timeout — proceeding anyway (may fail downstream)",
                                path.name, max_wait)

            logger.info("[UPLOAD] %s: ready (state=%s) in %s total", path.name, uploaded_file.state, _fmt_elapsed(overall_start))
            return uploaded_file

        except Exception as e:
            if attempt == config.MAX_UPLOAD_RETRIES:
                logger.error("[UPLOAD] %s: failed after %d attempt(s) / %s: %s",
                              path.name, config.MAX_UPLOAD_RETRIES, _fmt_elapsed(overall_start), e)
                return None
            backoff = (2 ** attempt) + random.uniform(0, 1)
            logger.warning("[UPLOAD] %s: attempt %d failed: %s — retrying in %.1fs", path.name, attempt, e, backoff)
            await asyncio.sleep(backoff)

    return None


async def extract_text_via_gemini(uploaded_file: object, filename: str) -> Optional[str]:
    """Extract full plain text from an uploaded PDF via Gemini."""
    start = time.monotonic()
    logger.info("[TEXT EXTRACTION] %s: requesting full-text extraction from Gemini (model=%s)...",
                filename, config.GEMINI_TEXT_MODEL)
    try:
        response = await gemini_client.aio.models.generate_content(
            model=config.GEMINI_TEXT_MODEL,
            contents=[uploaded_file, _TEXT_EXTRACTION_PROMPT],
            config=types.GenerateContentConfig(temperature=0, max_output_tokens=65536),
        )
        try:
            finish_reason = response.candidates[0].finish_reason
            if str(finish_reason) in ("MAX_TOKENS", "2"):
                logger.warning("[TEXT EXTRACTION] %s hit MAX_TOKENS — document may be partially indexed.", filename)
        except (IndexError, AttributeError):
            pass

        text = response.text
        logger.info("[TEXT EXTRACTION] %s: extracted %d character(s) in %s", filename, len(text or ""), _fmt_elapsed(start))
        return text
    except Exception as e:
        logger.error("[TEXT EXTRACTION] %s: failed after %s: %s", filename, _fmt_elapsed(start), e)
        return None


async def cleanup_uploaded_file(uploaded_file: object):
    loop = asyncio.get_running_loop()
    _file_name = uploaded_file.name
    try:
        await loop.run_in_executor(None, lambda: gemini_client.files.delete(name=_file_name))
    except Exception as e:
        logger.warning("[UPLOAD] Could not clean up %s: %s", _file_name, e)


# ─────────────────────────────────────────────
# Fallback text extraction (image-only / scanned PDFs)
# ─────────────────────────────────────────────

def extract_text_via_pymupdf(file_path: str, filename: str) -> Optional[str]:
    logger.info("[PYMUPDF] %s: Gemini text extraction returned nothing — trying local PDF text layer...", filename)
    start = time.monotonic()
    try:
        import fitz
    except ImportError:
        logger.warning("[PYMUPDF] pymupdf not available — cannot extract text for %s", filename)
        return None

    try:
        doc = fitz.open(file_path)
        all_text = [doc[p].get_text("text").strip() for p in range(len(doc)) if doc[p].get_text("text").strip()]
        doc.close()
        combined = "\n\n".join(all_text)
        if len(combined.strip()) < 100:
            logger.info("[PYMUPDF] %s: text layer too short (%s) — likely image-only PDF, will try OCR", filename, _fmt_elapsed(start))
            return None
        logger.info("[PYMUPDF] %s: extracted %d chars from text layer in %s", filename, len(combined), _fmt_elapsed(start))
        return combined
    except Exception as e:
        logger.error("[PYMUPDF] %s: text extraction failed after %s: %s", filename, _fmt_elapsed(start), e)
        return None


def render_all_pages_as_pngs(file_path: str, dpi: int = None, max_pages: int = None) -> List[bytes]:
    dpi = dpi or config.OCR_PAGE_DPI
    max_pages = max_pages or config.OCR_MAX_PAGES
    import fitz
    doc = fitz.open(file_path)
    pages_to_render = min(max_pages, len(doc))
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pngs = [doc[i].get_pixmap(matrix=mat, alpha=False).tobytes("png") for i in range(pages_to_render)]
    doc.close()
    return pngs


async def extract_text_via_ocr(file_path: str, filename: str) -> Optional[str]:
    """OCR fallback for image-only PDFs — renders pages as PNGs and sends
    them to Gemini Vision in batches."""
    logger.info("[OCR] %s: no usable text layer found — falling back to Gemini Vision OCR...", filename)
    overall_start = time.monotonic()
    loop = asyncio.get_running_loop()
    try:
        png_list = await loop.run_in_executor(None, render_all_pages_as_pngs, file_path)
    except Exception as e:
        logger.error("[OCR] %s: page rendering failed after %s: %s", filename, _fmt_elapsed(overall_start), e)
        return None

    if not png_list:
        logger.warning("[OCR] %s: page rendering produced no pages", filename)
        return None

    batch_size = config.OCR_BATCH_SIZE
    n_batches = (len(png_list) + batch_size - 1) // batch_size
    logger.info("[OCR] %s: rendered %d page(s) in %s — sending to Gemini Vision in %d batch(es) of %d",
                filename, len(png_list), _fmt_elapsed(overall_start), n_batches, batch_size)

    all_text_parts = []
    for batch_num, batch_start in enumerate(range(0, len(png_list), batch_size), start=1):
        batch = png_list[batch_start:batch_start + batch_size]
        contents = [types.Part.from_bytes(data=p, mime_type="image/png") for p in batch]
        contents.append(_OCR_TEXT_EXTRACTION_PROMPT)
        batch_start_time = time.monotonic()
        logger.info("[OCR] %s: batch %d/%d (pages %d-%d) — requesting OCR from Gemini...",
                    filename, batch_num, n_batches, batch_start, batch_start + len(batch) - 1)
        try:
            response = await gemini_client.aio.models.generate_content(
                model=config.GEMINI_TEXT_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(temperature=0, max_output_tokens=65536),
            )
            batch_text = (response.text or "").strip()
            if batch_text:
                all_text_parts.append(batch_text)
            logger.info("[OCR] %s: batch %d/%d done in %s (%d chars)",
                        filename, batch_num, n_batches, _fmt_elapsed(batch_start_time), len(batch_text))
        except Exception as e:
            logger.error("[OCR] %s: batch %d/%d failed after %s: %s",
                          filename, batch_num, n_batches, _fmt_elapsed(batch_start_time), e)

    if not all_text_parts:
        logger.warning("[OCR] %s: no text recovered from any batch after %s", filename, _fmt_elapsed(overall_start))
        return None
    combined = "\n\n".join(all_text_parts)
    logger.info("[OCR] %s: extracted %d chars total via OCR in %s", filename, len(combined), _fmt_elapsed(overall_start))
    return combined


# ─────────────────────────────────────────────
# Date extraction
# ─────────────────────────────────────────────

def render_cover_pages_as_pngs(file_path: str, dpi: int = None, max_pages: int = 2) -> List[bytes]:
    dpi = dpi or config.COVER_PAGE_DPI
    import fitz
    doc = fitz.open(file_path)
    pages_to_render = min(max_pages, len(doc))
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pngs = [doc[i].get_pixmap(matrix=mat, alpha=False).tobytes("png") for i in range(pages_to_render)]
    doc.close()
    return pngs


def _parse_gemini_date_response(raw: str) -> Dict:
    if not raw or not raw.strip():
        return {"filing_date": None, "grant_date": None}
    cleaned = raw.strip()
    if "```" in cleaned:
        cleaned = re.sub(r"```(?:json)?", "", cleaned).replace("```", "").strip()
    match = re.search(r"\{[^{}]*\}", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)
    try:
        dates = json.loads(cleaned)
        return {"filing_date": clean_date(dates.get("filing_date")), "grant_date": clean_date(dates.get("grant_date"))}
    except (json.JSONDecodeError, AttributeError):
        return {"filing_date": None, "grant_date": None}


async def _call_gemini_for_dates(contents: list, filename: str) -> Dict:
    try:
        response = await gemini_client.aio.models.generate_content(
            model=config.GEMINI_TEXT_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0, max_output_tokens=256),
        )
        raw = (response.text or "").strip()
        if raw:
            dates = _parse_gemini_date_response(raw)
            if dates.get("filing_date") or dates.get("grant_date"):
                return dates
    except Exception as e:
        logger.warning("[DATES] Structured call failed for %s: %s", filename, e)

    try:
        response = await gemini_client.aio.models.generate_content(
            model=config.GEMINI_TEXT_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(temperature=0, max_output_tokens=256),
        )
        raw = (response.text or "").strip()
        if raw:
            return _parse_gemini_date_response(raw)
    except Exception as e:
        logger.warning("[DATES] Free-form call also failed for %s: %s", filename, e)

    return {"filing_date": None, "grant_date": None}


async def extract_dates_from_pdf(file_path: str, filename: str) -> Dict:
    """
    Extract filing/grant dates from a patent PDF.

    Fallback chain:
      1. Native PDF upload to Gemini (trimmed to first 2 pages if large).
      2. Render cover pages as PNGs and send to Gemini Vision.
      3. OCR-focused vision prompt on the same rendered pages (last resort).
    """
    if not file_path or not Path(file_path).exists():
        return {"filing_date": None, "grant_date": None}

    overall_start = time.monotonic()
    loop = asyncio.get_running_loop()

    # Step 1: native PDF upload
    logger.info("[DATE EXTRACTION] %s: step 1/3 — native PDF upload to Gemini...", filename)
    try:
        pdf_bytes = Path(file_path).read_bytes()
        max_bytes = 2 * 1024 * 1024
        if len(pdf_bytes) > max_bytes:
            try:
                import fitz
                doc = fitz.open(file_path)
                new_doc = fitz.open()
                for i in range(min(2, len(doc))):
                    new_doc.insert_pdf(doc, from_page=i, to_page=i)
                pdf_bytes = new_doc.tobytes()
                new_doc.close()
                doc.close()
            except ImportError:
                if len(pdf_bytes) > 5 * 1024 * 1024:
                    pdf_bytes = None

        if pdf_bytes:
            dates = await _call_gemini_for_dates(
                contents=[types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"), DATE_EXTRACTION_PROMPT],
                filename=filename,
            )
            if dates.get("filing_date") or dates.get("grant_date"):
                logger.info("[DATE EXTRACTION] %s: resolved in %s (native PDF) -> Filed: %s | Granted: %s",
                            filename, _fmt_elapsed(overall_start), dates["filing_date"], dates["grant_date"])
                return dates
    except Exception as e:
        logger.warning("[DATE EXTRACTION] %s: native PDF upload failed: %s", filename, e)

    # Step 2: cover-page vision
    logger.info("[DATE EXTRACTION] %s: step 2/3 — rendering cover page(s) for Gemini Vision...", filename)
    png_list: List[bytes] = []
    try:
        png_list = await loop.run_in_executor(None, render_cover_pages_as_pngs, file_path)
    except Exception as e:
        logger.warning("[DATE EXTRACTION] %s: cover page render failed: %s", filename, e)

    if png_list:
        contents = [types.Part.from_bytes(data=p, mime_type="image/png") for p in png_list]
        contents.append(DATE_EXTRACTION_PROMPT)
        dates = await _call_gemini_for_dates(contents=contents, filename=filename)
        if dates.get("filing_date") or dates.get("grant_date"):
            logger.info("[DATE EXTRACTION] %s: resolved in %s (vision) -> Filed: %s | Granted: %s",
                        filename, _fmt_elapsed(overall_start), dates["filing_date"], dates["grant_date"])
            return dates

    # Step 3: OCR-focused vision (last resort, reuses rendered pages if any)
    if not png_list:
        logger.warning("[DATE EXTRACTION] %s: no pages rendered — giving up after %s", filename, _fmt_elapsed(overall_start))
        return {"filing_date": None, "grant_date": None}
    logger.info("[DATE EXTRACTION] %s: step 3/3 — OCR-focused vision prompt (last resort)...", filename)
    contents = [types.Part.from_bytes(data=p, mime_type="image/png") for p in png_list]
    contents.append(_OCR_DATE_PROMPT)
    dates = await _call_gemini_for_dates(contents=contents, filename=filename)
    logger.info("[DATE EXTRACTION] %s: finished in %s (OCR fallback) -> Filed: %s | Granted: %s",
                filename, _fmt_elapsed(overall_start), dates.get("filing_date"), dates.get("grant_date"))
    return dates


# ─────────────────────────────────────────────
# Embeddings
# ─────────────────────────────────────────────

async def generate_embeddings(texts: List[str], filename: str = "") -> List[List[float]]:
    loop = asyncio.get_running_loop()
    overall_start = time.monotonic()
    n_batches = (len(texts) + 99) // 100
    logger.info("[EMBEDDINGS] %s: embedding %d chunk(s) in %d batch(es) of up to 100...",
                filename or "?", len(texts), n_batches)
    try:
        embeddings = []
        for batch_num, i in enumerate(range(0, len(texts), 100), start=1):
            batch = texts[i:i + 100]
            batch_start = time.monotonic()
            for attempt in range(1, config.MAX_EMBED_RETRIES + 1):
                try:
                    logger.info("[EMBEDDINGS] %s: batch %d/%d (%d chunk(s), attempt %d/%d)...",
                                filename or "?", batch_num, n_batches, len(batch), attempt, config.MAX_EMBED_RETRIES)
                    result = await loop.run_in_executor(
                        None,
                        lambda b=batch: gemini_client.models.embed_content(
                            model=config.GEMINI_EMBEDDING_MODEL,
                            contents=b,
                            config=types.EmbedContentConfig(task_type="SEMANTIC_SIMILARITY"),
                        ),
                    )
                    for emb in result.embeddings:
                        embeddings.append(emb.values)
                    logger.info("[EMBEDDINGS] %s: batch %d/%d done in %s",
                                filename or "?", batch_num, n_batches, _fmt_elapsed(batch_start))
                    break
                except Exception as e:
                    if attempt == config.MAX_EMBED_RETRIES:
                        raise RuntimeError(f"Embedding batch {i}-{i + len(batch)} failed: {e}") from e
                    backoff = (2 ** attempt) + random.uniform(0, 1)
                    logger.warning("[EMBEDDINGS] %s: batch %d/%d attempt %d failed — retrying in %.1fs",
                                   filename or "?", batch_num, n_batches, attempt, backoff)
                    await asyncio.sleep(backoff)
        logger.info("[EMBEDDINGS] %s: all %d batch(es) done in %s", filename or "?", n_batches, _fmt_elapsed(overall_start))
        return embeddings
    except Exception as e:
        logger.error("[EMBEDDINGS] %s: generation failed after %s: %s", filename or "?", _fmt_elapsed(overall_start), e)
        return []


# ─────────────────────────────────────────────
# AlloyDB helpers
# ─────────────────────────────────────────────

_chroma_write_lock = asyncio.Lock()


def sanitize_collection_name(drug_name: str) -> str:
    safe = safe_collection_name(drug_name)
    safe = safe.ljust(3, "x")[:55]
    safe = re.sub(r"[^a-zA-Z0-9]+$", "", safe)
    return f"patents_{safe}"


def get_or_create_collection(drug_name: str):
    name = sanitize_collection_name(drug_name)
    client = alloydb_client()
    try:
        col = client.get_collection(name=name)
        logger.info("[ALLOYDB] Using existing: %s", name)
    except Exception:
        col = client.create_collection(name=name, metadata={"description": f"Patent embeddings for {drug_name}"})
        logger.info("[ALLOYDB] Created new: %s", name)
    return col


def sentinel_exists(collection, filename: str) -> bool:
    try:
        sid = hashlib.md5(filename.encode()).hexdigest() + "_complete"
        result = collection.get(ids=[sid])
        return bool(result["ids"])
    except Exception:
        return False


def get_dates_from_alloydb(collection, filename: str) -> dict:
    """Read filing/grant dates for a patent from AlloyDB (sentinel, then chunk_0)."""
    file_hash = hashlib.md5(filename.encode()).hexdigest()

    try:
        result = collection.get(ids=[file_hash + "_complete"], include=["metadatas"])
        if result["metadatas"]:
            meta = result["metadatas"][0]
            filing, grant = meta.get("filing_date") or None, meta.get("grant_date") or None
            if filing or grant:
                return {"filing_date": filing, "grant_date": grant}
    except Exception as e:
        logger.warning("[DATES] Sentinel read failed for %s: %s", filename, e)

    try:
        result = collection.get(ids=[f"{file_hash}_chunk_0"], include=["metadatas"])
        if result["metadatas"]:
            meta = result["metadatas"][0]
            filing, grant = meta.get("filing_date") or None, meta.get("grant_date") or None
            if filing or grant:
                return {"filing_date": filing, "grant_date": grant}
    except Exception as e:
        logger.warning("[DATES] Chunk-0 read failed for %s: %s", filename, e)

    return {"filing_date": None, "grant_date": None}


async def index_text(drug_name: str, filename: str, text: str, collection, dates: dict = None) -> bool:
    """Chunk and index a patent into AlloyDB. Dates are stored in every
    chunk's metadata and the sentinel record."""
    start = time.monotonic()
    file_hash = hashlib.md5(filename.encode()).hexdigest()
    sentinel_id = f"{file_hash}_complete"

    try:
        if collection.get(ids=[sentinel_id])["ids"]:
            logger.info("[INDEXING] Already fully indexed: %s", filename)
            return True
    except Exception:
        pass

    try:
        stale = collection.get(where={"filename": filename}, include=["ids"])
        if stale["ids"]:
            logger.info("[INDEXING] Incomplete index for %s — clearing and re-indexing", filename)
            async with _chroma_write_lock:
                collection.delete(where={"filename": filename})
    except Exception:
        pass

    logger.info("[INDEXING] %s: chunking %d character(s) (chunk_size=%d, overlap=%d)...",
                filename, len(text), config.CHUNK_SIZE_CHARS, config.OVERLAP_CHARS)
    chunks = chunk_text(text, config.CHUNK_SIZE_CHARS, config.OVERLAP_CHARS)
    if not chunks:
        logger.warning("[INDEXING] %s: chunk_text produced 0 chunks — nothing to index", filename)
        return False
    logger.info("[INDEXING] %s: produced %d chunk(s) in %s", filename, len(chunks), _fmt_elapsed(start))

    embeddings = await generate_embeddings(chunks, filename=filename)
    if not embeddings:
        logger.warning("[INDEXING] %s: embedding generation returned nothing — aborting index for this patent", filename)
        return False

    dates = dates or {}
    filing_date = clean_date(dates.get("filing_date")) or ""
    grant_date = clean_date(dates.get("grant_date")) or ""

    logger.info("[INDEXING] %s: writing %d chunk(s) + sentinel to AlloyDB...", filename, len(chunks))
    async with _chroma_write_lock:
        collection.add(
            documents=chunks,
            embeddings=embeddings,
            metadatas=[
                {
                    "filename": filename, "drug": drug_name, "chunk_index": i,
                    "total_chunks": len(chunks), "filing_date": filing_date, "grant_date": grant_date,
                }
                for i in range(len(chunks))
            ],
            ids=[f"{file_hash}_chunk_{i}" for i in range(len(chunks))],
        )
        collection.add(
            documents=["__index_complete__"],
            embeddings=[[0.0] * len(embeddings[0])],
            metadatas=[{
                "filename": filename, "drug": drug_name, "chunk_index": -1,
                "total_chunks": len(chunks), "filing_date": filing_date, "grant_date": grant_date,
            }],
            ids=[sentinel_id],
        )

    logger.info("[INDEXING] %s — %d chunks stored in %s total | Filed: %s | Granted: %s",
                filename, len(chunks), _fmt_elapsed(start), filing_date or "unknown", grant_date or "unknown")
    return True


def find_in_any_collection(filename: str) -> Optional[str]:
    sid = hashlib.md5(filename.encode()).hexdigest() + "_complete"
    client = alloydb_client()
    for col in client.list_collections():
        if not col.name.startswith("patents_"):
            continue
        try:
            existing = client.get_collection(col.name)
            if existing.get(ids=[sid])["ids"]:
                return col.name
        except Exception:
            continue
    return None


async def copy_from_collection(filename: str, source_name: str, target_collection, target_drug: str) -> bool:
    """Copy all chunks + sentinel from source to target collection. Dates
    are embedded in chunk metadata, so they transfer automatically."""
    try:
        source = alloydb_client().get_collection(source_name)
        file_hash = hashlib.md5(filename.encode()).hexdigest()

        chunks = source.get(
            where={"$and": [{"filename": {"$eq": filename}}, {"chunk_index": {"$gte": 0}}]},
            include=["documents", "metadatas", "embeddings"],
        )
        if chunks["ids"]:
            async with _chroma_write_lock:
                target_collection.add(
                    documents=chunks["documents"], embeddings=chunks["embeddings"],
                    metadatas=[{**m, "drug": target_drug} for m in chunks["metadatas"]], ids=chunks["ids"],
                )
            logger.info("[COPY] %d chunks -> '%s'", len(chunks["ids"]), target_collection.name)

        sentinel_id = f"{file_hash}_complete"
        sentinel = source.get(ids=[sentinel_id], include=["documents", "metadatas", "embeddings"])
        if sentinel["ids"]:
            async with _chroma_write_lock:
                target_collection.add(
                    documents=sentinel["documents"], embeddings=sentinel["embeddings"],
                    metadatas=[{**sentinel["metadatas"][0], "drug": target_drug}], ids=[sentinel_id],
                )
            logger.info("[COPY] Sentinel copied for '%s'", filename)
        return True
    except Exception as e:
        logger.error("[COPY] Failed to copy '%s' from '%s': %s", filename, source_name, e)
        return False


# ─────────────────────────────────────────────
# Per-patent processing task
# ─────────────────────────────────────────────

async def _process_single_patent(
    ref: dict, drug_name: str, collection, reindex: bool, semaphore: asyncio.Semaphore,
    completed: set = None, progress: dict = None, position: str = "",
) -> dict:
    filename = ref["filename"]

    def _mark_step(step: str):
        """Record the current step + its start time in the shared progress
        dict, so the heartbeat can report exactly where this patent is."""
        if progress is not None:
            progress["in_progress"][filename] = {
                "step": step, "since": time.monotonic(), "position": position,
            }

    def _finish(outcome: str):
        if progress is not None:
            progress["in_progress"].pop(filename, None)
            progress["done"] += 1
            logger.info("[PATENT] %s %s: %s (overall: %d/%d done)",
                        position, filename, outcome, progress["done"], progress["total"])

    if completed is not None and filename in completed:
        if progress is not None:
            progress["done"] += 1
        return {"filename": filename, "path": None, "tmp_dir": None}

    patent_start = time.monotonic()
    logger.info("[PATENT] %s %s: queued, waiting for a free slot...", position, filename)

    async with semaphore:
        logger.info("[PATENT] %s %s: starting (waited %s for a slot)", position, filename, _fmt_elapsed(patent_start))
        step_start = time.monotonic()

        _mark_step("checking cache (AlloyDB sentinel)")
        if not reindex and sentinel_exists(collection, filename):
            existing_dates = get_dates_from_alloydb(collection, filename)
            if existing_dates.get("filing_date"):
                if completed is not None:
                    _mark_completed(drug_name, filename, completed)
                _finish(f"already indexed — skipped (checked in {_fmt_elapsed(step_start)})")
                return {"filename": filename, "path": None, "tmp_dir": None}

        _mark_step("checking other drugs' collections for a cached copy")
        if not reindex:
            source_col = find_in_any_collection(filename)
            if source_col:
                logger.info("[PATENT] %s %s: found cached copy in '%s' — copying instead of re-processing",
                            position, filename, source_col)
                await copy_from_collection(filename, source_col, collection, drug_name)
                if completed is not None:
                    _mark_completed(drug_name, filename, completed)
                _finish(f"copied from '{source_col}' in {_fmt_elapsed(step_start)}")
                return {"filename": filename, "path": None, "tmp_dir": None}

        _mark_step("downloading PDF from GCS")
        loop = asyncio.get_running_loop()
        pf = await loop.run_in_executor(None, download_single_patent_pdf, ref["blob_name"], filename, drug_name)
        if not pf:
            logger.warning("[WARNING] Could not download %s", filename)
            _finish(f"download failed after {_fmt_elapsed(step_start)}")
            return {"filename": filename, "path": None, "tmp_dir": None}

        try:
            _mark_step("uploading to Gemini Files API")
            uploaded_file = await upload_pdf_to_gemini(pf["path"])
            if not uploaded_file:
                _finish(f"Gemini upload failed after {_fmt_elapsed(step_start)}")
                return pf

            _mark_step("extracting text + filing/grant dates (parallel)")
            logger.info("[PATENT] %s %s: extracting text and dates in parallel...", position, filename)
            text, dates = await asyncio.gather(
                extract_text_via_gemini(uploaded_file, filename),
                extract_dates_from_pdf(pf["path"], filename),
            )

            if not text:
                _mark_step("text extraction fallback: PyMuPDF text layer")
                text = extract_text_via_pymupdf(pf["path"], filename)
            if not text:
                _mark_step("text extraction fallback: Gemini Vision OCR")
                text = await extract_text_via_ocr(pf["path"], filename)

            if text:
                _mark_step("chunking + embedding + writing to AlloyDB")
                await index_text(drug_name, filename, text, collection, dates=dates)
                if completed is not None:
                    _mark_completed(drug_name, filename, completed)
                outcome = f"fully indexed in {_fmt_elapsed(step_start)}"
            else:
                logger.warning("[WARNING] No text extracted from %s — all methods failed", filename)
                outcome = f"no text extracted (all methods failed) after {_fmt_elapsed(step_start)}"

            # Cleanup happens BEFORE _finish() so the heartbeat doesn't show
            # this patent as both "done" and still "in flight" for the
            # cleanup step.
            _mark_step("cleaning up Gemini upload")
            await cleanup_uploaded_file(uploaded_file)
            await asyncio.sleep(0.5 + random.uniform(0, 0.5))
            _finish(outcome)

        except Exception as e:
            logger.error("[ERROR] %s: processing failed after %s: %s", filename, _fmt_elapsed(step_start), e)
            _finish(f"errored after {_fmt_elapsed(step_start)}: {e}")

        finally:
            if pf.get("tmp_dir"):
                shutil.rmtree(pf["tmp_dir"], ignore_errors=True)
                pf["tmp_dir"] = None
            if progress is not None:
                progress["in_progress"].pop(filename, None)

        return pf


# ─────────────────────────────────────────────
# Heartbeat — periodic snapshot so a long run never goes silent
# ─────────────────────────────────────────────

async def _heartbeat(drug_name: str, progress: dict, run_start: float):
    """Logs a progress snapshot every config.INDEXER_HEARTBEAT_SECONDS:
    how many patents are done, how many are in flight, and — for each
    in-flight patent — which step it's on and how long it's been there.
    Runs until cancelled by the caller once indexing finishes."""
    interval = config.INDEXER_HEARTBEAT_SECONDS
    try:
        while True:
            await asyncio.sleep(interval)
            done, total = progress["done"], progress["total"]
            in_progress = dict(progress["in_progress"])
            logger.info("[HEARTBEAT] '%s': %d/%d done | %d in flight | elapsed %s",
                        drug_name, done, total, len(in_progress), _fmt_elapsed(run_start))
            for filename, info in sorted(in_progress.items(), key=lambda kv: kv[1]["since"]):
                waited = time.monotonic() - info["since"]
                flag = "  <-- stuck here a while" if waited > interval * 2 else ""
                logger.info("[HEARTBEAT]   %s %s: '%s' for %.0fs%s",
                            info.get("position", ""), filename, info["step"], waited, flag)
    except asyncio.CancelledError:
        pass


# ─────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────

async def indexer(drug_name: str, reindex: bool = False, max_concurrency: Optional[int] = None) -> List[dict]:
    """
    Full fetch + chunk + embed + store pipeline for one drug.

    Looks up the drug's patent PDFs via patent_filter(), then indexes each
    one into AlloyDB (bounded concurrency, crash-resilient resume).

    Args:
        drug_name:       Drug name (must match a GCS patent folder).
        reindex:         If True, force re-indexing even if already indexed.
        max_concurrency: Max patents processed in parallel (defaults to
                         config.INDEXER_CONCURRENCY).

    Returns:
        List of {"filename": str, "path": str | None, "tmp_dir": str | None}
    """
    max_concurrency = max_concurrency or config.INDEXER_CONCURRENCY
    run_start = time.monotonic()

    pdf_refs = patent_filter(drug_name)
    if not pdf_refs:
        logger.warning("[INDEXER] No PDFs found for '%s'", drug_name)
        return []

    logger.info("[INDEXER] '%s': getting/creating AlloyDB collection...", drug_name)
    collection = get_or_create_collection(drug_name)
    if reindex:
        alloydb_client().delete_collection(name=collection.name)
        collection = get_or_create_collection(drug_name)

    completed = _load_progress(drug_name) if not reindex else set()
    already_done = sum(1 for r in pdf_refs if r["filename"] in completed)

    logger.info("[INDEXER] '%s': %d patent(s) total, %d running in parallel (%d already done, "
                "%d to process), heartbeat every %ds",
                drug_name, len(pdf_refs), max_concurrency, already_done,
                len(pdf_refs) - already_done, config.INDEXER_HEARTBEAT_SECONDS)

    progress = {"done": 0, "total": len(pdf_refs), "in_progress": {}}
    heartbeat_task = asyncio.create_task(_heartbeat(drug_name, progress, run_start))

    semaphore = asyncio.Semaphore(max_concurrency)
    tasks = [
        _process_single_patent(
            ref, drug_name, collection, reindex, semaphore, completed=completed,
            progress=progress, position=f"[{i + 1}/{len(pdf_refs)}]",
        )
        for i, ref in enumerate(pdf_refs)
    ]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

    downloaded_files: List[dict] = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            filename = pdf_refs[i]["filename"]
            logger.error("[ERROR] Task for %s raised: %s", filename, result)
            downloaded_files.append({"filename": filename, "path": None, "tmp_dir": None})
        else:
            downloaded_files.append(result)

    all_filenames = {r["filename"] for r in pdf_refs}
    if all_filenames.issubset(completed):
        _clear_progress(drug_name)

    logger.info("[INDEXER] '%s': run finished in %s — %d/%d patent(s) completed",
                drug_name, _fmt_elapsed(run_start), progress["done"], progress["total"])

    return downloaded_files


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    drug = sys.argv[1] if len(sys.argv) > 1 else None
    if not drug:
        print("Usage: python -m IP_refactored.chunking.indexer <drug_name> [--reindex]")
        sys.exit(1)
    force_reindex = "--reindex" in sys.argv
    out = asyncio.run(indexer(drug, reindex=force_reindex))
    print(json.dumps(out, indent=2, default=str))