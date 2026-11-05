"""
batch.py
────────
Batch mode over every company.
"""

import asyncio
import os
from typing import List
from ....drug_list import GLP1_DRUG_QUERY
from .bq_storage import _bq_client
from .constants import BQ_DATASET_ID, BQ_PROJECT_ID
from .reporting import export_to_excel, print_assessment
from .review import review_business_strategy


# ─────────────────────────────────────────────────────────────────────────────
# Batch — fetch companies from BigQuery
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_all_companies(therapy: str = "GLP-1") -> List[str]:
    src_table = f"{BQ_PROJECT_ID}.{BQ_DATASET_ID}.{os.getenv('BQ_SOURCE_TABLE', 'patent_pipeline')}"
    # Companies are scoped to the same GLP-1 drug universe as the rest of the pipeline
    # (drug_list.GLP1_DRUG_QUERY); only its WHERE clause is needed here since this query
    # selects Parent_Company_Name, not cleaned_generic_name.
    where_clause = GLP1_DRUG_QUERY.strip().split("WHERE", 1)[1]
    query = f"""
    SELECT DISTINCT Parent_Company_Name
    FROM `{src_table}`
    WHERE {where_clause}
    ORDER BY Parent_Company_Name
    """
    try:
        df        = _bq_client.query(query).to_dataframe()
        companies = (
            df["Parent_Company_Name"]
            .dropna().astype(str).str.strip()
            .loc[lambda s: (s != "") & (s.str.lower() != "nan")]
            .unique().tolist()
        )
        companies = sorted(companies)
        print(f"[BQ] Found {len(companies)} companies in source table")
        return companies
    except Exception as e:
        print(f"[BQ] Source query failed: {e}")
        return []


async def _run_batch(companies: List[str], therapy_area: str, export: bool):

    succeeded, failed = 0, []
    for i, company in enumerate(companies, 1):
        print(f"\n{'█'*70}\n  [{i}/{len(companies)}] {company}\n{'█'*70}")
        try:
            loop       = asyncio.get_event_loop()
            assessment = await loop.run_in_executor(
                None,
                lambda c=company: review_business_strategy(
                    company=c, therapy_area=therapy_area, write_bq=True
                ),
            )
            print_assessment(assessment)
            if export:
                safe = company.lower().replace(" ", "_").replace("/", "_")
                export_to_excel(assessment, f"{safe}.xlsx")
            succeeded += 1
        except Exception as e:
            print(f"[ERROR] {company}: {e}")
            failed.append((company, str(e)))

    print(f"\n{'═'*70}")
    print(f"  Done — {succeeded}/{len(companies)} succeeded, {len(failed)} failed")
    if failed:
        for c, e in failed:
            print(f"  ✗ {c}: {e}")
    print(f"{'═'*70}")
