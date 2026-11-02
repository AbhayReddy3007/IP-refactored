"""
sec_edgar.py
────────────
SEC EDGAR: CIK lookup and 10-K / 20-F text.
"""

import time
from typing import Optional
import requests
from bs4 import BeautifulSoup
from .companies import _get_company_config
from .constants import _HTTP_DELAY, _HTTP_HEADERS


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helper
# ─────────────────────────────────────────────────────────────────────────────

def _http_get(url: str, **kwargs) -> Optional[requests.Response]:
    try:
        time.sleep(_HTTP_DELAY)
        r = requests.get(url, headers=_HTTP_HEADERS, timeout=15, **kwargs)
        if r.status_code == 200:
            return r
        print(f"[HTTP] {r.status_code} → {url}")
    except Exception as e:
        print(f"[HTTP] Error: {url} — {e}")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Source 1 — SEC EDGAR
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_cik(company: str) -> Optional[str]:
    cfg = _get_company_config(company)
    if cfg.get("sec_cik"):
        print(f"[SEC] Known CIK: {cfg['sec_cik']} for '{company}'")
        return cfg["sec_cik"]
    resp = _http_get("https://www.sec.gov/files/company_tickers.json")
    if resp:
        try:
            tickers = resp.json()
            lc = company.strip().lower()
            for _, entry in tickers.items():
                if lc in str(entry.get("title", "")).lower():
                    cik = str(entry["cik_str"]).zfill(10)
                    print(f"[SEC] Resolved CIK: {cik}")
                    return cik
        except Exception:
            pass
    return None


def _fetch_sec_10k(company: str) -> str:
    cik = _resolve_cik(company)
    if not cik:
        print(f"[SEC] Could not resolve CIK for '{company}'")
        return ""

    resp = _http_get(f"https://data.sec.gov/submissions/CIK{cik}.json")
    if not resp:
        return ""

    try:
        data        = resp.json()
        co_name     = data.get("name", company)
        recent      = data.get("filings", {}).get("recent", {})
        forms       = recent.get("form", [])
        accessions  = recent.get("accessionNumber", [])
        dates       = recent.get("filingDate", [])
        primary_doc = recent.get("primaryDocument", [])

        filing_url = filing_date = form_type = None
        for i, form in enumerate(forms):
            if form in ("10-K", "20-F", "10-K/A", "20-F/A"):
                acc = accessions[i].replace("-", "")
                filing_url  = (
                    f"https://www.sec.gov/Archives/edgar/data/"
                    f"{cik.lstrip('0')}/{acc}/{primary_doc[i]}"
                )
                filing_date = dates[i]
                form_type   = form
                print(f"[SEC] {form} filed {filing_date}: {filing_url}")
                break

        if not filing_url:
            print(f"[SEC] No 10-K/20-F found for '{company}'")
            return ""

        doc_resp = _http_get(filing_url)
        if not doc_resp:
            return ""

        text = BeautifulSoup(doc_resp.text, "html.parser").get_text(separator="\n", strip=True)
        if len(text) > 200_000:
            text = text[:200_000]
        print(f"[SEC] Extracted {len(text)} chars from {form_type}")
        return f"SOURCE: SEC {form_type} filed {filing_date}\nCompany: {co_name}\n\n{text}"

    except Exception as e:
        print(f"[SEC] Parse error: {e}")
        return ""
