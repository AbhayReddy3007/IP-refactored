"""
constants.py
────────────
Gemini model, BigQuery client and table names, patent-category definitions.
"""

from .... import config, gcp_utils


# ─────────────────────────────────────────────
# Gemini client
# ─────────────────────────────────────────────

_GEMINI_MODEL = config.GEMINI_TEXT_MODEL


# ─────────────────────────────────────────────
# BigQuery client & table names
# ─────────────────────────────────────────────

class _LazyBQClient:
    """The shared BigQuery client, created on first use so importing this module needs no credentials."""
    def __getattr__(self, name):
        return getattr(gcp_utils.get_bq_client(), name)


_BQ_CLIENT = _LazyBQClient()

_CLINICAL_TABLE = config.CLINICAL_EFFICACY_TABLE


# ─────────────────────────────────────────────
# Category definitions
# ─────────────────────────────────────────────

_FORECAST_COLS = ["API Patent", "Formulation", "Device", "Method", "Dosing", "Combination"]

_CATEGORY_DESCRIPTIONS = {
    "API Patent":   "Composition of Matter — the core molecule itself (the active pharmaceutical ingredient)",
    "Formulation":  "Formulation — the physical form, excipients, delivery system, or stability of the drug product",
    "Device":       "Device — the delivery device such as auto-injector, pen, or inhaler",
    "Method":       "Method of Treatment — a method of treating a disease or condition using the drug",
    "Dosing":       "Dosage Regimen — a specific dosing schedule, frequency, titration, or amount",
    "Combination":  "Combination — combining this drug with other metabolic agents (e.g. insulin, SGLT2 inhibitors, statins, other GLP-1 agonists) for therapeutic effect",
}
