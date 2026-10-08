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
    try:
        client = gcp_utils.get_gcs_client()
        bucket = client.bucket(config.GCS_BUCKET)
        blob = bucket.blob(blob_name)
        tmp_dir = Path(tempfile.mkdtemp(prefix=f"patents_{drug_name}_"))
        local_path = tmp_dir / filename
        blob.download_to_filename(str(local_path))
        logger.info("[GCS] Downloaded %s", filename)
        return {"filename": filename, "path": str(local_path), "tmp_dir": str(tmp_dir)}
    except Exception as e:
        logger.error("[GCS] Failed to download %s: %s", filename, e)
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

    for attempt in range(1, config.MAX_UPLOAD_RETRIES + 1):
        try:
            uploaded_file = await loop.run_in_executor(
                None,
                lambda: gemini_client.files.upload(file=file_path, config=dict(mime_type="application/pdf")),
            )

            max_wait, wait_time = 60, 0
            while uploaded_file.state == "PROCESSING" and wait_time < max_wait:
                await asyncio.sleep(2 + random.uniform(0, 0.5))
                _file_name = uploaded_file.name
                uploaded_file = await loop.run_in_executor(None, lambda n=_file_name: gemini_client.files.get(name=n))
                wait_time += 2

            if uploaded_file.state == "FAILED":
                logger.error("[UPLOAD] Gemini failed to process %s", path.name)
                return None

            logger.info("[UPLOAD] Ready: %s", path.name)
            return uploaded_file

        except Exception as e:
            if attempt == config.MAX_UPLOAD_RETRIES:
                logger.error("[UPLOAD] Upload failed for %s after %d attempts: %s", path.name, config.MAX_UPLOAD_RETRIES, e)
                return None
            backoff = (2 ** attempt) + random.uniform(0, 1)
            logger.warning("[UPLOAD] Attempt %d failed: %s — retrying in %.1fs", attempt, e, backoff)
            await asyncio.sleep(backoff)

    return None


async def extract_text_via_gemini(uploaded_file: object, filename: str) -> Optional[str]:
    """Extract full plain text from an uploaded PDF via Gemini."""
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
        logger.info("[TEXT EXTRACTION] Extracted %d characters from %s", len(text or ""), filename)
        return text
    except Exception as e:
        logger.error("[TEXT EXTRACTION] Failed for %s: %s", filename, e)
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
            logger.info("[PYMUPDF] %s: text layer too short — likely image-only PDF", filename)
            return None
        logger.info("[PYMUPDF] %s: extracted %d chars from text layer", filename, len(combined))
        return combined
    except Exception as e:
        logger.error("[PYMUPDF] Text extraction failed for %s: %s", filename, e)
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
    loop = asyncio.get_running_loop()
    try:
        png_list = await loop.run_in_executor(None, render_all_pages_as_pngs, file_path)
    except Exception as e:
        logger.error("[OCR] Page rendering failed for %s: %s", filename, e)
        return None

    if not png_list:
        return None

    all_text_parts = []
    batch_size = config.OCR_BATCH_SIZE
    for batch_start in range(0, len(png_list), batch_size):
        batch = png_list[batch_start:batch_start + batch_size]
        contents = [types.Part.from_bytes(data=p, mime_type="image/png") for p in batch]
        contents.append(_OCR_TEXT_EXTRACTION_PROMPT)
        try:
            response = await gemini_client.aio.models.generate_content(
                model=config.GEMINI_TEXT_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(temperature=0, max_output_tokens=65536),
            )
            batch_text = (response.text or "").strip()
            if batch_text:
                all_text_parts.append(batch_text)
        except Exception as e:
            logger.error("[OCR] Batch starting at page %d failed for %s: %s", batch_start, filename, e)

    if not all_text_parts:
        return None
    combined = "\n\n".join(all_text_parts)
    logger.info("[OCR] %s: extracted %d chars total via OCR", filename, len(combined))
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

    loop = asyncio.get_running_loop()

    # Step 1: native PDF upload
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
                logger.info("[DATE EXTRACTION] %s -> Filed: %s | Granted: %s (native PDF)",
                            filename, dates["filing_date"], dates["grant_date"])
                return dates
    except Exception as e:
        logger.warning("[DATE EXTRACTION] Native PDF upload failed for %s: %s", filename, e)

    # Step 2: cover-page vision
    png_list: List[bytes] = []
    try:
        png_list = await loop.run_in_executor(None, render_cover_pages_as_pngs, file_path)
    except Exception as e:
        logger.warning("[DATE EXTRACTION] Cover page render failed for %s: %s", filename, e)

    if png_list:
        contents = [types.Part.from_bytes(data=p, mime_type="image/png") for p in png_list]
        contents.append(DATE_EXTRACTION_PROMPT)
        dates = await _call_gemini_for_dates(contents=contents, filename=filename)
        if dates.get("filing_date") or dates.get("grant_date"):
            logger.info("[DATE EXTRACTION] %s -> Filed: %s | Granted: %s (vision)",
                        filename, dates["filing_date"], dates["grant_date"])
            return dates

    # Step 3: OCR-focused vision (last resort, reuses rendered pages if any)
    if not png_list:
        return {"filing_date": None, "grant_date": None}
    contents = [types.Part.from_bytes(data=p, mime_type="image/png") for p in png_list]
    contents.append(_OCR_DATE_PROMPT)
    dates = await _call_gemini_for_dates(contents=contents, filename=filename)
    logger.info("[DATE EXTRACTION] %s -> Filed: %s | Granted: %s (OCR fallback)",
                filename, dates.get("filing_date"), dates.get("grant_date"))
    return dates


