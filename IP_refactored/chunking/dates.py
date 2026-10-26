"""
dates.py
────────
Patent filing / grant date handling:

    extract_dates_from_pdf()   — read the dates off a patent PDF's cover page with Gemini
                                  (fallback chain: native PDF upload -> rendered-page vision -> OCR prompt)
    get_dates_from_alloydb()   — read the dates already stored for a patent in AlloyDB
                                  (sentinel record, then chunk 0)

Used by indexer.py (extract once at index time, reuse when already indexed) and
by blocking_analysis (reads the stored dates).
"""

import asyncio
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Dict, List

from google.genai import types

from .. import config
from .. import gcp_utils
from .utils import clean_date, fmt_elapsed

logger = logging.getLogger(__name__)

gemini_client = gcp_utils.get_gemini_client()


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
