#!/usr/bin/env python3
"""
check_patents.py
─────────────────
Local diagnostic script — lives next to dimension_1.py, in the same
IP_refactored/ package. Shows exactly what patent_filter() does for a given
drug (or every drug, via --all): which GCS folder it searched, every PDF it
found there, which BigQuery table it queried for approved patent numbers,
what came back, and — file by file — which GCS PDFs were kept vs skipped
and why. Nothing is downloaded or written anywhere; this only lists and
compares.

SETUP — fill in SERVICE_ACCOUNT_JSON_PATH below, then run locally:

    python -m IP_refactored.check_patents "BGM-0504"
    python -m IP_refactored.check_patents --all

Must be run with `-m` from the directory that CONTAINS the IP_refactored/
folder (the same place you'd run `python -m IP_refactored.dimension_1`
from) — not as `python check_patents.py`, because this file uses the
package's relative imports.

Local Python environment needs: google-cloud-bigquery, google-cloud-storage,
google-auth (the same packages dimension_1.py needs for GCS/BigQuery — no
AlloyDB/Gemini/pandas dependency here).
"""

import os

# ─────────────────────────────────────────────
# Hardcoded local settings — EDIT THESE before running
# ─────────────────────────────────────────────

# Path to your service-account JSON key file (used for BOTH BigQuery and GCS).
SERVICE_ACCOUNT_JSON_PATH = r"/path/to/your/service-account.json"

# Optional overrides — leave as "" to use config.py's built-in defaults /
# whatever is already set in your shell's environment variables.
GCS_BUCKET_OVERRIDE          = ""   # e.g. "my-patents-bucket"
PROJECT_ID_OVERRIDE          = ""   # e.g. "cognito-prod-394707"
PATENT_MASTER_TABLE_OVERRIDE = ""   # e.g. "cognito-dev-380506.stage.patent_master"

# These MUST be set before importing the package — config.py reads env vars
# exactly once, at import time.
if SERVICE_ACCOUNT_JSON_PATH and SERVICE_ACCOUNT_JSON_PATH != r"/path/to/your/service-account.json":
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = SERVICE_ACCOUNT_JSON_PATH
if GCS_BUCKET_OVERRIDE:
    os.environ["GCS_BUCKET"] = GCS_BUCKET_OVERRIDE
if PROJECT_ID_OVERRIDE:
    os.environ["PROJECT_ID"] = PROJECT_ID_OVERRIDE
if PATENT_MASTER_TABLE_OVERRIDE:
    os.environ["PATENT_MASTER_TABLE"] = PATENT_MASTER_TABLE_OVERRIDE

import sys
from pathlib import Path
from typing import Dict, List

from . import config
from . import gcp_utils
from .chunking.utils import normalize, normalize_patent_number
from .chunking.patent_filter import get_approved_patent_numbers, patent_filter
from .chunking.drug_list import get_glp1_drug_names


def _print_header():
    print("=" * 78)
    print("PATENT FILTER DIAGNOSTIC")
    print("=" * 78)
    creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or "(none set — will try ADC)"
    print(f"  Service account file : {creds}")
    print(f"  GCS bucket           : {config.GCS_BUCKET or '(NOT SET)'}")
    print(f"  GCS patents prefix   : {config.GCS_PATENTS_PREFIX}")
    print(f"  BigQuery project     : {config.PROJECT_ID}")
    print(f"  patent_master table  : {config.PATENT_MASTER_TABLE or '(NOT SET)'}")
    print("=" * 78)
    if not Path(creds).exists() if creds != "(none set — will try ADC)" else False:
        print(f"  !! WARNING: service account file not found at: {creds}")
        print("=" * 78)


