"""
batch.py
────────
Parallel batch over every drug, with CSV export.
"""

import csv
import asyncio
import random
from .... import gcp_utils
from ....drug_list import list_glp1_patent_files
from .analysis import analyze_innovator_filing_patterns
from .bq_storage import create_dataset_if_not_exists, create_table_if_not_exists, get_bq_client, insert_into_bq
from .constants import MAX_CONCURRENT_DRUGS, MAX_RETRIES, TIMEOUT_PER_DRUG, _STEP4_SUBFOLDER, log_error


# ─────────────────────────────────────────────
# GCS LIST
# ─────────────────────────────────────────────
def list_all_gcs_drugs():
    drugs = sorted(list_glp1_patent_files())
    print(f"[GCS] Found {len(drugs)} drugs")
    return drugs

# ─────────────────────────────────────────────
# CSV
# ─────────────────────────────────────────────
def save_drug_csv(drug, result):
    """Write CSV to GCS instead of local disk."""
    import io as _io
    buf = _io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Drug", "Company", "Characterization", "Confidence"])

    for inv in result.get("innovators", []):
        writer.writerow([
            drug,
            inv.get("company"),
            inv.get("characterization"),
            inv.get("confidence"),
        ])

    uri = gcp_utils.write_bytes(
        _STEP4_SUBFOLDER,
        f"{drug}.csv",
        buf.getvalue().encode("utf-8"),
        content_type="text/csv",
    )
    print(f"[STEP4] CSV saved to GCS: {uri}")
    return uri

# ─────────────────────────────────────────────
# PROCESS DRUG
# ─────────────────────────────────────────────
async def process_drug(i, total, drug, semaphore, bq_client, results, failed):

    print(f"\n[{i}/{total}] {drug}")

    for attempt in range(MAX_RETRIES + 1):
        try:
            async with semaphore:
                result = await asyncio.wait_for(
                    analyze_innovator_filing_patterns(drug),
                    timeout=TIMEOUT_PER_DRUG
                )

            if result:
                results[drug] = result

                save_drug_csv(drug, result)
                insert_into_bq(bq_client, drug, result)

                print(f"✅ Done: {drug}")
                return

            raise Exception("Empty result")

        except Exception as e:
            if attempt < MAX_RETRIES:
                wait = (2 ** attempt) + random.random()
                print(f"[Retry] {drug} in {wait:.2f}s")
                await asyncio.sleep(wait)
            else:
                print(f"[FAILED] {drug}: {e}")
                log_error(f"{drug}: {e}")
                failed.append((drug, str(e)))

# ─────────────────────────────────────────────
# RUN ALL
# ─────────────────────────────────────────────
async def run_all(drugs):

    # ✅ INIT BIGQUERY
    bq_client = get_bq_client()
    create_dataset_if_not_exists(bq_client)
    create_table_if_not_exists(bq_client)

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_DRUGS)

    results = {}
    failed = []

    tasks = [
        process_drug(i + 1, len(drugs), drug, semaphore, bq_client, results, failed)
        for i, drug in enumerate(drugs)
    ]

    await asyncio.gather(*tasks)

    print("\n======== SUMMARY ========")
    print(f"Success: {len(results)}")
    print(f"Failed : {len(failed)}")
