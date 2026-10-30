"""
utils.py
────────
Helpers specific to the chunking sub-package (indexer.py, alloydb_client.py).
Package-wide helpers (normalize, safe_name, extract_jurisdiction, ...) live
in IP_pillar/utils.py.
"""

import re
import time
from datetime import datetime
from typing import List, Optional


def fmt_elapsed(start: float) -> str:
    """Seconds since *start* (a time.monotonic() value), e.g. '3.2s' — every
    indexing step logs how long it took so a stuck step is visible."""
    return f"{time.monotonic() - start:.1f}s"


def safe_collection_name(drug_name: str) -> str:
    """Strict alphanumeric-safe name for AlloyDB collection identifiers."""
    s = str(drug_name or "").strip()
    s = re.sub(r"[^a-zA-Z0-9_-]", "_", s)
    s = re.sub(r"[_-]{2,}", "_", s)
    return s.strip("_-")


def chunk_text(text: str, chunk_size: int, overlap: int) -> List[str]:
    """Split *text* into overlapping chunks, breaking on sentence/paragraph
    boundaries where possible."""
    if overlap >= chunk_size:
        raise ValueError(f"overlap ({overlap}) must be less than chunk_size ({chunk_size})")

    chunks, start = [], 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        if end < len(text):
            bp = max(chunk.rfind("."), chunk.rfind("\n"))
            if bp > chunk_size // 2:
                chunk = chunk[: bp + 1]
                end = start + bp + 1
        chunk = chunk.strip()
        if chunk:
            chunks.append(chunk)
        start = end - overlap
    return chunks


def clean_date(val) -> Optional[str]:
    """Normalise a date value returned by Gemini into YYYY-MM-DD (or None)."""
    if val is None or not isinstance(val, str):
        return None

    stripped = val.strip()
    if stripped.lower() in ("null", "none", "n/a", "unknown", ""):
        return None

    if re.match(r"^\d{4}-\d{2}-\d{2}$", stripped):
        return stripped

    for fmt in (
        "%B %d, %Y", "%b. %d, %Y", "%b %d, %Y",
        "%m/%d/%Y", "%d/%m/%Y",
        "%d.%m.%Y", "%Y.%m.%d", "%d-%m-%Y",
    ):
        try:
            return datetime.strptime(stripped, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    return None
