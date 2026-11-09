"""
formulation_lookup.py
─────────────────────
The reference data for blocking-analysis STEP 2, found on the web instead of read from the formulation Excel.

Step 2 asks whether a patent claims something specific that this drug's real product actually uses (its form,
formulation, route, device or combination technology). That needs a description of the real product. The Excel
file (config.FORMULATION_EXCEL_PATH) is the curated source; when it has no rows for the drug, this module builds
equivalent rows from two public sources:

    1. ClinicalTrials.gov (API v2)  - the studies that name the drug as an intervention: intervention and arm
                                      descriptions ("solution for subcutaneous administration 1.34 mg/mL", ...)
    2. openFDA drug labels          - for an approved drug: description, dosage forms, inactive ingredients,
                                      how supplied, instructions for use (pen / autoinjector ...)

Gemini reads both once per drug and fills the same columns the Excel has (Active Ingredient & Form, Formulation
Details, Route of Administration, Device Description, Combination Tech/Process). It may only use what the sources
state; every record has to cite a study (NCT id) or label that was really fetched, and anything else is dropped.
The result is cached per drug in GCS (config.FORMULATION_CACHE_DAYS), so a drug is looked up once a month at most.

    get_drug_rows_with_fallback(drug)   Excel rows if the Excel has the drug, otherwise lookup_formulation_rows()
    lookup_formulation_rows(drug)       the rows built from ClinicalTrials.gov + openFDA (cached)

Set FORMULATION_LOOKUP_ENABLED=false to turn the lookup off (Step 2 then runs on the Excel or the patent alone).

Try it from a shell (no Gemini needed with --sources-only):
    python -m ip_pillar.primary_market_entry_horizon.blocking_analysis.formulation_lookup "Semaglutide" --sources-only
"""

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests

from ... import config, gcp_helpers, gcp_utils
from .step2_claim_elements import _STEP2_COLUMNS, get_drug_rows

logger = logging.getLogger(__name__)

CTGOV_URL = "https://clinicaltrials.gov/api/v2/studies"
OPENFDA_URL = "https://api.fda.gov/drug/label.json"
_CTGOV_FIELDS = ("NCTId|BriefTitle|Phase|OverallStatus|InterventionName|InterventionType|"
                 "InterventionDescription|InterventionOtherName|ArmGroupLabel|ArmGroupDescription")
_USER_AGENT = "ip-pillar/1.0"
_CACHE_FILE = "formulation_records.json"
_CACHE_VERSION = 1
_MAX_RECORDS = 12
_MAX_TERMS = 4
_MAX_VALUE_CHARS = 300
_BLANK = {"", "nan", "none", "n/a", "na", "null", "not stated", "not specified", "unknown"}

# Trailing words dropped from a drug name to get the active moiety ("Orforglipron Calcium" -> "Orforglipron").
_SALT_WORDS = {
    "calcium", "sodium", "potassium", "magnesium", "hydrochloride", "hcl", "acetate", "tartrate", "mesylate",
    "citrate", "phosphate", "sulfate", "maleate", "fumarate", "succinate", "erbumine", "arginine", "l-arginine",
    "hydrate", "anhydrous", "besylate", "tosylate", "bromide", "chloride", "biosimilar", "biobetter",
}


class SourceUnavailable(Exception):
    """A public API could not be read (network error, repeated 429/5xx, bad reply)."""


# ─────────────────────────────────────────────
# Search terms
# ─────────────────────────────────────────────

