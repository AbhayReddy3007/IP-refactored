"""
bq_loaders.py
─────────────
BigQuery client and the loaders for the drug-company map, forecast_s3, filing patterns and company analysis.
"""

from typing import Dict
import pandas as pd
from .... import gcp_utils
from ....drug_list import GLP1_DRUG_QUERY
from .constants import BQ_COMPANY_ANALYSIS_TABLE, BQ_FILING_PATTERN_TABLE, BQ_FORECAST_S3_TABLE, DRUG_COMPANY_TABLE


# ─────────────────────────────────────────────
# BigQuery client helper
# ─────────────────────────────────────────────

def _get_bq_client():
    return gcp_utils.get_bq_client()


# ─────────────────────────────────────────────
# 1. Load drug → company mapping from BigQuery
# ─────────────────────────────────────────────

def _load_drug_company_map() -> Dict[str, Dict]:
    """
    Queries BigQuery for drug → parent company mapping.
    Returns dict: {drug_name_lower: {"company": str, "geographies": [str]}}
    """
    if not DRUG_COMPANY_TABLE:
        print("[BQ] Missing DRUG_COMPANY_TABLE config — drug-company mapping unavailable")
        return {}

    try:
        client = _get_bq_client()
        fq_table = DRUG_COMPANY_TABLE

        # Same GLP-1 drug universe as the rest of the pipeline (drug_list.GLP1_DRUG_QUERY)
        _glp1_where = GLP1_DRUG_QUERY.strip().split("WHERE", 1)[1]
        query = f"""
        WITH base AS (
          SELECT DISTINCT
            cleaned_generic_name,
            Parent_Company_Name,
            Drug_Geography
          FROM `{fq_table}`
          WHERE {_glp1_where}
        )
        SELECT
          b.cleaned_generic_name,
          b.Parent_Company_Name,
          b.Drug_Geography
        FROM base AS b
        WHERE EXISTS (
          SELECT 1
          FROM UNNEST(REGEXP_EXTRACT_ALL(b.Drug_Geography, r'[^,;]+')) AS x
          WHERE TRIM(x) IN ('EU', 'United States', 'China', 'India', 'Brazil',
                             'Australia', 'Russia', 'Canada', 'Japan', 'Mexico',
                             'Taiwan', 'South Korea',
                             'CN', 'IN', 'BR', 'AU', 'RU', 'US', 'CA', 'JP',
                             'MX', 'TW', 'KR', 'EP')
        )
        ORDER BY b.cleaned_generic_name
        """

        print(f"[BQ] Querying drug-company mapping from {fq_table}...")
        df = client.query(query).to_dataframe()

        mapping = {}
        for _, row in df.iterrows():
            drug    = str(row.get("cleaned_generic_name", "")).strip()
            company = str(row.get("Parent_Company_Name", "")).strip()
            geo_raw = str(row.get("Drug_Geography", "")).strip()
            if drug and company and drug.lower() not in ("nan", "none", ""):
                key = drug.lower()
                if key not in mapping:
                    mapping[key] = {"company": company, "geographies": []}
                for geo in geo_raw.replace(";", ",").split(","):
                    geo = geo.strip()
                    if geo and geo not in mapping[key]["geographies"]:
                        mapping[key]["geographies"].append(geo)

        print(f"[BQ] Loaded {len(mapping)} drug→company mapping(s)")
        for d, info in sorted(mapping.items()):
            geos = ", ".join(info["geographies"][:5])
            print(f"  {d} → {info['company']} [{geos}]")

        return mapping

    except Exception as e:
        print(f"[BQ] Drug-company mapping query failed: {e}")
        return {}


# ─────────────────────────────────────────────
# 2. Load Step 2/3 from BQ (forecast_s3)
# ─────────────────────────────────────────────

