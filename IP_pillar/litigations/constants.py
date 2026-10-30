"""
constants.py
────────────
Analysis constants for the litigation dimension.

Infrastructure settings (BigQuery table, Gemini model, the dimension's name) live in
IP_pillar.config like every other dimension.
"""

# Patents searched per Gemini call. A smaller group means a shorter reply that is less likely to be truncated.
PATENTS_PER_LLM_CALL = 5

# How many patent groups are searched at once.
PATENT_GROUP_CONCURRENCY = 4

# Sampling temperature of the two Gemini calls (both use Google Search grounding).
BRAND_LOOKUP_TEMPERATURE = 0.1
LITIGATION_SEARCH_TEMPERATURE = 0.1

# Case types the search prompt asks for; anything else Gemini returns is mapped by CASE_TYPE_ALIASES or becomes "Other".
CASE_TYPES = ("ANDA", "IPR", "EPO", "India", "Compounding")
CASE_TYPE_ALIASES = {
    "anda": "ANDA",
    "paragraph iv": "ANDA",
    "para iv": "ANDA",
    "hatch-waxman": "ANDA",
    "ipr": "IPR",
    "inter partes review": "IPR",
    "ptab": "IPR",
    "epo": "EPO",
    "opposition": "EPO",
    "india": "India",
    "delhi high court": "India",
    "compounding": "Compounding",
}
OTHER_CASE_TYPE = "Other"