def _search_terms(drug_name: str) -> List[str]:
    """The names to search for: each part of a combination ('A+B', 'A and B', 'A/B') as written and without a
    trailing salt word. 'Orforglipron Calcium' -> ['Orforglipron', 'Orforglipron Calcium'];
    'Insulin Icodec+Semaglutide' -> ['Insulin Icodec', 'Semaglutide']."""
    terms: List[str] = []
    seen = set()
    for part in re.split(r"\s*\+\s*|\s+and\s+|\s*/\s*", str(drug_name or "").strip()):
        part = part.strip().replace('"', "")
        if not part:
            continue
        words = part.split()
        while len(words) > 1 and words[-1].lower() in _SALT_WORDS:
            words.pop()
        for candidate in (" ".join(words), part):
            if candidate and candidate.lower() not in seen:
                seen.add(candidate.lower())
                terms.append(candidate)
    return terms[:_MAX_TERMS]


# ─────────────────────────────────────────────
# HTTP
# ─────────────────────────────────────────────

def _get_json(url: str, params: Dict, what: str, retries: int = 3, backoff: float = 2.0) -> Optional[Dict]:
    """GET -> parsed JSON. None for 404 (openFDA answers 404 when nothing matches). Network errors, 429 and 5xx
    are retried; running out of retries (or any other error status) raises SourceUnavailable."""
    last: object = None
    for attempt in range(retries):
        try:
            response = requests.get(url, params=params, timeout=30, headers={"User-Agent": _USER_AGENT})
        except requests.RequestException as e:
            last = e
        else:
            status = response.status_code
            if status == 404:
                return None
            if status < 400:
                try:
                    return response.json()
                except ValueError as e:
                    last = e
            elif status in (429, 500, 502, 503, 504):
                last = f"HTTP {status}"
            else:
                raise SourceUnavailable(f"{what}: HTTP {status}")
        if attempt < retries - 1:
            time.sleep(backoff * (2 ** attempt))
    raise SourceUnavailable(f"{what}: {last}")


def _squash(text) -> str:
    """Single-spaced text without the replacement characters openFDA labels are full of."""
    return re.sub(r"\s+", " ", str(text or "").replace("�", " ")).strip()


# ─────────────────────────────────────────────
# Source 1 - ClinicalTrials.gov
# ─────────────────────────────────────────────

def _format_phase(phases) -> str:
    names = {"EARLY_PHASE1": "Early Phase 1", "PHASE1": "Phase 1", "PHASE2": "Phase 2", "PHASE3": "Phase 3",
             "PHASE4": "Phase 4", "NA": "N/A"}
    return "/".join(names.get(p, str(p)) for p in (phases or []))


def _parse_study(study: Dict) -> Optional[Dict]:
    protocol = (study or {}).get("protocolSection") or {}
    ident = protocol.get("identificationModule") or {}
    nct_id = str(ident.get("nctId") or "").strip().upper()
    if not nct_id:
        return None
    arms_module = protocol.get("armsInterventionsModule") or {}
    interventions = []
    for item in arms_module.get("interventions") or []:
        interventions.append({
            "type": str(item.get("type") or "").strip(),
            "name": _squash(item.get("name")),
            "description": _squash(item.get("description")),
            "other_names": [_squash(n) for n in item.get("otherNames") or [] if _squash(n)],
        })
    arms = [{"label": _squash(a.get("label")), "description": _squash(a.get("description"))}
            for a in arms_module.get("armGroups") or []]
    return {
        "nct_id": nct_id,
        "title": _squash(ident.get("briefTitle")),
        "status": str((protocol.get("statusModule") or {}).get("overallStatus") or "").strip(),
        "phase": _format_phase((protocol.get("designModule") or {}).get("phases")),
        "interventions": interventions,
        "arms": arms,
    }


def _informativeness(study: Dict) -> int:
    """How much descriptive text a study has - the studies worth showing to the model come first."""
    return (sum(len(i["description"]) for i in study["interventions"])
            + sum(len(a["description"]) for a in study["arms"]))