def _load_step23_from_bq() -> Dict[str, pd.DataFrame]:
    """
    Reads Step 2/3 data from BQ table: forecast_s3.
    Groups rows by drug name and returns {drug_name: DataFrame}.
    """
    try:
        client = _get_bq_client()
        query = f"SELECT DISTINCT * FROM `{BQ_FORECAST_S3_TABLE}`"
        print(f"[STEP 2/3] Loading from {BQ_FORECAST_S3_TABLE}...")
        df = client.query(query).to_dataframe()

        if df.empty:
            print("[STEP 2/3] Table is empty")
            return {}

        # Identify the drug name column (try common names)
        drug_col = None
        for candidate in ["drug_name", "Drug", "drug", "cleaned_generic_name",
                          "Drug Name", "drug_name_clean", "generic_name"]:
            if candidate in df.columns:
                drug_col = candidate
                break

        if drug_col is None:
            # Fall back to first column
            drug_col = df.columns[0]
            print(f"[STEP 2/3] Drug column not found — using '{drug_col}'")

        drug_dfs = {}
        for drug_name, group_df in df.groupby(drug_col):
            name = str(drug_name).strip()
            if name and name.lower() not in ("nan", "none", ""):
                drug_dfs[name] = group_df.reset_index(drop=True)

        print(f"[STEP 2/3] Loaded {len(drug_dfs)} drug(s) from BQ")
        return drug_dfs

    except Exception as e:
        print(f"[STEP 2/3] Failed to load from BQ: {e}")
        return {}


# ─────────────────────────────────────────────
# 3. Load Step 4 from BQ (filing_pattern_table)
# ─────────────────────────────────────────────

def _load_step4_from_bq(drug_name: str) -> str:
    """
    Reads innovator filing pattern data from BQ for a specific drug.
    Returns a text summary.
    """
    try:
        client = _get_bq_client()
        query = f"""
        SELECT DISTINCT *
        FROM `{BQ_FILING_PATTERN_TABLE}`
        WHERE LOWER(drug_name) = LOWER(@drug)
           OR LOWER(drug_name) LIKE CONCAT('%%', LOWER(@drug), '%%')
        """
        job_config = _bq_job_config({"drug": drug_name})
        df = client.query(query, job_config=job_config).to_dataframe()

        if df.empty:
            return ""

        lines = [f"INNOVATOR FILING PATTERNS FOR {drug_name}:"]
        for _, row in df.iterrows():
            company = row.get("Company", row.get("company", "N/A"))
            char    = row.get("Characterization", row.get("characterization", "N/A"))
            conf    = row.get("Confidence", row.get("confidence", "N/A"))
            rat     = row.get("Rationale", row.get("rationale", "N/A"))
            lines.append(f"  {company}: {char} (confidence: {conf})")
            lines.append(f"    {rat}")

        result = "\n".join(lines)
        print(f"[STEP 4] Loaded filing patterns for {drug_name} from BQ ({len(result)} chars)")
        return result

    except Exception as e:
        print(f"[STEP 4] Failed to load from BQ for {drug_name}: {e}")
        return ""


def _load_all_step4_from_bq() -> Dict[str, str]:
    """Load ALL Step 4 data from BQ in one query, return {drug_name_lower: text}."""
    try:
        client = _get_bq_client()
        query = f"SELECT DISTINCT * FROM `{BQ_FILING_PATTERN_TABLE}`"
        print(f"[STEP 4] Loading all filing patterns from {BQ_FILING_PATTERN_TABLE}...")
        df = client.query(query).to_dataframe()

        if df.empty:
            print("[STEP 4] Table is empty")
            return {}

        # Identify drug column
        drug_col = None
        for candidate in ["drug_name", "Drug", "drug", "cleaned_generic_name"]:
            if candidate in df.columns:
                drug_col = candidate
                break
        if drug_col is None:
            drug_col = df.columns[0]

        result = {}
        for drug_name, group in df.groupby(drug_col):
            name = str(drug_name).strip()
            if not name or name.lower() in ("nan", "none", ""):
                continue

            lines = [f"INNOVATOR FILING PATTERNS FOR {name}:"]
            for _, row in group.iterrows():
                company = row.get("Company", row.get("company", "N/A"))
                char    = row.get("Characterization", row.get("characterization", "N/A"))
                conf    = row.get("Confidence", row.get("confidence", "N/A"))
                rat     = row.get("Rationale", row.get("rationale", "N/A"))
                lines.append(f"  {company}: {char} (confidence: {conf})")
                lines.append(f"    {rat}")

            result[name.lower()] = "\n".join(lines)

        print(f"[STEP 4] Loaded filing patterns for {len(result)} drug(s)")
        return result

    except Exception as e:
        print(f"[STEP 4] Failed to load all from BQ: {e}")
        return {}


