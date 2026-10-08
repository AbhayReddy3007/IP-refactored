#!/usr/bin/env python3
"""
check_patent_filter.py
────────────────────────
Diagnostic script — NOT part of the pipeline, doesn't write anything.
Shows, for one or more drug names, exactly what
chunking.patent_filter.patent_filter() is doing internally:

  1. Every PDF sitting in GCS under the drug's folder (where it's looking,
     and everything that's actually there — before any filtering).
  2. The approved patent numbers patent_master returns for that drug
     (patent_status = 'PDF Downloaded', confidence > 0.3).
  3. Which GCS PDFs matched an approved patent number -> FETCHED,
     which did not -> SKIPPED.
  4. The real patent_filter() output, cross-checked against step 3 as a
     sanity check (the two should always agree — a mismatch means this
     script and patent_filter.py have drifted apart).

Lives alongside dimension_1.py in IP_refactored/, so it uses the same
package-relative imports — run it as a module, same as dimension_1.py:

    python -m IP_refactored.check_patent_filter

Drug names come from a hardcoded JSON file (edit DRUG_LIST_JSON_PATH
below). The JSON can be either a plain list:

    ["Semaglutide", "Tirzepatide"]

or a dict:

    {"drug_names": ["Semaglutide", "Tirzepatide"]}

If the file is missing, empty, or unreadable, this falls back to every
GLP-1 drug from chunking.drug_list.get_glp1_drug_names() so the script is
still runnable with zero setup.
"""

import json
import sys
from pathlib import Path
from typing import Dict, List

from . import config
from . import gcp_utils
from .chunking.utils import normalize, normalize_patent_number
from .chunking.patent_filter import patent_filter, get_approved_patent_numbers

# ─────────────────────────────────────────────
# EDIT THIS — hardcoded path to your drug-list JSON file
# ─────────────────────────────────────────────
DRUG_LIST_JSON_PATH = "/path/to/drug_list.json"


def load_drug_names(path: str) -> List[str]:
    """Reads DRUG_LIST_JSON_PATH -> list of drug names. Falls back to the
    full GLP-1 drug list (chunking.drug_list) if the file is missing, empty,
    or unreadable, so the script always has something to check."""
    p = Path(path)
    if p.exists():
        try:
            data = json.loads(p.read_text())
            if isinstance(data, dict):
                names = data.get("drug_names", [])
            elif isinstance(data, list):
                names = data
            else:
                names = []
            names = [str(n).strip() for n in names if str(n).strip()]
            if names:
                print(f"[CHECK] Loaded {len(names)} drug name(s) from {path}: {names}")
                return names
            print(f"[CHECK] {path} has no drug names — falling back to GLP-1 discovery")
        except Exception as e:
            print(f"[CHECK] Could not parse {path} ({e}) — falling back to GLP-1 discovery")
    else:
        print(f"[CHECK] {path} not found — falling back to GLP-1 discovery")

    from .chunking.drug_list import get_glp1_drug_names
    return get_glp1_drug_names()


def list_raw_gcs_pdfs(drug_name: str) -> List[Dict]:
    """Re-does patent_filter()'s GCS folder lookup WITHOUT the patent_master
    filter, so we can see every PDF sitting in GCS for this drug, regardless
    of approval status. Mirrors patent_filter.py's own folder-matching logic
    exactly — if that logic ever changes there, update this too."""
    if not config.GCS_BUCKET:
        print("[CHECK] GCS_BUCKET not set")
        return []

    prefix = config.GCS_PATENTS_PREFIX.rstrip("/") + "/"
    drug_norm = normalize(drug_name)

    all_blobs = gcp_utils.list_blobs_with_prefix(prefix)
    prefix_depth = len(prefix.split("/")) - 1

    drug_folders: Dict[str, str] = {}
    for blob in all_blobs:
        parts = blob.name.split("/")
        if len(parts) > prefix_depth + 1:
            folder_name = parts[prefix_depth]
            norm = normalize(folder_name)
            if norm not in drug_folders:
                drug_folders[norm] = "/".join(parts[: prefix_depth + 1]) + "/"

    if drug_norm not in drug_folders:
        print(f"[CHECK] No GCS folder matching '{drug_name}'. Available folders: {list(drug_folders.keys())}")
        return []

    matched_prefix = drug_folders[drug_norm]
    pdf_blobs = [
        b for b in all_blobs
        if b.name.startswith(matched_prefix)
        and b.name.lower().endswith(".pdf")
        and not b.name.endswith("/")
    ]
    return [
        {"filename": Path(b.name).name, "blob_name": b.name}
        for b in sorted(pdf_blobs, key=lambda b: b.name)
    ]