def fetch_ctgov_studies(drug_name: str, max_studies: Optional[int] = None) -> List[Dict]:
    """The most descriptive ClinicalTrials.gov studies that name the drug as an intervention (every search term,
    de-duplicated by NCT id). Raises SourceUnavailable when the API cannot be read."""
    max_studies = max_studies or config.CTGOV_MAX_STUDIES
    found: Dict[str, Dict] = {}
    for term in _search_terms(drug_name):
        data = _get_json(
            CTGOV_URL,
            {"query.intr": term, "pageSize": 50, "fields": _CTGOV_FIELDS, "sort": "@relevance"},
            what=f"ClinicalTrials.gov '{term}'",
        )
        for raw in (data or {}).get("studies") or []:
            study = _parse_study(raw)
            if study and study["nct_id"] not in found:
                found[study["nct_id"]] = study
    ranked = sorted(found.values(), key=_informativeness, reverse=True)
    return [s for s in ranked if _informativeness(s) > 0][:max_studies]


def _study_text(study: Dict, cap: int = 1500) -> str:
    lines = [f"{study['nct_id']} | {study['phase'] or 'phase not stated'} | {study['status']} | {study['title']}"]
    for item in study["interventions"]:
        also = f" (also called: {', '.join(item['other_names'])})" if item["other_names"] else ""
        lines.append(f"  Intervention [{item['type']}] {item['name']}{also}: {item['description'] or 'no description'}")
    for arm in study["arms"]:
        if arm["description"]:
            lines.append(f"  Arm {arm['label']}: {arm['description']}")
    return "\n".join(lines)[:cap]


# ─────────────────────────────────────────────
# Source 2 - openFDA drug labels
# ─────────────────────────────────────────────

# (label field, characters kept). The rest of a label (adverse reactions, studies ...) says nothing about the product.
_LABEL_FIELDS = (
    ("description", 2000),
    ("dosage_forms_and_strengths", 1500),
    ("how_supplied", 1200),
    ("spl_product_data_elements", 2500),
    ("dosage_and_administration", 1000),
    ("instructions_for_use", 1500),
)


def _parse_label(item: Dict) -> Optional[Dict]:
    openfda = (item or {}).get("openfda") or {}
    brands = [_squash(b) for b in openfda.get("brand_name") or [] if _squash(b)]
    if not brands:
        return None
    sections = {}
    for field, cap in _LABEL_FIELDS:
        value = item.get(field)
        text = _squash(value[0] if isinstance(value, list) and value else value)
        if text:
            sections[field] = text[:cap]
    return {
        "brands": brands,
        "generic": [_squash(g) for g in openfda.get("generic_name") or []],
        "route": [_squash(r) for r in openfda.get("route") or []],
        "sections": sections,
    }


def fetch_openfda_labels(drug_name: str, max_labels: int = 3) -> List[Dict]:
    """Up to *max_labels* distinct FDA labels (one per brand) for the drug, labels that describe their dosage
    forms first. An empty list when the drug has no FDA label. Raises SourceUnavailable when the API fails."""
    found: Dict[str, Dict] = {}
    for term in _search_terms(drug_name):
        params = {"search": f'openfda.generic_name:"{term}" openfda.substance_name:"{term}"', "limit": 10}
        if config.OPENFDA_API_KEY:
            params["api_key"] = config.OPENFDA_API_KEY
        data = _get_json(OPENFDA_URL, params, what=f"openFDA '{term}'")
        for raw in (data or {}).get("results") or []:
            label = _parse_label(raw)
            if not label:
                continue
            key = label["brands"][0].upper()
            if key not in found or len(label["sections"]) > len(found[key]["sections"]):
                found[key] = label
    ranked = sorted(found.values(), key=lambda lab: ("dosage_forms_and_strengths" not in lab["sections"],
                                                     -len(lab["sections"]), lab["brands"][0]))
    return ranked[:max_labels]


def _label_text(label: Dict) -> str:
    lines = [f"FDA label: {', '.join(label['brands'])} (generic: {', '.join(label['generic']) or 'n/a'}; "
             f"route: {', '.join(label['route']) or 'n/a'})"]
    for field, _ in _LABEL_FIELDS:
        if field in label["sections"]:
            lines.append(f"  {field}: {label['sections'][field]}")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# Gemini: sources -> rows
