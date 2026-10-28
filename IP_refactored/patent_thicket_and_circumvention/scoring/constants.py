"""
constants.py
────────────
Analysis constants for the patent thicket / circumvention dimension.

Infrastructure settings (BigQuery tables, Gemini model, GCS, the dimension's name)
live in IP_refactored.config like every other dimension.
"""

# Categories left out of the thicket count (a composition-of-matter patent is a blocking patent, not a fence).
EXCLUDED_CATEGORIES = {"Composition Of Matter"}

# Patents sent to the circumvention analysis, per claim category.
MAX_PATENTS_PER_CATEGORY = 10

# How many claim categories are analysed at once.
CATEGORY_CONCURRENCY = 8

# Per-drug cache of finished circumvention results (GCS), so a re-run does not repeat Gemini calls.
CHECKPOINT_SUBFOLDER = "ipd3_checkpoints"
CHECKPOINT_FILE = "circumvention.json"
