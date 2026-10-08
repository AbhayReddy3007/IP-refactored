"""
step3_scientific_barrier.py
───────────────────────────
STEP 3 — Scientific barrier analysis.

Determines whether the claimed feature solves a REAL technical barrier
(stability, bioavailability, safety) rather than being optional/incremental.

  Marketed drugs → FDA Label/Review PDFs + EMA EPARs + Europe PMC + open web
  Clinical drugs → PubMed + Europe PMC + open web + completed trial rows

  is_technical_barrier = True  → continue to Step 4
  is_technical_barrier = False → NON-BLOCKING

Also hosts the EMA EPAR helpers and `_cap_evidence`, which Steps 4 and 5 reuse.
"""

import asyncio
import io
import random
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup
from google.genai import types

from .. import config
from .. import gcp_utils
from .step1_claim_classification import _call_gemini_json
from .step2_claim_elements import _format_rows_for_prompt

gemini_client = gcp_utils.get_gemini_client()


# ── EMA EPAR helpers (inlined from ema_epar_extractor to avoid import conflicts) ──

_EMA_BASE_URL    = "https://www.ema.europa.eu"
_EMA_EXCEL_PAGE  = "https://www.ema.europa.eu/en/medicines/download-medicine-data"
_EMA_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}
_EMPTY_EPAR = {
    "overview_pdf"                    : None,
    "public_summary_pdf"              : None,
    "risk_management_plan_summary_pdf": None,
    "product_information_pdf"         : None,
}
_ema_df_cache: Optional[pd.DataFrame] = None


def _load_ema_excel() -> Optional[pd.DataFrame]:
    global _ema_df_cache
    if _ema_df_cache is not None:
        return _ema_df_cache
    try:
        page_resp = requests.get(_EMA_EXCEL_PAGE, headers=_EMA_HTTP_HEADERS, timeout=60)
        if page_resp.status_code == 404:
            return None
        page_resp.raise_for_status()
    except Exception as e:
        print(f"[EMA EXCEL] Failed to fetch page: {e}")
        return None

    soup   = BeautifulSoup(page_resp.text, "html.parser")
    anchor = None
    for a in soup.find_all("a"):
        if "download medicines data table" in (a.get_text(strip=True) or "").lower():
            anchor = a
            break
    if anchor is None:
        anchor = soup.find("a", href=re.compile(
            r"/en/documents/report/medicines-output-medicines-report_en\.xlsx$", re.I
        ))
    if not anchor or not anchor.get("href"):
        print("[EMA EXCEL] Could not find download link")
        return None

    try:
        xls_resp = requests.get(
            urljoin(_EMA_BASE_URL, anchor["href"]),
            headers=_EMA_HTTP_HEADERS, timeout=120,
        )
        xls_resp.raise_for_status()
    except Exception as e:
        print(f"[EMA EXCEL] Download failed: {e}")
        return None

    df = pd.read_excel(io.BytesIO(xls_resp.content), engine="openpyxl")
    df = df.dropna(how="all").reset_index(drop=True)
    header_row_idx = None
    for i, row in df.iterrows():
        if "Name of medicine" in row.values:
            header_row_idx = i
            break
    if header_row_idx is None:
        print("[EMA EXCEL] Could not find header row")
        return None
    df.columns = df.iloc[header_row_idx]
    df         = df[header_row_idx + 1:].reset_index(drop=True)
    df.columns = [str(c).strip() for c in df.columns]
    _ema_df_cache = df
    print(f"[EMA EXCEL] Loaded and cached ({len(df)} medicines)")
    return df


def _resolve_all_ema_brands(generic_name: str) -> List[str]:
    """Resolve INN/generic name to all EMA brand names via INN column + FDA fallback."""
    df = _load_ema_excel()
    seen: set = set()
    brands: List[str] = []

    if df is not None:
        inn_col = next(
            (c for c in df.columns if any(x in str(c).lower()
             for x in ["international non-proprietary", "inn", "common name"])),
            None
        )
        if inn_col:
            target = generic_name.lower().strip()
            mask   = df[inn_col].astype(str).str.lower().str.strip() == target
            if not mask.any():
                mask = df[inn_col].astype(str).str.lower().str.contains(target, na=False, regex=False)
            for brand in df.loc[mask, "Name of medicine"].astype(str).str.strip():
                if brand.lower() not in seen and brand.lower() not in ("nan", ""):
                    seen.add(brand.lower())
                    brands.append(brand)

    # FDA fallback
    try:
        r = requests.get(
            _OPEN_FDA_BASE,
            params={"search": f'products.active_ingredients.name:"{generic_name}"', "limit": 5},
            timeout=20, headers=_EMA_HTTP_HEADERS,
        )
        if r.status_code == 200:
            fda_brands = [
                p.get("brand_name", "").strip().lower()
                for res in r.json().get("results", []) or []
                for p in res.get("products", []) or []
                if p.get("brand_name")
            ]
            if df is not None and "Name of medicine" in df.columns:
                ema_names = df["Name of medicine"].astype(str).str.lower().str.strip()
                for fb in fda_brands:
                    for matched in df.loc[ema_names == fb, "Name of medicine"].astype(str).str.strip():
                        if matched.lower() not in seen and matched.lower() not in ("nan", ""):
                            seen.add(matched.lower())
                            brands.append(matched)
    except Exception as e:
        print(f"[EMA BRANDS] FDA fallback failed: {e}")

    if not brands:
        print(f"[EMA BRANDS] No brands found for '{generic_name}' — trying name directly")
        brands = [generic_name]
    else:
        print(f"[EMA BRANDS] '{generic_name}' → {brands}")
    return brands