def check_drug(drug_name: str) -> None:
    print(f"\n{'=' * 70}\nDRUG: {drug_name}\n{'=' * 70}")

    # ── 1. Where it's looking, and everything actually in GCS ──────────────
    gcs_pdfs = list_raw_gcs_pdfs(drug_name)
    print(f"\n[1] GCS — gs://{config.GCS_BUCKET}/{config.GCS_PATENTS_PREFIX}/<folder matching '{drug_name}'>/")
    if not gcs_pdfs:
        print("    (no PDFs found — nothing more to check for this drug)")
        return
    for ref in gcs_pdfs:
        print(f"    {ref['blob_name']}")
    print(f"    -> {len(gcs_pdfs)} PDF(s) total in GCS")

    # ── 2. What patent_master approves for this molecule ───────────────────
    approved = get_approved_patent_numbers(drug_name)
    print(
        f"\n[2] BigQuery patent_master ({config.PATENT_MASTER_TABLE}) — "
        f"lower(molecule_name) = '{drug_name.lower()}', patent_status = 'PDF Downloaded', confidence > 0.3"
    )
    if not approved:
        print("    (no approved patent_number rows for this molecule)")
    else:
        for pn in approved:
            print(f"    {pn}")
        print(f"    -> {len(approved)} approved patent number(s)")
    approved_norm = {normalize_patent_number(p) for p in approved}

    # ── 3. Match GCS PDFs against approved patent numbers ──────────────────
    print("\n[3] Match — GCS filename vs approved patent_number (punctuation/case ignored)")
    fetched, skipped = [], []
    for ref in gcs_pdfs:
        stem_norm = normalize_patent_number(Path(ref["filename"]).stem)
        if stem_norm in approved_norm:
            fetched.append(ref)
            print(f"    FETCH  {ref['filename']}")
        else:
            skipped.append(ref)
            print(f"    SKIP   {ref['filename']}  (not in patent_master approved list for this molecule)")

    # ── 4. Cross-check against the real patent_filter() output ─────────────
    actual = patent_filter(drug_name)
    actual_names = sorted(r["filename"] for r in actual)
    fetched_names = sorted(r["filename"] for r in fetched)
    print(f"\n[4] patent_filter('{drug_name}') actually returned {len(actual)} file(s): {actual_names}")
    if actual_names == fetched_names:
        print("    -> matches this script's independent recomputation. OK")
    else:
        print("    -> MISMATCH vs this script's recomputation — investigate:")
        print(f"       patent_filter() only: {sorted(set(actual_names) - set(fetched_names))}")
        print(f"       this script only:     {sorted(set(fetched_names) - set(actual_names))}")

    print(
        f"\n[SUMMARY] {drug_name}: {len(gcs_pdfs)} in GCS | {len(approved)} approved in patent_master | "
        f"{len(fetched)} fetched | {len(skipped)} skipped"
    )


def main():
    drug_names = load_drug_names(DRUG_LIST_JSON_PATH)
    if not drug_names:
        print("[CHECK] No drug names to check.")
        sys.exit(1)

    for drug_name in drug_names:
        check_drug(drug_name)


if __name__ == "__main__":
    main()