# ─────────────────────────────────────────────
# Embeddings
# ─────────────────────────────────────────────

async def generate_embeddings(texts: List[str]) -> List[List[float]]:
    loop = asyncio.get_running_loop()
    try:
        embeddings = []
        for i in range(0, len(texts), 100):
            batch = texts[i:i + 100]
            for attempt in range(1, config.MAX_EMBED_RETRIES + 1):
                try:
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
                    break
                except Exception as e:
                    if attempt == config.MAX_EMBED_RETRIES:
                        raise RuntimeError(f"Embedding batch {i}-{i + len(batch)} failed: {e}") from e
                    backoff = (2 ** attempt) + random.uniform(0, 1)
                    logger.warning("[EMBEDDINGS] Attempt %d failed — retrying in %.1fs", attempt, backoff)
                    await asyncio.sleep(backoff)
        return embeddings
    except Exception as e:
        logger.error("[EMBEDDINGS] Generation failed: %s", e)
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

    chunks = chunk_text(text, config.CHUNK_SIZE_CHARS, config.OVERLAP_CHARS)
    if not chunks:
        return False

    embeddings = await generate_embeddings(chunks)
    if not embeddings:
        return False

    dates = dates or {}
    filing_date = clean_date(dates.get("filing_date")) or ""
    grant_date = clean_date(dates.get("grant_date")) or ""

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

    logger.info("[INDEXING] %s — %d chunks stored | Filed: %s | Granted: %s",
                filename, len(chunks), filing_date or "unknown", grant_date or "unknown")
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
    ref: dict, drug_name: str, collection, reindex: bool, semaphore: asyncio.Semaphore, completed: set = None,
) -> dict:
    filename = ref["filename"]

    if completed is not None and filename in completed:
        return {"filename": filename, "path": None, "tmp_dir": None}

    async with semaphore:
        if not reindex and sentinel_exists(collection, filename):
            existing_dates = get_dates_from_alloydb(collection, filename)
            if existing_dates.get("filing_date"):
                if completed is not None:
                    _mark_completed(drug_name, filename, completed)
                return {"filename": filename, "path": None, "tmp_dir": None}

        if not reindex:
            source_col = find_in_any_collection(filename)
            if source_col:
                await copy_from_collection(filename, source_col, collection, drug_name)
                if completed is not None:
                    _mark_completed(drug_name, filename, completed)
                return {"filename": filename, "path": None, "tmp_dir": None}

        loop = asyncio.get_running_loop()
        pf = await loop.run_in_executor(None, download_single_patent_pdf, ref["blob_name"], filename, drug_name)
        if not pf:
            logger.warning("[WARNING] Could not download %s", filename)
            return {"filename": filename, "path": None, "tmp_dir": None}

        try:
            uploaded_file = await upload_pdf_to_gemini(pf["path"])
            if not uploaded_file:
                return pf

            text, dates = await asyncio.gather(
                extract_text_via_gemini(uploaded_file, filename),
                extract_dates_from_pdf(pf["path"], filename),
            )

            if not text:
                text = extract_text_via_pymupdf(pf["path"], filename)
            if not text:
                text = await extract_text_via_ocr(pf["path"], filename)

            if text:
                await index_text(drug_name, filename, text, collection, dates=dates)
                if completed is not None:
                    _mark_completed(drug_name, filename, completed)
            else:
                logger.warning("[WARNING] No text extracted from %s — all methods failed", filename)

            await cleanup_uploaded_file(uploaded_file)
            await asyncio.sleep(0.5 + random.uniform(0, 0.5))

        except Exception as e:
            logger.error("[ERROR] Processing failed for %s: %s", filename, e)

        finally:
            if pf.get("tmp_dir"):
                shutil.rmtree(pf["tmp_dir"], ignore_errors=True)
                pf["tmp_dir"] = None

        return pf


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

    pdf_refs = patent_filter(drug_name)
    if not pdf_refs:
        logger.warning("[INDEXER] No PDFs found for '%s'", drug_name)
        return []

    collection = get_or_create_collection(drug_name)
    if reindex:
        alloydb_client().delete_collection(name=collection.name)
        collection = get_or_create_collection(drug_name)

    completed = _load_progress(drug_name) if not reindex else set()
    already_done = sum(1 for r in pdf_refs if r["filename"] in completed)

    logger.info("[INDEXER] '%s': %d patent(s), %d running in parallel (%d already done)",
                drug_name, len(pdf_refs), max_concurrency, already_done)

    semaphore = asyncio.Semaphore(max_concurrency)
    tasks = [
        _process_single_patent(ref, drug_name, collection, reindex, semaphore, completed=completed)
        for ref in pdf_refs
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

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