# ─────────────────────────────────────────────
# 4. Load Step 5 from BQ (company_analysis_table)
# ─────────────────────────────────────────────

def _load_step5_from_bq(company_name: str) -> str:
    """
    Reads company analysis from BQ for a specific company.
    Returns a text summary.
    """
    if not company_name:
        return ""

    try:
        client = _get_bq_client()
        query = f"""
        SELECT DISTINCT *
        FROM `{BQ_COMPANY_ANALYSIS_TABLE}`
        WHERE LOWER(company_name) = LOWER(@company)
           OR LOWER(company_name) LIKE CONCAT('%%', LOWER(@company), '%%')
        """
        job_config = _bq_job_config({"company": company_name})
        df = client.query(query, job_config=job_config).to_dataframe()

        if df.empty:
            print(f"[STEP 5] No company analysis found in BQ for '{company_name}'")
            return ""

        lines = [f"BUSINESS STRATEGY ASSESSMENT FOR {company_name}:"]
        for _, row in df.iterrows():
            section = row.get("Section", row.get("section", ""))
            rating  = row.get("Rating", row.get("rating", ""))
            details = row.get("Details", row.get("details", ""))
            lines.append(f"  {section}: {rating}")
            if details and str(details).strip() not in ("", "nan", "N/A"):
                lines.append(f"    {str(details)[:500]}")

        result = "\n".join(lines)
        print(f"[STEP 5] Loaded company analysis for {company_name} from BQ ({len(result)} chars)")
        return result

    except Exception as e:
        print(f"[STEP 5] Failed to load from BQ for {company_name}: {e}")
        return ""


def _load_all_step5_from_bq() -> Dict[str, str]:
    """Load ALL Step 5 data from BQ in one query, return {company_name_lower: text}."""
    try:
        client = _get_bq_client()
        query = f"SELECT DISTINCT * FROM `{BQ_COMPANY_ANALYSIS_TABLE}`"
        print(f"[STEP 5] Loading all company analyses from {BQ_COMPANY_ANALYSIS_TABLE}...")
        df = client.query(query).to_dataframe()

        if df.empty:
            print("[STEP 5] Table is empty")
            return {}

        # Identify company column
        comp_col = None
        for candidate in ["company_name", "Company", "company", "Parent_Company_Name"]:
            if candidate in df.columns:
                comp_col = candidate
                break
        if comp_col is None:
            comp_col = df.columns[0]

        result = {}
        for company, group in df.groupby(comp_col):
            name = str(company).strip()
            if not name or name.lower() in ("nan", "none", ""):
                continue

            lines = [f"BUSINESS STRATEGY ASSESSMENT FOR {name}:"]
            for _, row in group.iterrows():
                section = row.get("Section", row.get("section", ""))
                rating  = row.get("Rating", row.get("rating", ""))
                details = row.get("Details", row.get("details", ""))
                lines.append(f"  {section}: {rating}")
                if details and str(details).strip() not in ("", "nan", "N/A"):
                    lines.append(f"    {str(details)[:500]}")

            result[name.lower()] = "\n".join(lines)

        print(f"[STEP 5] Loaded company analyses for {len(result)} company(ies)")
        return result

    except Exception as e:
        print(f"[STEP 5] Failed to load all from BQ: {e}")
        return {}


# ─────────────────────────────────────────────
# BQ parameterized query helper
# ─────────────────────────────────────────────

def _bq_job_config(params: Dict[str, str]):
    from google.cloud import bigquery
    query_params = []
    for name, value in params.items():
        query_params.append(
            bigquery.ScalarQueryParameter(name, "STRING", value)
        )
    job_config = bigquery.QueryJobConfig(query_parameters=query_params)
    return job_config