# ─────────────────────────────────────────────

_PROMPT = """You are a pharmaceutical formulation analyst. Describe the real-world product(s) of one drug using ONLY the
sources below. The description is the reference for a patent freedom-to-operate check, so it must be accurate.

DRUG: ##DRUG##

##SOURCES##

TASK: write one record per distinct product / dosage form / regimen of this drug that the sources describe.
(If the drug is a combination, a record may describe the combination product or one of its components.)

Each record has these fields:
  "Trial ID"                    - the NCT id of the study, or "FDA label: <BRAND>" for a label. Copy it exactly from
                                  the sources. A record that cites anything else is discarded.
  "Phase"                       - the study phase from the source, or "Approved" for an FDA label
  "Active Ingredient & Form"    - the active ingredient and its chemical form if stated (named salt, free base,
                                  conjugate ...), e.g. "semaglutide"
  "Formulation Details"         - dosage form, strength, concentration, named excipients, release mechanism
  "Route of Administration"     - the route (oral, subcutaneous ...)
  "Device Description"          - ONLY a device the source names (pen injector, autoinjector, prefilled syringe,
                                  patch, inhaler ...) with the detail given
  "Combination Tech/Process"    - ONLY a named co-formulated technology, absorption enhancer or process that the
                                  source names (for example an excipient that enables oral absorption), or a
                                  named combination product
  "Source Quote"                - a short exact quote (under 200 characters) from the source that supports the record

RULES:
- Use only what a source states. Never use your own knowledge of the drug and never guess.
- If a source does not state a field, return an empty string for it. Empty is correct and expected.
- Keep each value specific and short (under 250 characters). Do not write generic filler.
- Skip studies that do not describe this drug's own product (for example where it is only a comparator with no
  description of its form).
- At most ##MAX## records. Put FDA label records first.
- Return ONLY valid JSON, no markdown:
{"records": [{"Trial ID": "...", "Phase": "...", "Active Ingredient & Form": "...", "Formulation Details": "...",
 "Route of Administration": "...", "Device Description": "...", "Combination Tech/Process": "...",
 "Source Quote": "..."}]}
"""


def build_prompt(drug_name: str, studies: List[Dict], labels: List[Dict]) -> Tuple[str, set, List[str]]:
    """(prompt, the NCT ids the model may cite, the upper-case brand names it may cite). Labels go first, then as
    many studies as fit in config.FORMULATION_MAX_PROMPT_CHARS."""
    budget = config.FORMULATION_MAX_PROMPT_CHARS
    blocks: List[str] = []
    brands: List[str] = []
    for label in labels:
        text = _label_text(label)
        blocks.append(text)
        budget -= len(text)
        brands.extend(b.upper() for b in label["brands"])
    nct_ids = set()
    for study in studies:
        text = _study_text(study)
        if budget - len(text) < 0 and nct_ids:
            break
        blocks.append(text)
        nct_ids.add(study["nct_id"])
        budget -= len(text)
    sources = ("SOURCES (FDA labels, then ClinicalTrials.gov studies):\n\n" + "\n\n".join(blocks)) if blocks else ""
    prompt = _PROMPT.replace("##DRUG##", drug_name).replace("##SOURCES##", sources).replace("##MAX##", str(_MAX_RECORDS))
    return prompt, nct_ids, brands


def _clean_value(value) -> str:
    text = _squash(value)
    return "" if text.lower() in _BLANK else text[:_MAX_VALUE_CHARS]


