"""
dates.py
────────
Patent filing / grant date handling:

    extract_dates_from_pdf()   — read the dates off a patent PDF's cover page with Gemini
                                  (fallback chain: native PDF upload -> rendered-page vision -> OCR prompt)
    get_dates_from_alloydb()   — read the dates already stored for a patent in AlloyDB
                                  (sentinel record, then chunk 0)
    get_master_filing_date()   — the patent's filing_date in config.PATENT_MASTER_TABLE
    resolve_dates()            — dates for a patent PDF: filing_date from patent_master,
                                  read off the PDF only when patent_master has none
    get_dates()                — stored dates, with filing_date taken from patent_master
    get_dates_with_pdf_fallback() — get_dates(), reading the PDF if there is still no filing_date

patent_master is the source of truth for filing_date. The PDF is only read for
it when patent_master has no filing_date for that patent.

Used by indexer.py (resolve once at index time, reuse when already indexed) and
by blocking_analysis (get_dates).
"""

import asyncio
import hashlib
import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional

from google.cloud import bigquery
from google.genai import types

from .. import config
from .. import gcp_utils
from ..gemini_utils import gemini_generate_async
from ..utils import normalize_patent_number
from .utils import clean_date, fmt_elapsed

logger = logging.getLogger(__name__)

# normalised patent number -> filing_date (YYYY-MM-DD) or None when patent_master has none.
# Only successful lookups are cached, so a failed query is retried next time.
_master_filing_dates: Dict[str, Optional[str]] = {}

MASTER_FILING_DATE_QUERY = """
SELECT CAST(filing_date AS STRING) AS filing_date
FROM `{table}`
WHERE REGEXP_REPLACE(UPPER(CAST(patent_number AS STRING)), r'[^A-Z0-9]', '') = @patent_number
AND filing_date IS NOT NULL
LIMIT 1
"""


# ─────────────────────────────────────────────
# Prompts
# ─────────────────────────────────────────────

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
# Date extraction (Gemini)
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
        response = await gemini_generate_async(
            contents, config.GEMINI_TEXT_MODEL,
            response_mime_type="application/json", temperature=0, max_output_tokens=256,
        )
        raw = (response.text or "").strip()
        if raw:
            dates = _parse_gemini_date_response(raw)
            if dates.get("filing_date") or dates.get("grant_date"):
                return dates
    except Exception as e:
        logger.warning("[DATES] Structured call failed for %s: %s", filename, e)

    try:
        response = await gemini_generate_async(
            contents, config.GEMINI_TEXT_MODEL, temperature=0, max_output_tokens=256,
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
        if len(pdf_bytes) > config.DATE_EXTRACTION_PDF_MAX_BYTES:
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
                if len(pdf_bytes) > config.DATE_EXTRACTION_PDF_MAX_BYTES * 2:
                    pdf_bytes = None

        if pdf_bytes:
            dates = await _call_gemini_for_dates(
                contents=[types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"), DATE_EXTRACTION_PROMPT],
                filename=filename,
            )
            if dates.get("filing_date") or dates.get("grant_date"):
                logger.info("[DATE EXTRACTION] %s: resolved in %s (native PDF) -> Filed: %s | Granted: %s",
                            filename, fmt_elapsed(overall_start), dates["filing_date"], dates["grant_date"])
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
                        filename, fmt_elapsed(overall_start), dates["filing_date"], dates["grant_date"])
            return dates

    # Step 3: OCR-focused vision (last resort, reuses rendered pages if any)
    if not png_list:
        logger.warning("[DATE EXTRACTION] %s: no pages rendered — giving up after %s", filename, fmt_elapsed(overall_start))
        return {"filing_date": None, "grant_date": None}
    logger.info("[DATE EXTRACTION] %s: step 3/3 — OCR-focused vision prompt (last resort)...", filename)
    contents = [types.Part.from_bytes(data=p, mime_type="image/png") for p in png_list]
    contents.append(_OCR_DATE_PROMPT)
    dates = await _call_gemini_for_dates(contents=contents, filename=filename)
    logger.info("[DATE EXTRACTION] %s: finished in %s (OCR fallback) -> Filed: %s | Granted: %s",
                filename, fmt_elapsed(overall_start), dates.get("filing_date"), dates.get("grant_date"))
    return dates


# ─────────────────────────────────────────────
# Filing date from patent_master (BigQuery)
# ─────────────────────────────────────────────

def get_master_filing_date(filename: str) -> Optional[str]:
    """The filing_date (YYYY-MM-DD) patent_master holds for the patent behind
    *filename* (matched on the filename stem, ignoring punctuation and case).
    None if patent_master has no filing_date for it, or the query fails."""
    if not config.PATENT_MASTER_TABLE:
        return None
    patent_number = normalize_patent_number(Path(filename).stem)
    if not patent_number:
        return None
    if patent_number in _master_filing_dates:
        return _master_filing_dates[patent_number]

    try:
        query = MASTER_FILING_DATE_QUERY.format(table=config.PATENT_MASTER_TABLE)
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("patent_number", "STRING", patent_number)]
        )
        rows = list(gcp_utils.get_bq_client().query(query, job_config=job_config).result())
    except Exception as e:
        logger.warning("[DATES] patent_master filing_date lookup failed for %s: %s", filename, e)
        return None

    filing_date = clean_date(str(rows[0].filing_date)[:10]) if rows else None
    _master_filing_dates[patent_number] = filing_date
    return filing_date