def _get_ema_epar_documents(drug_name: str) -> dict:
    """Fetch EPAR PDF links for a given EMA brand name."""
    df = _load_ema_excel()
    if df is None or "Name of medicine" not in df.columns or "Medicine URL" not in df.columns:
        return _EMPTY_EPAR.copy()

    result = df.loc[
        df["Name of medicine"].astype(str).str.lower().str.strip() == drug_name.lower().strip(),
        "Medicine URL"
    ]
    if result.empty:
        return _EMPTY_EPAR.copy()

    drug_page_url = str(result.iloc[0]).strip()
    try:
        resp = requests.get(drug_page_url, headers=_EMA_HTTP_HEADERS, timeout=60)
        resp.raise_for_status()
    except Exception as e:
        print(f"[EMA EPAR] Failed to fetch page for '{drug_name}': {e}")
        return _EMPTY_EPAR.copy()

    soup     = BeautifulSoup(resp.text, "html.parser")
    patterns = {
        "overview_pdf"                    : re.compile(r"/en/documents/.*epar-medicine-overview.*_en\.pdf",   re.I),
        "public_summary_pdf"              : re.compile(r"/en/documents/.*epar-summary.*_en\.pdf",             re.I),
        "risk_management_plan_summary_pdf": re.compile(r"/en/documents/.*epar-risk-management.*_en\.pdf",     re.I),
        "product_information_pdf"         : re.compile(r"/en/documents/.*epar-product-information.*_en\.pdf", re.I),
    }
    found = _EMPTY_EPAR.copy()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/en/documents/" not in href or not href.lower().endswith(".pdf"):
            continue
        for key, pattern in patterns.items():
            if found[key] is None and pattern.search(href):
                found[key] = urljoin(_EMA_BASE_URL, href)
        if all(found.values()):
            break
    return found


# ─────────────────────────────────────────────
# STEP 3 — Scientific barrier analysis
# ─────────────────────────────────────────────

# Maximum characters of evidence passed into any single prompt.
# Each source is trimmed proportionally so all sources contribute equally.
# This prevents Gemini output truncation regardless of how much evidence was gathered.
_MAX_EVIDENCE_CHARS = config.MAX_EVIDENCE_CHARS