def check_drug(drug_name: str) -> None:
    """Full trace of patent_filter()'s decision for one drug."""
    print(f"\n{'#' * 78}")
    print(f"# DRUG: {drug_name}")
    print(f"{'#' * 78}")

    # ── 1. Where in GCS it's looking ────────────────────────────────────────
    prefix = config.GCS_PATENTS_PREFIX.rstrip("/") + "/"
    drug_lower = drug_name.strip().lower()
    drug_norm = normalize(drug_name)
    print(f"\n[1] GCS folder search")
    print(f"    Searching under: gs://{config.GCS_BUCKET}/{prefix}")
    print(f"    Match order: (a) exact lower(folder_name) == '{drug_lower}', "
          f"then (b) fuzzy normalised == '{drug_norm}'")

    if not config.GCS_BUCKET:
        print("    !! GCS_BUCKET not set — cannot list anything. Stopping.")
        return

    try:
        all_blobs = gcp_utils.list_blobs_with_prefix(prefix)
    except Exception as e:
        print(f"    !! GCS listing failed: {e}")
        return

    prefix_depth = len(prefix.split("/")) - 1
    drug_folders_exact: Dict[str, str] = {}
    drug_folders_fuzzy: Dict[str, str] = {}
    for blob in all_blobs:
        parts = blob.name.split("/")
        if len(parts) > prefix_depth + 1:
            folder_name = parts[prefix_depth]
            folder_prefix = "/".join(parts[: prefix_depth + 1]) + "/"
            exact_key = folder_name.strip().lower()
            fuzzy_key = normalize(folder_name)
            if exact_key not in drug_folders_exact:
                drug_folders_exact[exact_key] = folder_prefix
            if fuzzy_key not in drug_folders_fuzzy:
                drug_folders_fuzzy[fuzzy_key] = folder_prefix

    if drug_lower in drug_folders_exact:
        matched_prefix = drug_folders_exact[drug_lower]
        print(f"    (a) Exact match found: gs://{config.GCS_BUCKET}/{matched_prefix}")
    elif drug_norm in drug_folders_fuzzy:
        matched_prefix = drug_folders_fuzzy[drug_norm]
        print(f"    (a) No exact match for '{drug_lower}'.")
        print(f"    (b) Fuzzy match found instead: gs://{config.GCS_BUCKET}/{matched_prefix}")
    else:
        print(f"    No GCS folder matches '{drug_name}' — neither exact nor fuzzy.")
        print(f"    Folders that DO exist under this prefix: {sorted(drug_folders_exact.keys()) or '(none found)'}")
        return

    pdf_blobs = [
        b for b in all_blobs
        if b.name.startswith(matched_prefix) and b.name.lower().endswith(".pdf") and not b.name.endswith("/")
    ]
    gcs_filenames = sorted(Path(b.name).name for b in pdf_blobs)
    print(f"    PDFs found in GCS ({len(gcs_filenames)}):")
    for fn in gcs_filenames:
        print(f"      - {fn}")
    if not gcs_filenames:
        print("    No PDFs in this folder. Stopping.")
        return

    # ── 2. What BigQuery approves for this molecule ─────────────────────────
    print(f"\n[2] BigQuery patent_master lookup")
    print(f"    Table: {config.PATENT_MASTER_TABLE}")
    print(f"    Query: lower(molecule_name) = '{drug_name.strip().lower()}' "
          f"AND patent_status = 'PDF Downloaded' AND confidence > 0.3")

    approved_numbers = get_approved_patent_numbers(drug_name)
    print(f"    Approved patent_number(s) returned ({len(approved_numbers)}):")
    for pn in approved_numbers:
        print(f"      - {pn}  (normalised: {normalize_patent_number(pn)})")
    if not approved_numbers:
        print("    No approved rows for this molecule in patent_master — patent_filter() will return 0 files.")

    # ── 3. File-by-file match: GCS filename vs approved patent numbers ──────
    approved_norm = {normalize_patent_number(p) for p in approved_numbers}
    print(f"\n[3] File-by-file match (GCS filename stem vs approved patent numbers, normalised)")
    kept, skipped = [], []
    for fn in gcs_filenames:
        stem_norm = normalize_patent_number(Path(fn).stem)
        is_match = stem_norm in approved_norm
        (kept if is_match else skipped).append(fn)
        status = "KEPT   " if is_match else "SKIPPED"
        print(f"      [{status}] {fn}  (normalised stem: {stem_norm})")

    # ── 4. Final result — exactly what patent_filter() returns ──────────────
    print(f"\n[4] Final result — what patent_filter('{drug_name}') returns and the "
          f"rest of the pipeline (indexing, blocking analysis, Excel) will use")
    actual = patent_filter(drug_name)
    actual_filenames = sorted(r["filename"] for r in actual)
    print(f"    {len(actual_filenames)} file(s) will be fetched/analysed:")
    for fn in actual_filenames:
        print(f"      - {fn}")
    if not actual_filenames:
        print("      (none)")

    print(f"\n    Summary: {len(gcs_filenames)} PDF(s) in GCS -> "
          f"{len(kept)} matched an approved patent number -> "
          f"{len(actual_filenames)} returned by patent_filter()")
    if sorted(kept) != actual_filenames:
        print("    !! Mismatch between the file-by-file trace above and patent_filter()'s "
              "actual output — this would indicate a bug, please report it.")


def check_all() -> None:
    """Runs check_drug() for every drug in the GLP-1 drug list (chunking.drug_list)."""
    print("\n[DRUG LIST] Resolving every drug via chunking.drug_list.get_glp1_drug_names() ...")
    drugs = get_glp1_drug_names()
    if not drugs:
        print("No drugs returned by the drug-list query. Nothing to check.")
        return
    print(f"[DRUG LIST] {len(drugs)} drug(s) to check: {drugs}")
    for drug in drugs:
        check_drug(drug)


if __name__ == "__main__":
    _print_header()

    if len(sys.argv) < 2:
        print("\nUsage:")
        print('  python -m IP_refactored.check_patents "<drug_name>"')
        print("  python -m IP_refactored.check_patents --all")
        sys.exit(1)

    if sys.argv[1] == "--all":
        check_all()
    else:
        check_drug(sys.argv[1])