def clean_records(raw, nct_ids: set, brands: List[str]) -> Tuple[List[Dict], List[Dict], int]:
    """(rows for Step 2, audit records with their quotes, number dropped). A record is dropped when it cites a study
    or label that was not fetched, when it has none of the five formulation fields, or when it repeats another
    record. Rows carry only the columns Step 2 reads; the quote stays in the audit list."""
    rows: List[Dict] = []
    audit: List[Dict] = []
    seen = set()
    dropped = 0
    for record in raw if isinstance(raw, list) else []:
        if not isinstance(record, dict):
            dropped += 1
            continue
        source = _clean_value(record.get("Trial ID"))
        upper = source.upper()
        is_label = upper.startswith("FDA LABEL") and any(brand in upper for brand in brands)
        match = re.search(r"NCT\d{8}", upper)
        is_study = bool(match) and match.group(0) in nct_ids
        if not (is_label or is_study):
            dropped += 1
            logger.warning("[FORMULATION] Dropped a record citing a source that was not fetched: %r", source)
            continue
        fields = {column: _clean_value(record.get(column)) for column in _STEP2_COLUMNS}
        if not any(fields.values()):
            dropped += 1
            continue
        fingerprint = tuple(fields.values())
        if fingerprint in seen:
            dropped += 1
            continue
        seen.add(fingerprint)
        row = {"Trial ID": match.group(0) if is_study else source, "Phase": _clean_value(record.get("Phase"))}
        row.update(fields)
        rows.append({k: v for k, v in row.items() if v})
        audit.append({**row, "Source Quote": _clean_value(record.get("Source Quote"))})
        if len(rows) >= _MAX_RECORDS:
            break
    # FDA label records first, as asked of the model
    order = sorted(range(len(rows)), key=lambda i: not str(rows[i].get("Trial ID", "")).upper().startswith("FDA LABEL"))
    return [rows[i] for i in order], [audit[i] for i in order], dropped


# ─────────────────────────────────────────────
# Cache (GCS, one file per drug)
# ─────────────────────────────────────────────