def _cap_evidence(evidence_block: str, max_chars: int = _MAX_EVIDENCE_CHARS) -> str:
    """
    Caps the evidence block to max_chars characters.
    Splits on source section boundaries (lines starting with "[") so each
    source is trimmed proportionally rather than cutting mid-sentence.
    Appends a note if truncation occurred.
    """
    if len(evidence_block) <= max_chars:
        return evidence_block

    # Split into named sections — each starts with a "[Source]" header line
    sections = re.split(r'(?=^\[)', evidence_block, flags=re.MULTILINE)
    if not sections:
        return evidence_block[:max_chars] + "\n\n[Evidence truncated to fit context window]"

    per_section = max(max_chars // max(len(sections), 1), 500)
    capped = []
    total  = 0
    for section in sections:
        if not section.strip():
            continue
        allowed = min(per_section, max_chars - total)
        if allowed <= 0:
            break
        chunk = section[:allowed]
        if len(section) > allowed:
            # Try to cut at a sentence boundary
            cut = max(chunk.rfind(". "), chunk.rfind("\n"))
            if cut > allowed // 2:
                chunk = chunk[:cut + 1]
        capped.append(chunk.strip())
        total += len(chunk)

    result = "\n\n".join(capped)
    if len(evidence_block) > len(result):
        result += "\n\n[Evidence capped to fit context window — full details available in logs]"
    print(f"[EVIDENCE CAP] {len(evidence_block):,} chars -> {len(result):,} chars "
          f"({len(sections)} section(s))")
    return result

#
# Determines whether the claimed feature solves a REAL technical barrier
# (stability, bioavailability, safety) vs. being optional/incremental.
#
# Marketed drugs  → FDA Medical/CMC Review PDFs + EMA Assessment Report
# Clinical drugs  → PubMed abstracts (6 keyword combos) + Gemini journal search
#                   + completed trial rows from Excel
#
# is_technical_barrier = True  → continue to Step 4
# is_technical_barrier = False → NON-BLOCKING

_PUBMED_ESEARCH  = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
_PUBMED_EFETCH   = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
_PUBMED_EMAIL    = config.PUBMED_EMAIL
_PUBMED_API_KEY  = config.NCBI_API_KEY or None  # free at ncbi.nlm.nih.gov/account
# With API key: 10 req/s. Without: 3 req/s. Use conservative delay either way.
_PUBMED_DELAY    = 0.15 if _PUBMED_API_KEY else 0.4   # seconds between requests
_PUBMED_MAX_RETRIES = 3
_HTTP_HEADERS    = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"}
_OPEN_FDA_BASE   = "https://api.fda.gov/drug/drugsfda.json"

_KEYWORD_TEMPLATES = [
    "{molecule} formulation stability",
    "{molecule} pharmacokinetics",
    "{molecule} bioavailability",
    "{molecule} phase clinical trial",
    "{molecule} degradation",
    "{molecule} delivery system",
]


# ── PubMed helpers ────────────────────────────────────────────────────────────

def _pubmed_search(query: str, max_results: int = 5) -> List[str]:
    """Search PubMed and return a list of PMIDs. Retries on 429."""
    params = {
        "db":      "pubmed",
        "term":    query,
        "retmax":  max_results,
        "retmode": "json",
        "email":   _PUBMED_EMAIL,
    }
    if _PUBMED_API_KEY:
        params["api_key"] = _PUBMED_API_KEY

    for attempt in range(1, _PUBMED_MAX_RETRIES + 1):
        try:
            r = requests.get(
                _PUBMED_ESEARCH,
                params=params,
                timeout=15,
                headers=_HTTP_HEADERS,
            )
            if r.status_code == 429:
                wait = (2 ** attempt) + random.uniform(0, 1)
                print(f"[PUBMED] 429 on '{query}' (attempt {attempt}) — waiting {wait:.1f}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            ids = r.json().get("esearchresult", {}).get("idlist", [])
            print(f"[PUBMED] '{query}' → {len(ids)} result(s)")
            return ids
        except Exception as e:
            if attempt == _PUBMED_MAX_RETRIES:
                print(f"[PUBMED] Search failed for '{query}' after {_PUBMED_MAX_RETRIES} attempts: {e}")
                return []
            time.sleep(1)
    return []


def _pubmed_fetch_abstracts(pmids: List[str]) -> str:
    """Fetch abstracts for a list of PMIDs. Returns concatenated plain text."""
    if not pmids:
        return ""

    params = {
        "db":      "pubmed",
        "id":      ",".join(pmids),
        "rettype": "abstract",
        "retmode": "text",
        "email":   _PUBMED_EMAIL,
    }
    if _PUBMED_API_KEY:
        params["api_key"] = _PUBMED_API_KEY

    for attempt in range(1, _PUBMED_MAX_RETRIES + 1):
        try:
            r = requests.get(
                _PUBMED_EFETCH,
                params=params,
                timeout=20,
                headers=_HTTP_HEADERS,
            )
            if r.status_code == 429:
                wait = (2 ** attempt) + random.uniform(0, 1)
                print(f"[PUBMED] 429 on fetch (attempt {attempt}) — waiting {wait:.1f}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.text.strip()
        except Exception as e:
            if attempt == _PUBMED_MAX_RETRIES:
                print(f"[PUBMED] Fetch failed for PMIDs {pmids}: {e}")
                return ""
            time.sleep(1)
    return ""


async def _gather_pubmed_evidence(drug_name: str) -> str:
    """
    Run all 6 keyword searches against PubMed SEQUENTIALLY with rate-limit delays.
    Parallel requests cause 429s — PubMed allows max 3 req/s without API key.
    Returns a single string of deduplicated abstracts.
    """
    loop    = asyncio.get_event_loop()
    queries = [t.format(molecule=drug_name) for t in _KEYWORD_TEMPLATES]

    seen_pmids: set = set()
    all_pmids:  List[str] = []

    for query in queries:
        pmids = await loop.run_in_executor(None, _pubmed_search, query, 5)
        for pmid in pmids:
            if pmid not in seen_pmids:
                seen_pmids.add(pmid)
                all_pmids.append(pmid)
        # Respect PubMed rate limit between searches
        await asyncio.sleep(_PUBMED_DELAY)

    if not all_pmids:
        return ""

    print(f"[PUBMED] Fetching {len(all_pmids)} unique abstract(s) for '{drug_name}'")
    await asyncio.sleep(_PUBMED_DELAY)
    abstracts = await loop.run_in_executor(None, _pubmed_fetch_abstracts, all_pmids[:20])
    return abstracts


# ── Gemini open evidence search (Google Search grounding) ────────────────────

_SEARCH_ANGLES = [
    {
        "label": "Regulatory",
        "prompt": (
            "Search for regulatory and scientific evidence about whether this specific "
            "feature of '{drug_name}' is technically necessary for the drug to work:\n\n"
            "Patent claim: {claim_category} — {claim_reason}\n\n"
            "Look across ALL available sources: FDA reviews, EMA assessment reports, "
            "WHO reports, ICH guidelines, regulatory agency publications, health authority "
            "submissions, drug dossiers, post-market surveillance reports, and any other "
            "regulatory or government source that addresses whether this feature was required "
            "for stability, bioavailability, safety, or regulatory approval.\n\n"
            "Do NOT restrict yourself to any specific source. Find the most authoritative "
            "evidence available anywhere on the web.\n\n"
            "Summarise the key findings in 250 words. Cite the source name and year."
        ),
    },
    {
        "label": "Scientific Literature",
        "prompt": (
            "Search for peer-reviewed scientific evidence about whether this specific "
            "feature of '{drug_name}' solves a real technical barrier:\n\n"
            "Patent claim: {claim_category} — {claim_reason}\n\n"
            "Look across ALL scientific sources: journals, conference proceedings, "
            "preprints (bioRxiv, medRxiv), dissertations, technical reports, patent "
            "literature, pharmacopoeia monographs, and any scientific publication that "
            "addresses whether this formulation or delivery feature is scientifically "
            "necessary vs optional.\n\n"
            "Focus on: stability data, bioavailability studies, pharmacokinetics, "
            "degradation mechanisms, solubility challenges, absorption barriers.\n\n"
            "Do NOT restrict yourself to specific journals. Find the most relevant "
            "scientific evidence anywhere.\n\n"
            "Summarise the key findings in 250 words. Cite source names and years."
        ),
    },
    {
        "label": "Clinical & Industry",
        "prompt": (
            "Search for clinical and industry evidence about '{drug_name}' related to "
            "this patent claim:\n\n"
            "Patent claim: {claim_category} — {claim_reason}\n\n"
            "Look across ALL relevant sources: ClinicalTrials.gov records, clinical study "
            "reports, pharmaceutical company technical documents, industry white papers, "
            "patent filings by competitors (which may acknowledge the technical problem), "
            "drug product information sheets, pharmacist references, hospital formulary "
            "documents, and any clinical or industry source that discusses whether this "
            "feature was technically essential.\n\n"
            "Do NOT restrict yourself to any specific database. Find the most relevant "
            "evidence available.\n\n"
            "Summarise the key findings in 250 words. Cite source names and years."
        ),
    },
]


async def _gather_gemini_evidence_angle(
    drug_name:      str,
    claim_category: str,
    claim_reason:   str,
    angle:          dict,
) -> str:
    """Run a single search angle via Gemini with Google Search grounding."""
    prompt = angle["prompt"].format(
        drug_name      = drug_name,
        claim_category = claim_category,
        claim_reason   = claim_reason,
    )
    label = angle["label"]

    try:
        response = await gemini_client.aio.models.generate_content(
            model    = config.GEMINI_TEXT_MODEL,
            contents = prompt,
            config   = types.GenerateContentConfig(
                tools       = [types.Tool(google_search=types.GoogleSearch())],
                temperature = 0.1,
            ),
        )
        text = (response.text or "").strip()
        if text:
            print(f"[GEMINI SEARCH] {label}: {len(text)} chars for '{drug_name}'")
            return f"[{label} Evidence — Open Web Search]\n{text}"
        return ""
    except Exception as e:
        print(f"[GEMINI SEARCH] {label} failed for '{drug_name}': {e}")
        return ""


async def _gather_all_gemini_evidence(
    drug_name:      str,
    claim_category: str,
    claim_reason:   str,
) -> str:
    """
    Run all 3 search angles in parallel via Gemini Google Search grounding.
    Each angle searches the open web with no source restrictions.
    Returns combined evidence string.
    """
    results = await asyncio.gather(
        *[
            _gather_gemini_evidence_angle(drug_name, claim_category, claim_reason, angle)
            for angle in _SEARCH_ANGLES
        ],
        return_exceptions=True,
    )

    parts = []
    for angle, result in zip(_SEARCH_ANGLES, results):
        if isinstance(result, Exception):
            print(f"[GEMINI SEARCH] {angle['label']} error: {result}")
        elif result and str(result).strip():
            parts.append(str(result).strip())

    return "\n\n".join(parts)


# ── FDA Medical/CMC Review fetcher ────────────────────────────────────────────

def _get_fda_review_urls(drug_name: str) -> List[Tuple[str, str]]:
    """
    Query Drugs@FDA and return the single latest Label URL and single latest
    Review URL — same logic as fda_label_extractor.get_brand_nda_details_with_dates.

    Tracks the latest doc by date across ALL NDA applications.
    Returns at most 2 entries: (Label, url) and/or (Review, url).
    """
    try:
        r = requests.get(
            _OPEN_FDA_BASE,
            params={
                "search": f'products.active_ingredients.name:"{drug_name}"',
                "limit":  10,
            },
            timeout=20,
            headers=_HTTP_HEADERS,
        )
        if r.status_code == 404:
            return []
        r.raise_for_status()
        results = r.json().get("results", []) or []
    except Exception as e:
        print(f"[FDA REVIEW] API request failed for '{drug_name}': {e}")
        return []

    # Track single latest Label and single latest Review across all NDAs
    latest_docs: Dict[str, Dict] = {
        "Label":  {"date": "0", "url": None},
        "Review": {"date": "0", "url": None},
    }

    for app in results:
        for submission in app.get("submissions", []) or []:
            for doc in submission.get("application_docs", []) or []:
                doc_type = doc.get("type", "")
                doc_date = doc.get("date", "") or "0"
                doc_url  = doc.get("url", "")

                # Map all review variants to single "Review" key
                if doc_type in ("Review", "Medical Review", "Chemistry Review",
                                "Pharmacology Review", "Clinical Pharmacology Review"):
                    key = "Review"
                elif doc_type == "Label":
                    key = "Label"
                else:
                    continue

                if doc_url and doc_date > latest_docs[key]["date"]:
                    latest_docs[key] = {"date": doc_date, "url": doc_url}
                    print(f"[FDA REVIEW] Latest {key} so far: {doc_date} | {doc_url[:80]}")

    result = []
    for key in ("Label", "Review"):
        url = latest_docs[key]["url"]
        if url:
            print(f"[FDA REVIEW] Using latest {key}: {url[:80]}")
            result.append((key, url))

    if not result:
        print(f"[FDA REVIEW] No Label or Review PDFs found for '{drug_name}'")

    return result


async def _analyse_fda_review_pdf(pdf_url: str, doc_type: str, drug_name: str, claim_reason: str) -> str:
    """Send an FDA review PDF to Gemini and extract scientific necessity evidence."""
    prompt = (
        f"You are analysing an FDA {doc_type} for '{drug_name}'.\n\n"
        f"The patent in question claims: {claim_reason}\n\n"
        f"From this regulatory review document, extract ONLY content relevant to:\n"
        f"1. Whether the specific formulation feature, delivery system, or process was "
        f"considered NECESSARY by the FDA for approval (stability, bioavailability, safety)\n"
        f"2. Any statements indicating the feature solved a technical problem\n"
        f"3. Any CMC (Chemistry, Manufacturing, Controls) requirements related to the claim\n\n"
        f"If the document does not address technical necessity of the claimed feature, "
        f"state 'Not addressed in this review'.\n\n"
        f"Be concise — 200 words maximum."
    )

    try:
        response = await gemini_client.aio.models.generate_content(
            model    = config.GEMINI_TEXT_MODEL,
            contents = [
                types.Part.from_uri(file_uri=pdf_url, mime_type="application/pdf"),
                prompt,
            ],
            config = types.GenerateContentConfig(
                temperature       = 0.1,
                max_output_tokens = 1024,
            ),
        )
        text = response.text or ""
        print(f"[FDA REVIEW] Extracted {len(text)} chars from {doc_type}")
        return f"[FDA {doc_type}]\n{text.strip()}"
    except Exception as e:
        print(f"[FDA REVIEW] Gemini analysis failed for {doc_type}: {e}")
        return ""


async def _gather_fda_evidence(drug_name: str, claim_reason: str) -> str:
    """Fetch and analyse the latest FDA Label + Review PDFs for marketed drugs."""
    loop        = asyncio.get_event_loop()
    review_urls = await loop.run_in_executor(None, _get_fda_review_urls, drug_name)

    if not review_urls:
        print(f"[FDA REVIEW] No review PDFs found for '{drug_name}'")
        return ""

    results = await asyncio.gather(
        *[_analyse_fda_review_pdf(url, doc_type, drug_name, claim_reason)
          for doc_type, url in review_urls],
        return_exceptions=True,
    )

    parts = []
    for r in results:
        if isinstance(r, Exception):
            print(f"[FDA REVIEW] Error: {r}")
        elif r:
            parts.append(r)

    return "\n\n".join(parts)


# ── EMA Assessment Report fetcher ────────────────────────────────────────────

_EMA_EPAR_PREFERENCE = [
    "product_information_pdf",
    "public_summary_pdf",
    "overview_pdf",
    "risk_management_plan_summary_pdf",
]

_EMA_STEP3_PROMPT = """You are analysing an EMA EPAR document for '{drug_name}'.

The patent in question claims: {claim_reason}

From this EMA regulatory document, extract ONLY content relevant to:
1. Whether the specific formulation feature, delivery system, or process was
   considered NECESSARY by EMA for marketing authorisation
2. Any scientific assessment of technical necessity (stability, bioavailability, safety)
3. Any pharmaceutical development (CMC) findings related to the claim
4. Statements about why specific excipients, devices, or processes were required

If the document does not address technical necessity of the claimed feature,
state 'Not addressed in this EMA document'.

Be concise — 200 words maximum."""


async def _analyse_ema_pdf(pdf_url: str, doc_type: str, drug_name: str, claim_reason: str) -> str:
    """Send a single EMA EPAR PDF to Gemini and extract Step 3 evidence."""
    prompt = _EMA_STEP3_PROMPT.format(
        drug_name    = drug_name,
        claim_reason = claim_reason,
    )
    try:
        response = await gemini_client.aio.models.generate_content(
            model    = config.GEMINI_TEXT_MODEL,
            contents = [
                types.Part.from_uri(file_uri=pdf_url, mime_type="application/pdf"),
                prompt,
            ],
            config = types.GenerateContentConfig(
                temperature       = 0.1,
                max_output_tokens = 1024,
            ),
        )
        text = (response.text or "").strip()
        print(f"[EMA EPAR] Extracted {len(text)} chars from {doc_type} for '{drug_name}'")
        return f"[EMA EPAR — {doc_type}]\n{text}"
    except Exception as e:
        print(f"[EMA EPAR] Gemini failed for {doc_type} ({pdf_url[:60]}): {e}")
        return ""


async def _gather_ema_evidence(drug_name: str, claim_reason: str) -> str:
    """
    Resolve all EMA brand names for the drug, fetch their EPAR PDFs,
    and extract Step 3 scientific barrier evidence from each.

    Uses ema_epar_extractor._resolve_all_ema_brands + get_ema_epar_documents
    for brand resolution and PDF link finding.
    Deduplicates PDFs so the same file is never sent to Gemini twice.
    """
    loop = asyncio.get_event_loop()

    # Step 1 — resolve all EMA brand names
    print(f"[EMA EPAR] Resolving EMA brands for '{drug_name}'...")
    brands = await loop.run_in_executor(None, _resolve_all_ema_brands, drug_name)

    if not brands:
        print(f"[EMA EPAR] No EMA brands found for '{drug_name}' — skipping")
        return ""

    print(f"[EMA EPAR] Found {len(brands)} brand(s): {brands}")

    # Step 2 — collect EPAR PDF URLs (deduplicated)
    seen_urls: set = set()
    pdf_tasks: List[Tuple[str, str]] = []  # (doc_type, url)

    for brand in brands:
        try:
            links = await loop.run_in_executor(None, _get_ema_epar_documents, brand)
        except Exception as e:
            print(f"[EMA EPAR] Failed to get EPAR docs for '{brand}': {e}")
            continue

        for key in _EMA_EPAR_PREFERENCE:
            url = links.get(key)
            if url and url not in seen_urls:
                seen_urls.add(url)
                pdf_tasks.append((key, url))
                print(f"[EMA EPAR] '{brand}' → {key}: {url[:80]}")
                break  # one PDF per brand

    if not pdf_tasks:
        print(f"[EMA EPAR] No EPAR PDFs found for any brand of '{drug_name}'")
        return ""

    # Step 3 — send each PDF to Gemini in parallel
    print(f"[EMA EPAR] Analysing {len(pdf_tasks)} EPAR PDF(s) for '{drug_name}'...")
    results = await asyncio.gather(
        *[_analyse_ema_pdf(url, doc_type, drug_name, claim_reason)
          for doc_type, url in pdf_tasks],
        return_exceptions=True,
    )

    parts = []
    for r in results:
        if isinstance(r, Exception):
            print(f"[EMA EPAR] Error: {r}")
        elif r and str(r).strip():
            parts.append(str(r).strip())

    if not parts:
        return ""

    return "[EMA Assessment Reports]\n\n" + "\n\n".join(parts)


# ── Europe PMC ────────────────────────────────────────────────────────────────

_EUROPEPMC_BASE    = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
_EUROPEPMC_DELAY   = 0.5   # seconds between requests — EBI requests polite crawling
_EUROPEPMC_RETRIES = 3


def _europepmc_search(query: str, max_results: int = 5) -> List[Dict]:
    """
    Search Europe PMC and return article dicts with title + abstract.
    Sorted by citation count (most cited first).
    Retries on 429.
    """
    params = {
        "query":      query,
        "resultType": "core",
        "pageSize":   max_results,
        "format":     "json",
        "sort":       "CITED desc",
    }

    for attempt in range(1, _EUROPEPMC_RETRIES + 1):
        try:
            r = requests.get(
                _EUROPEPMC_BASE,
                params=params,
                timeout=15,
                headers=_HTTP_HEADERS,
            )
            if r.status_code == 429:
                wait = (2 ** attempt) + random.uniform(0, 1)
                print(f"[EUROPEPMC] 429 on '{query}' (attempt {attempt}) — waiting {wait:.1f}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            articles = r.json().get("resultList", {}).get("result", []) or []
            print(f"[EUROPEPMC] '{query}' → {len(articles)} result(s)")
            return articles
        except Exception as e:
            if attempt == _EUROPEPMC_RETRIES:
                print(f"[EUROPEPMC] Search failed for '{query}': {e}")
                return []
            time.sleep(1)
    return []


def _europepmc_format_results(articles: List[Dict]) -> str:
    """Format Europe PMC results into a readable evidence block."""
    if not articles:
        return ""
    lines = []
    for a in articles:
        title    = a.get("title", "").strip()
        abstract = (a.get("abstractText") or "").strip()
        journal  = a.get("journalTitle", "").strip()
        year     = a.get("pubYear", "")
        cited_by = a.get("citedByCount", 0)
        pmid     = a.get("pmid", "")

        if not abstract:
            continue

        lines.append(
            f"Title   : {title}\n"
            f"Journal : {journal} ({year}) | Cited by: {cited_by} | PMID: {pmid}\n"
            f"Abstract: {abstract[:600]}{'...' if len(abstract) > 600 else ''}\n"
        )

    return "\n---\n".join(lines)


async def _gather_europepmc_evidence(drug_name: str) -> str:
    """
    Run all 6 keyword searches against Europe PMC sequentially with delays.
    Returns deduplicated formatted evidence block.
    """
    loop    = asyncio.get_event_loop()
    queries = [t.format(molecule=drug_name) for t in _KEYWORD_TEMPLATES]

    seen_ids:     set       = set()
    all_articles: List[Dict] = []

    for query in queries:
        articles = await loop.run_in_executor(None, _europepmc_search, query, 5)
        for article in articles:
            uid = article.get("id") or article.get("pmid") or article.get("doi", "")
            if uid and uid not in seen_ids:
                seen_ids.add(uid)
                all_articles.append(article)
        await asyncio.sleep(_EUROPEPMC_DELAY)

    if not all_articles:
        print(f"[EUROPEPMC] No articles found for '{drug_name}'")
        return ""

    print(f"[EUROPEPMC] {len(all_articles)} unique article(s) for '{drug_name}'")
    formatted = _europepmc_format_results(all_articles[:15])
    return f"[Europe PMC Evidence]\n{formatted}" if formatted else ""


# ── Completed trial rows from Excel ───────────────────────────────────────────

def _get_completed_rows(drug_rows: List[Dict]) -> List[Dict]:
    """Filter Excel rows to Status = Completed only."""
    completed = [
        r for r in drug_rows
        if str(r.get("Status", "")).strip().lower() == "completed"
    ]
    print(f"[STEP 3] {len(completed)} completed trial row(s) out of {len(drug_rows)} total")
    return completed


# ── Step 3 Gemini prompt ──────────────────────────────────────────────────────

STEP3_PROMPT = """You are a pharmaceutical patent expert performing scientific barrier analysis.
You are a SCEPTICAL REVIEWER. Your default position is NON-BLOCKING unless the evidence
explicitly and unambiguously proves otherwise.

DRUG NAME      : {drug_name}
PATENT NUMBER  : {patent_number}
JURISDICTION   : {jurisdiction}
CLAIM CATEGORY : {claim_category}  (Step 1)
CLAIM DETAILS  : {step1_reason}
MATCHED ELEMENTS (Step 2): {matched_elements}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SCIENTIFIC EVIDENCE GATHERED
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{evidence_block}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PATENT DOCUMENT CHUNKS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{context}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

─────────────────────────────────────────────
TASK — STEP 3: SCIENTIFIC BARRIER ANALYSIS
─────────────────────────────────────────────

Determine whether the claimed feature solves a REAL TECHNICAL BARRIER.
The standard is HIGH. Most patents will NOT meet it.

A REAL TECHNICAL BARRIER requires ALL of the following to be true:
1. Without this SPECIFIC feature (not just some formulation), the drug
   demonstrably fails — on stability, bioavailability, absorption, or safety
2. The evidence is EXPLICIT and DIRECT — a regulatory review (FDA/EMA) or
   peer-reviewed study states this specific feature was necessary, not just
   that it was used or that it produced improvements
3. No scientifically equivalent alternative existed at the time of filing
   that could achieve the same outcome

DOES NOT qualify as a real technical barrier:
- The feature improves convenience, dosing frequency, or patient compliance
- The feature improves manufacturing efficiency or cost
- Regulators accepted or approved the drug WITH this feature (accepted ≠ required)
- The drug works without this feature but less conveniently
- The evidence only shows the feature is common practice or industry standard
- General statements that formulation is important for this drug class
- The patent itself claims the feature is necessary (self-serving, not independent evidence)
- Improvements in PK/PD that are incremental, not enabling
- The claim category alone implies technical need — category is NOT evidence

─────────────────────────────────────────────
STRICT DECISION RULES — follow in order:
─────────────────────────────────────────────
1. If no independent external evidence (FDA/EMA/peer-reviewed) was found
   → is_technical_barrier = false, confidence = low

2. If evidence exists but only shows the feature is beneficial or preferred,
   not that the drug fails without it
   → is_technical_barrier = false, confidence = medium

3. If evidence is present but indirect, inferred, or from manufacturer sources only
   → is_technical_barrier = false, confidence = medium

4. Only if EXPLICIT independent evidence directly states the feature was
   technically necessary for the drug to function safely and effectively
   → is_technical_barrier = true, confidence = high

5. If you are uncertain after applying rules 1–4
   → is_technical_barrier = false, confidence = low

Confidence levels:
  high   → explicit, direct, independent statement of technical necessity
  medium → evidence exists but does not explicitly confirm necessity
  low    → no relevant evidence, or evidence is indirect/manufacturer-sourced

─────────────────────────────────────────────
OUTPUT — return ONLY valid JSON, no markdown:
─────────────────────────────────────────────
{{
  "is_technical_barrier": true or false,
  "confidence":           "high" or "medium" or "low",
  "evidence_type":        "FDA Review" | "EMA Assessment" | "Peer-reviewed Journal" | "Multiple Sources" | "Insufficient Evidence",
  "evidence_summary":     "2-3 sentences summarising the key scientific evidence found and why it does or does not confirm technical necessity",
  "reason":               "1-2 sentences: specific reason why this feature is/is not a real technical barrier — cite the evidence"
}}
"""


async def _run_step3(
    filename:       str,
    context:        str,
    step1_result:   Dict,
    step2_result:   Dict,
    drug_name:      str,
    drug_phase:     Dict[str, Optional[str]],
    drug_rows:      List[Dict],
) -> Optional[Dict]:
    """
    Step 3: Scientific barrier analysis.

    Determines whether the claimed feature solves a real technical barrier
    by consulting regulatory reviews (marketed) or journal literature (clinical).

    Args:
        filename:     Patent PDF filename
        context:      RAG context string
        step1_result: Output from _run_step1
        step2_result: Output from _run_step2
        drug_name:    Drug name string
        drug_phase:   {"US": phase_or_None, "EP": phase_or_None}
        drug_rows:    All Excel rows for this drug

    Returns:
        Parsed JSON dict or None on Gemini failure.
    """
    print(f"[STEP 3] Scientific barrier analysis for {filename}...")

    jurisdiction  = (step1_result.get("jurisdiction") or "").upper()
    phase         = drug_phase.get(jurisdiction) or drug_phase.get("US") or drug_phase.get("EP")
    claim_reason  = step1_result.get("reason") or ""
    claim_cat     = step1_result.get("claim_category") or ""
    matched_elems = step2_result.get("matched_elements") or []
    is_marketed   = (phase or "").lower() == "marketed"

    print(f"[STEP 3] {filename} — Phase: {phase} | Marketed: {is_marketed}")

    # ── Gather evidence in parallel ───────────────────────────────────────────
    evidence_parts: List[str] = []

    if is_marketed:
        print(f"[STEP 3] Marketed drug — fetching FDA reviews, EMA assessment, Europe PMC, open web search...")
        fda_ev, ema_ev, epmc_ev, gem_ev = await asyncio.gather(
            _gather_fda_evidence(drug_name, claim_reason),
            _gather_ema_evidence(drug_name, claim_reason),
            _gather_europepmc_evidence(drug_name),
            _gather_all_gemini_evidence(drug_name, claim_cat, claim_reason),
            return_exceptions=True,
        )

        for label, ev in [
            ("FDA",              fda_ev),
            ("EMA",              ema_ev),
            ("Europe PMC",       epmc_ev),
            ("Open Web Search",  gem_ev),
        ]:
            if isinstance(ev, Exception):
                print(f"[STEP 3] {label} evidence error: {ev}")
            elif ev and str(ev).strip():
                evidence_parts.append(str(ev).strip())

    else:
        print(f"[STEP 3] Clinical drug — fetching PubMed, Europe PMC, open web search...")
        pubmed_ev, epmc_ev, gem_ev = await asyncio.gather(
            _gather_pubmed_evidence(drug_name),
            _gather_europepmc_evidence(drug_name),
            _gather_all_gemini_evidence(drug_name, claim_cat, claim_reason),
            return_exceptions=True,
        )

        for label, ev in [
            ("PubMed",           pubmed_ev),
            ("Europe PMC",       epmc_ev),
            ("Open Web Search",  gem_ev),
        ]:
            if isinstance(ev, Exception):
                print(f"[STEP 3] {label} evidence error: {ev}")
            elif ev and str(ev).strip():
                evidence_parts.append(f"[{label} Evidence]\n{str(ev).strip()}")

        # Add completed trial rows from Excel as additional context
        completed_rows = _get_completed_rows(drug_rows)
        if completed_rows:
            completed_block = _format_rows_for_prompt(completed_rows)
            evidence_parts.append(
                f"[Completed Clinical Trial Formulation Data]\n{completed_block}"
            )

    if not evidence_parts:
        evidence_block = "No scientific evidence could be retrieved. Base assessment on patent claims and drug context."
    else:
        evidence_block = "\n\n" + "─" * 40 + "\n\n".join(evidence_parts)

    # ── Call Gemini for final determination ───────────────────────────────────
    safe_context = context.replace("{", "{{").replace("}", "}}")
    safe_evidence = _cap_evidence(evidence_block).replace("{", "{{").replace("}", "}}")

    prompt = STEP3_PROMPT.format(
        drug_name       = drug_name,
        patent_number   = step1_result.get("patent_number", Path(filename).stem),
        jurisdiction    = jurisdiction,
        claim_category  = claim_cat,
        step1_reason    = claim_reason,
        matched_elements = ", ".join(matched_elems) if matched_elems else "None",
        evidence_block  = safe_evidence,
        context         = safe_context,
    )

    result = await _call_gemini_json(prompt, filename, "STEP3")
    if result is None:
        return None

    # Normalise
    result["is_technical_barrier"] = bool(result.get("is_technical_barrier", False))
    result["confidence"]           = result.get("confidence", "low")
    result["evidence_type"]        = result.get("evidence_type", "Insufficient Evidence")
    result["evidence_summary"]     = result.get("evidence_summary", "")
    result["reason"]               = result.get("reason", "")

    # ── Confidence gate ───────────────────────────────────────────────────────
    # Only high confidence passes as a real technical barrier.
    # medium/low → force NON-BLOCKING regardless of is_technical_barrier value.
    if result["is_technical_barrier"] and result["confidence"] != "high":
        print(
            f"[STEP 3] {filename} → Confidence gate: "
            f"is_technical_barrier=True but confidence={result['confidence']} "
            f"→ overriding to False (insufficient evidence strength)"
        )
        result["is_technical_barrier"] = False
        result["reason"] = (
            f"[Confidence gate: {result['confidence']} confidence is insufficient] "
            + (result.get("reason") or "")
        )

    verdict = "CONTINUE → Step 4" if result["is_technical_barrier"] else "NON-BLOCKING"
    print(
        f"[STEP 3] {filename}\n"
        f"  Barrier     : {result['is_technical_barrier']} ({result['confidence']} confidence)\n"
        f"  Evidence    : {result['evidence_type']}\n"
        f"  Summary     : {result['evidence_summary'][:120]}\n"
        f"  Verdict     : {verdict}"
    )

    # Pass the raw evidence block through so Step 4 can reuse it without re-fetching
    result["_evidence_block"] = evidence_block
    return result
