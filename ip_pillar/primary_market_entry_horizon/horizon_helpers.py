"""
horizon_helpers.py
──────────────────
The helpers primary_market_entry_horizon() calls (kept out of primary_market_entry_horizon.py, which holds only
that function): jurisdiction / CSV helpers, the glp_patent_loe / glp_master_loe publisher, the forecast
step and the two PDF reports.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .. import config
from ..gemini_utils import isolated_gemini_call_stats
from .bq_utils import fetch_drug_details_df, load_forecasted_loe_table, publish_table
from .score_calculation import patents_to_dataframe, run_merge
from .score_calculation.merge_forecasted_loe import is_marketed_or_prereg, load_forecasted_loe
from .score_calculation.phase_fetcher import canonicalise_drug_name

logger = logging.getLogger(__name__)


def marketed_in(patents: List[Dict], jurisdiction: str) -> bool:
    return any(
        p.get("phase_at_filing") == "Marketed" and (p.get("jurisdiction") or "").upper() == jurisdiction
        for p in patents
    )


def split_csv(value) -> List[str]:
    return [v.strip() for v in str(value or "").split(",") if v.strip()]


def publish_loe_tables(patents: List[Dict], drug_name: str, run_ts: str, only: Optional[List[str]] = None) -> Dict[str, Dict]:
    """Steps 9 and 11: build glp_patent_loe and glp_master_loe for this drug (or just the tables in
    *only*) and publish each to BigQuery + a JSON snapshot. A failure in one table does not stop the other."""
    builders = {
        config.BQ_PATENT_LOE_TABLE: lambda: patents_to_dataframe(patents, drug_name),
        config.BQ_MASTER_LOE_TABLE: lambda: run_merge(patents, drug_name),
    }
    builders = {table_id: build for table_id, build in builders.items() if only is None or table_id in only}
    # When this drug's scores were calculated: ISO 8601, microseconds, UTC.
    # One value for the drug, shared by both tables.
    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    outputs: Dict[str, Dict] = {}
    for table_id, build in builders.items():
        logger.info("[HORIZON] Building %s ...", table_id)
        try:
            table = build()
            table["updated_at"] = updated_at
            outputs[table_id] = publish_table(table, table_id, drug_name, run_ts)
        except Exception:
            logger.exception("[HORIZON] Could not build %s for %r", table_id, drug_name)
            outputs[table_id] = {"json_uri": None, "bq_table": None}
    return outputs


async def run_forecast(drug_name: str) -> Dict:
    """Step 10: forecast for the drug, then drop the cached forecasted_loe so the glp_master_loe
    merge sees the rows it just wrote. With SKIP_FORECAST (default) a drug that already has forecasted_loe rows
    is not re-forecast. {"error": ...} if it fails, {"skipped": reason} if it is not run:
    switched off, or the drug is Marketed / Pre-registration (the merge drops its forecasted rows)."""
    if not config.RUN_FORECAST:
        logger.info("[HORIZON] RUN_FORECAST is off - skipping forecast")
        return {"skipped": "RUN_FORECAST is off"}
    if not config.NO_MARKETED_SKIP:
        details = await asyncio.to_thread(fetch_drug_details_df, canonicalise_drug_name(drug_name))
        if is_marketed_or_prereg(details):
            logger.info("[HORIZON] %r is Marketed / Pre-registration - skipping forecast", drug_name)
            return {"skipped": "Marketed / Pre-registration"}
    if config.SKIP_FORECAST and not config.REINDEX:
        # Read fresh: the cached table may predate rows another run wrote.
        load_forecasted_loe_table.cache_clear()
        existing = await asyncio.to_thread(load_forecasted_loe, drug_name)
        if len(existing):
            logger.info("[HORIZON] SKIP_FORECAST: %r already has %d forecasted_loe row(s) - reusing them",
                        drug_name, len(existing))
            return {"skipped": f"SKIP_FORECAST: {len(existing)} existing forecasted_loe row(s) reused"}
        logger.info("[HORIZON] SKIP_FORECAST: %r has no forecasted_loe rows - running forecast", drug_name)
    # Imported here: the forecast package itself uses this package's approval-date fetcher.
    from .forecast import forecast

    logger.info("[HORIZON] Running forecast ...")
    try:
        # forecast resets and saves the Gemini counters itself; keep this dimension's.
        with isolated_gemini_call_stats():
            result = await forecast(drug_name, reset=config.REINDEX)
    except Exception as e:
        logger.exception("[HORIZON] forecast failed for %r", drug_name)
        return {"error": str(e)}
    load_forecasted_loe_table.cache_clear()
    if result.get("error"):
        logger.error("[HORIZON] forecast: %s", result["error"])
    return result


def generate_report(drug_name: str) -> Dict:
    """Build and upload the dimension's report; {"error": ...} if it fails."""
    # Imported here so reportlab is only needed when a report is built.
    from .generate_report_and_rationale.generate_report import report_for_drug

    logger.info("[HORIZON] Generating the %s report ...", config.DIMENSION_PRIMARY_MARKET_ENTRY_HORIZON)
    try:
        report = report_for_drug(drug_name)
    except Exception as e:
        logger.exception("[HORIZON] Report failed for %r", drug_name)
        return {"error": str(e), "gcs_uri": None, "archive_uri": None}
    logger.info("[HORIZON] Report -> %s", report["gcs_uri"])
    return report


def generate_blocking_report(drug_name: str, patents: List[Dict]) -> Dict:
    """Build and upload the Blocking Analysis report; {"error": ...} if it fails."""
    # Imported here so reportlab is only needed when a report is built.
    from .blocking_analysis.generate_report import report_for_drug

    logger.info("[HORIZON] Generating the %s report ...", config.DIMENSION_BLOCKING_ANALYSIS)
    try:
        report = report_for_drug(drug_name, patents)
    except Exception as e:
        logger.exception("[HORIZON] Blocking Analysis report failed for %r", drug_name)
        return {"error": str(e), "gcs_uri": None, "archive_uri": None}
    logger.info("[HORIZON] Blocking Analysis report -> %s", report["gcs_uri"])
    return report