def _cache_read(drug_name: str) -> Optional[Dict]:
    if config.REINDEX:
        return None
    try:
        data = gcp_utils.read_json(config.FORMULATION_CACHE_SUBFOLDER, _CACHE_FILE, drug_name=drug_name)
    except Exception as e:
        logger.warning("[FORMULATION] Cache read failed for %r: %s", drug_name, e)
        return None
    if not isinstance(data, dict) or data.get("version") != _CACHE_VERSION or not isinstance(data.get("records"), list):
        return None
    try:
        created = datetime.fromisoformat(str(data["created_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - created > timedelta(days=config.FORMULATION_CACHE_DAYS):
        return None
    return data


def _cache_write(drug_name: str, rows: List[Dict], audit: List[Dict], studies: List[Dict], labels: List[Dict]) -> None:
    payload = {
        "version": _CACHE_VERSION,
        "drug_name": drug_name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sources": {"clinicaltrials_gov": [s["nct_id"] for s in studies],
                    "fda_labels": [b for label in labels for b in label["brands"]]},
        "records": rows,
        "audit": audit,
    }
    try:
        gcp_utils.write_json(config.FORMULATION_CACHE_SUBFOLDER, _CACHE_FILE, payload, drug_name=drug_name)
    except Exception as e:
        logger.warning("[FORMULATION] Cache write failed for %r: %s", drug_name, e)


# ─────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────

def fetch_sources(drug_name: str) -> Tuple[List[Dict], List[Dict], bool]:
    """(studies, labels, complete). complete is False when either API could not be read, so a partial answer is
    used for this run but never cached."""
    studies: List[Dict] = []
    labels: List[Dict] = []
    complete = True
    try:
        studies = fetch_ctgov_studies(drug_name)
    except SourceUnavailable as e:
        complete = False
        logger.warning("[FORMULATION] ClinicalTrials.gov unavailable: %s", e)
    try:
        labels = fetch_openfda_labels(drug_name)
    except SourceUnavailable as e:
        complete = False
        logger.warning("[FORMULATION] openFDA unavailable: %s", e)
    return studies, labels, complete


async def lookup_formulation_rows(drug_name: str) -> List[Dict]:
    """Step 2 reference rows for the drug from ClinicalTrials.gov + openFDA, via Gemini. Cached per drug. An empty
    list when the sources say nothing about the product, or when a source or Gemini fails (nothing is cached then)."""
    cached = await asyncio.to_thread(_cache_read, drug_name)
    if cached is not None:
        logger.info("[FORMULATION] %d cached record(s) for %r (built %s)", len(cached["records"]), drug_name,
                    cached.get("created_at"))
        return cached["records"]

    studies, labels, complete = await asyncio.to_thread(fetch_sources, drug_name)
    logger.info("[FORMULATION] %r: %d ClinicalTrials.gov stud(ies), %d FDA label(s)%s", drug_name, len(studies),
                len(labels), "" if complete else " (a source was unavailable)")
    if not studies and not labels:
        if complete:
            await asyncio.to_thread(_cache_write, drug_name, [], [], [], [])   # known to have nothing: don't ask again
        return []

    prompt, nct_ids, brands = build_prompt(drug_name, studies, labels)
    result = await gcp_helpers.call_gemini_json(prompt, f"formulation lookup {drug_name}", "FORMULATION")
    if not isinstance(result, dict):
        logger.warning("[FORMULATION] Gemini returned nothing usable for %r", drug_name)
        return []

    rows, audit, dropped = clean_records(result.get("records"), nct_ids, brands)
    logger.info("[FORMULATION] %r: %d record(s) kept, %d dropped", drug_name, len(rows), dropped)
    if complete:
        await asyncio.to_thread(_cache_write, drug_name, rows, audit, studies, labels)
    return rows


async def get_drug_rows_with_fallback(drug_name: str) -> List[Dict]:
    """The Excel rows for the drug when the Excel has any (the curated source always wins); otherwise the rows
    looked up on ClinicalTrials.gov + openFDA. Never raises: a failed lookup means no rows."""
    rows = get_drug_rows(drug_name)
    if rows:
        logger.info("[FORMULATION] Using %d row(s) from the formulation Excel for %r", len(rows), drug_name)
        return rows
    if not config.FORMULATION_LOOKUP_ENABLED:
        return []
    try:
        return await lookup_formulation_rows(drug_name)
    except Exception:
        logger.exception("[FORMULATION] Lookup failed for %r - Step 2 will assess from the patent alone", drug_name)
        return []


# ─────────────────────────────────────────────
# Command line
# ─────────────────────────────────────────────

def _main() -> None:
    parser = argparse.ArgumentParser(description="Build the Step 2 formulation records for a drug from "
                                                 "ClinicalTrials.gov and openFDA")
    parser.add_argument("drug")
    parser.add_argument("--sources-only", action="store_true",
                        help="fetch the sources and show what Gemini would receive; no Gemini call, no cache")
    parser.add_argument("--no-cache", action="store_true", help="ignore the cached records (the result is still cached)")
    parser.add_argument("--show-prompt", action="store_true")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):   # labels contain symbols a Windows console (cp1252) cannot print
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.sources_only:
        studies, labels, complete = fetch_sources(args.drug)
        prompt, nct_ids, brands = build_prompt(args.drug, studies, labels)
        print(f"search terms : {_search_terms(args.drug)}")
        print(f"studies      : {len(studies)} {sorted(nct_ids)[:8]}{' ...' if len(nct_ids) > 8 else ''}")
        print(f"FDA labels   : {len(labels)} {brands}")
        print(f"complete     : {complete}")
        print(f"prompt chars : {len(prompt)}")
        if args.show_prompt:
            print("\n" + prompt)
        return

    if args.no_cache:
        config.REINDEX = True
    rows = asyncio.run(lookup_formulation_rows(args.drug))
    print(json.dumps(rows, indent=2, ensure_ascii=False))
    print(f"\n{len(rows)} record(s)")


if __name__ == "__main__":
    _main()
