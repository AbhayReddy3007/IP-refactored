"""
utils.py
────────
Shared helper functions used by more than one module inside chunking/
(patent_filter.py, indexer.py, alloydb_client.py).

Nothing here talks to Google Cloud — pure, dependency-free logic only.
GCP operations belong in IP_refactored/gcp_utils.py.
"""

import re
from datetime import datetime
from typing import List, Optional


# ─────────────────────────────────────────────
# Name normalisation — used by patent_filter.py (folder matching) and
# indexer.py (AlloyDB collection naming).
# ─────────────────────────────────────────────

def normalize(name: str) -> str:
    """Lowercase, strip, collapse spaces/hyphens/underscores for fuzzy matching."""
    return re.sub(r"[\s\-_]+", "", str(name or "").lower().strip())


def safe_name(drug_name: str, lowercase: bool = False) -> str:
    """GCS / filesystem-safe drug name.

    Preserves the canonical drug name as closely as possible (including
    the '+' used in combination drugs, e.g. "Cagrilintide+Semaglutide").
    Only characters that are genuinely unsafe in GCS paths / filesystems
    are replaced: / \\ < > : " | ? * and null bytes.
    """
    s = str(drug_name or "").strip()
    s = re.sub(r'[/\\<>:"|?*\x00]', "_", s)
    s = re.sub(r"_+", "_", s)
    s = s.strip("_")
    if lowercase:
        s = s.lower()
    return s


def safe_collection_name(drug_name: str) -> str:
    """Strict alphanumeric-safe name for AlloyDB collection identifiers.

    Replaces every character that is NOT alphanumeric, underscore, or
    hyphen with '_'.
    """
    s = str(drug_name or "").strip()
    s = re.sub(r"[^a-zA-Z0-9_-]", "_", s)
    s = re.sub(r"[_-]{2,}", "_", s)
    s = s.strip("_-")
    return s


# ─────────────────────────────────────────────
# Text chunking — used by indexer.py
# ─────────────────────────────────────────────

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


# ─────────────────────────────────────────────
# Date cleaning — used by indexer.py (filing / grant date extraction)
# ─────────────────────────────────────────────

def clean_date(val) -> Optional[str]:
    """Normalise a date value returned by Gemini into YYYY-MM-DD (or None).

    Handles common quirks: string "null"/"None"/"N/A", already-ISO dates,
    and a handful of common non-ISO formats salvaged via strptime.
    """
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


def has_valid_dates(meta: dict) -> bool:
    """True if a metadata dict has at least one non-empty filing/grant date."""
    filing = meta.get("filing_date", "")
    grant = meta.get("grant_date", "")
    return bool(filing and filing not in ("", "null", "None")) or \
        bool(grant and grant not in ("", "null", "None"))


def extract_jurisdiction(patent_number_hint: str) -> str:
    """Best-effort 2-letter jurisdiction code from a patent number / filename stem."""
    match = re.search(r"\b([A-Z]{2})\d", patent_number_hint.upper())
    if match:
        return match.group(1)
    fallback = re.match(r"^([A-Z]{2})", patent_number_hint.upper())
    return fallback.group(1) if fallback else "UN"
