"""
constants.py
────────────
Scoring constants for the patent strength dimension: country weights, sub-factor
weights and labels, and the section keyword lists used to re-rank chunks.

Infrastructure settings (BigQuery tables, Gemini model, GCS) live in
IP_refactored.config like every other dimension.
"""

DIMENSION_NAME = "Patent Strength"

# ── Concurrency / retrieval ───────────────────────────────────────────────────
TOP_K_CHUNKS = 12          # Hard cap on chunks fetched per patent
PATENT_CONCURRENCY = 8     # How many Gemini calls run at once
EXECUTOR_MAX_WORKERS = 16  # Threads for blocking Gemini / AlloyDB calls

# ── Checkpointing (GCS, one file per drug) ────────────────────────────────────
CHECKPOINT_SUBFOLDER = "ipd2_checkpoints"
CHECKPOINT_GCS_FILE = "scoring_checkpoint.json"
MIN_GCS_WRITE_INTERVAL = 10.0  # seconds between GCS checkpoint writes

# ── Country weights ───────────────────────────────────────────────────────────
COUNTRY_WEIGHTS = {
    "US": 0.25,
    "EP": 0.25,
    "CN": 0.05,
    "CA": 0.05,
    "AU": 0.05,
    "KR": 0.05,
    "IN": 0.05,
    "BR": 0.05,
    "MX": 0.05,
    "TW": 0.05,
    "JP": 0.05,
    "RU": 0.05,
}

JURISDICTION_LABELS = {
    "US": "United States",
    "EP": "European Union (EP)",
    "CN": "China",
    "CA": "Canada",
    "AU": "Australia",
    "KR": "South Korea",
    "IN": "India",
    "BR": "Brazil",
    "MX": "Mexico",
    "TW": "Taiwan",
    "JP": "Japan",
    "RU": "Russia",
}

# ── Sub-factors ───────────────────────────────────────────────────────────────
ACTIVE_SFS = ("sf1", "sf2", "sf3", "sf4")

SF_WEIGHTS = {"sf1": 0.4, "sf2": 0.3, "sf3": 0.2, "sf4": 0.1}

SF_DISPLAY_NAMES = {
    "sf1": "SF1 (Novelty & Non-Obviousness)",
    "sf2": "SF2 (Obvious-to-Combine Risk)",
    "sf3": "SF3 (Prosecution History Vulnerability)",
    "sf4": "SF4 (Secondary Considerations)",
}

SF1_SCORE_LABELS = {
    1: "Truly Novel Concept",
    2: "Significant Modification in Known Technical Area",
    3: "Derivative but Non-Trivial",
    4: "Derivative of Known Class with Predictable Properties",
    5: "Minor Predictable Variation of Known Prior Art",
}

SF2_SCORE_LABELS = {
    1: "No Clear Motivation or Unexpected Result",
    2: "Limited Motivation Due to Technical Challenges",
    3: "Some Motivation but Outcome Uncertain",
    4: "Clear Motivation to Combine with Reasonable Expectation",
    5: "Combination Widely Known and Predictable",
}

SF3_SCORE_LABELS = {
    1: "Claims Granted Largely as Filed",
    2: "Minor Claim Amendments",
    3: "Moderate Amendments and Arguable Vulnerabilities",
    4: "Meaningful Claim Narrowing to Overcome Prior Art",
    5: "Significant Narrowing Amendments Creating Strong Estoppel Risk",
}

SF4_SCORE_LABELS = {
    1: "Clear Unexpected Results Supported by Data",
    2: "Strong Technical Improvement",
    3: "Moderate Improvement",
    4: "Weak or Unsupported Advantages",
    5: "No Evidence of Unexpected Results",
}

SF_SCORE_LABELS = {
    "sf1": SF1_SCORE_LABELS,
    "sf2": SF2_SCORE_LABELS,
    "sf3": SF3_SCORE_LABELS,
    "sf4": SF4_SCORE_LABELS,
}

SF1_SECTIONS = [
    "independent claim", "claim 1", "background", "prior art",
    "summary of invention", "example", "field of invention",
]

SF2_SECTIONS = [
    "background", "state of the art", "references cited",
    "summary of invention", "technical problem", "prior art",
    "combination", "known", "disclosure", "independent claim",
    "claim 1", "motivation",
]

SF3_SECTIONS = [
    "independent claim", "claim 1", "claims", "amendment",
    "prosecution", "rejection", "office action", "response",
    "examiner", "prior art", "narrowing", "limitation",
    "background", "abstract",
]

SF4_SECTIONS = [
    "example", "experimental", "comparative", "data", "table",
    "result", "stability", "bioavailability", "efficacy",
    "toxicity", "improvement", "advantage", "unexpected",
    "synergy", "in vivo", "in vitro",
]