async def resolve_dates(file_path: str, filename: str) -> Dict:
    """Filing/grant dates for a patent PDF. filing_date comes from patent_master;
    only when it has none is the filing date read off the PDF. grant_date is not
    in patent_master, so it still comes from the PDF."""
    loop = asyncio.get_running_loop()
    master_filing = await loop.run_in_executor(None, get_master_filing_date, filename)
    dates = await extract_dates_from_pdf(file_path, filename)
    if master_filing:
        if dates.get("filing_date") and dates["filing_date"] != master_filing:
            logger.info("[DATES] %s: using patent_master filing date %s (PDF read %s)",
                        filename, master_filing, dates["filing_date"])
        dates["filing_date"] = master_filing
    return dates


# ─────────────────────────────────────────────
# Stored dates (AlloyDB)
# ─────────────────────────────────────────────

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


def get_dates(collection, filename: str) -> dict:
    """Dates for a patent: the stored (AlloyDB) dates, with filing_date taken
    from patent_master whenever it has one. The stored filing date (read off
    the PDF at index time) is used only when patent_master has none."""
    dates = get_dates_from_alloydb(collection, filename)
    master_filing = get_master_filing_date(filename)
    if master_filing:
        dates["filing_date"] = master_filing
    return dates


async def get_dates_with_pdf_fallback(collection, filename: str, blob_name: Optional[str], drug_name: str) -> dict:
    """get_dates(), and when that still has no filing_date (neither patent_master
    nor the stored metadata has one) download the patent PDF from GCS and read
    the dates off its cover page. Only the missing dates are filled in; nothing
    is written back to AlloyDB."""
    loop = asyncio.get_running_loop()
    dates = await loop.run_in_executor(None, get_dates, collection, filename)
    if dates.get("filing_date") or not blob_name:
        return dates

    from .indexer import download_single_patent_pdf  # indexer imports this module
    logger.info("[DATES] %s: no filing date in patent_master or AlloyDB — reading it from the PDF", filename)
    pf = await loop.run_in_executor(None, download_single_patent_pdf, blob_name, filename, drug_name)
    if not pf:
        return dates
    try:
        extracted = await extract_dates_from_pdf(pf["path"], filename)
    finally:
        if pf.get("tmp_dir"):
            shutil.rmtree(pf["tmp_dir"], ignore_errors=True)
    for key in ("filing_date", "grant_date"):
        dates[key] = dates.get(key) or extracted.get(key)
    return dates
