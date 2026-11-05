"""
data_loading.py
───────────────
Loads glp_patent_loe and clinical_efficacy from BigQuery.
"""

from typing import Optional
import pandas as pd
from .... import config
from ....drug_list import drug_list
from ..bq_utils import load_patent_loe
from .constants import _BQ_CLIENT, _CLINICAL_TABLE


# ─────────────────────────────────────────────
# Load data from BigQuery (replaces load_excel)
# ─────────────────────────────────────────────

def load_loe_table(drug_filter: Optional[set] = None) -> pd.DataFrame:
    """Loads the patent rows from glp_patent_loe (the blocking-analysis output) for the drugs in
    *drug_filter*, or for every GLP-1 drug when it is None. Columns use the friendly names
    ('Drug Name', 'Step 1 Claim Category', ...) the rest of the pipeline expects."""
    try:
        drugs = sorted(drug_filter) if drug_filter else drug_list()
        frames = [f for f in (load_patent_loe(d) for d in drugs) if not f.empty]
        if not frames:
            print(f"[INPUT 1] No rows in {config.BQ_PATENT_LOE_TABLE} for {drugs}")
            return pd.DataFrame()
        df = pd.concat(frames, ignore_index=True)
        df.columns = [c.strip() for c in df.columns]
        print(f"[INPUT 1] Loaded {len(df)} row(s) from: {config.BQ_PATENT_LOE_TABLE}")

        # ── Deduplicate ───────────────────────────────────────────────────
        # loe_table is written with `if_exists="append"` (see 2bq.py), so
        # every pipeline run for a drug appends a fresh copy of every patent
        # row. Over multiple runs this accumulates duplicates and silently
        # inflates downstream calculations. Drop duplicates on the natural
        # key (Drug Name, Patent Number, Jurisdiction). keep='last' favours
        # whichever row pandas received last from BigQuery — that ordering
        # isn't strictly deterministic without ORDER BY in the SQL, but it
        # is consistent within a single load and definitively better than
        # double-counting. A timestamp column on the table would let us
        # pick "newest" properly; without one this is the safe fix.
        dedupe_keys = [k for k in ("Drug Name", "Patent Number", "Jurisdiction") if k in df.columns]
        if dedupe_keys:
            before = len(df)
            df = df.drop_duplicates(subset=dedupe_keys, keep="last").reset_index(drop=True)
            removed = before - len(df)
            if removed:
                print(
                    f"[INPUT 1] Dropped {removed} duplicate row(s) on "
                    f"{dedupe_keys} (kept last); {len(df)} unique row(s) remain."
                )
            else:
                print(f"[INPUT 1] No duplicates found on {dedupe_keys}.")
        else:
            print(
                f"[INPUT 1] Skipping dedupe — none of "
                f"['Drug Name','Patent Number','Jurisdiction'] present in columns."
            )

        print(f"[INPUT 1] Renamed columns: {list(df.columns)}\n")
        return df
    except Exception as e:
        print(f"[INPUT 1] Failed to load {config.BQ_PATENT_LOE_TABLE}: {e}")
        return pd.DataFrame()


def load_clinical_efficacy() -> pd.DataFrame:
    """Loads phase/clinical data from BigQuery clinical_efficacy table."""
    query = f"""
        SELECT molecule_name, phase, trial_start_date
        FROM `{_CLINICAL_TABLE}`
        WHERE molecule_name IS NOT NULL
          AND phase IS NOT NULL
    """
    try:
        df = _BQ_CLIENT.query(query).to_dataframe()
        df.columns = [c.strip() for c in df.columns]
        # Rename to standard column names used throughout the pipeline
        df = df.rename(columns={
            "molecule_name":    "Molecule",
            "phase":            "Phase",
            "trial_start_date": "Start Date",
        })
        print(f"[INPUT 2] Loaded {len(df)} row(s) from: {_CLINICAL_TABLE}")
        print(f"[INPUT 2] Columns: {list(df.columns)}\n")
        return df
    except Exception as e:
        print(f"[INPUT 2] Failed to load clinical_efficacy: {e}")
        return pd.DataFrame()
