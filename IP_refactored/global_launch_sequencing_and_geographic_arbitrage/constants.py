"""
constants.py
────────────
Analysis constants for the global launch sequencing and geographic arbitrage dimension.

Infrastructure settings (BigQuery tables, Gemini model, the dimension's name) live in
IP_refactored.config like every other dimension.
"""

# ── Shortlist filters ────────────────────────────────────────────────────────
TARGET_JURISDICTIONS = {"CN", "IN", "BR", "AU", "RU", "US", "CA", "JP", "MX", "TW", "KR"}
KEEP_CLAIM_CATEGORIES = {"Composition Of Matter", "Device", "Formulation"}

KEEP_COLUMNS = [
    "Drug Name", "Patent Number", "Jurisdiction", "Tag",
    "Grant Date", "Filing Date", "Approval Date",
    "PTE (months)", "Step 1 Claim Category", "Years to Entry", "Type",
]

# Columns of the shortlisted patents table, in order.
FINAL_COLUMNS = [
    "Drug Name", "Patent Number", "Jurisdiction", "Tag",
    "Step 1 Claim Category",
    "Filing Date", "Grant Date", "Approval Date",
    "Base Patent Term (Filing+20y)",
    "PTE Status", "PTE Months (Granted)", "PTE Source",
    "Adjusted Expiry (with PTE)", "PTE Capped (Post-Approval Cap)",
    "Expiry Gap (Years)",
    "Dimension IV Score", "Dimension IV Rating",
]

# Concurrent Gemini lookups (approval dates, PTE).
LOOKUP_CONCURRENCY = 4

# ── Regulatory / PTE rules ───────────────────────────────────────────────────
JURISDICTION_FULL = {
    "CN": "China (NMPA / formerly CFDA)",
    "IN": "India (CDSCO / DCGI)",
    "BR": "Brazil (ANVISA)",
    "AU": "Australia (TGA)",
    "RU": "Russia (Ministry of Health / Roszdravnadzor)",
    "US": "United States (FDA)",
    "JP": "Japan (PMDA / MHLW)",
    "KR": "South Korea (MFDS)",
    "TW": "Taiwan (TFDA)",
    "CA": "Canada (Health Canada)",
    "MX": "Mexico (COFEPRIS)",
}

PTE_RULES = {
    "IN": {"allowed": False},
    "BR": {"allowed": False},
    "CN": {"allowed": True, "max_ext_years": 5, "max_effective_from_approval_years": 14},
    "AU": {"allowed": True, "max_ext_years": 5},
    "RU": {"allowed": True, "max_ext_years": 5},
    "US": {"allowed": True, "max_ext_years": 5},
    "JP": {"allowed": True, "max_ext_years": 5},
    "KR": {"allowed": True, "max_ext_years": 5, "max_effective_from_approval_years": 14},
    "TW": {"allowed": True, "max_ext_years": 5},
    "CA": {"allowed": True, "max_ext_years": 2, "post_expiry": True},
    "MX": {"allowed": False, "note": "USMCA mandates PTE but implementing regulations incomplete"},
}

PTE_REGISTRY_HINTS = {
    "CN": "CNIPA patent register",
    "AU": "AusPat (IP Australia)",
    "RU": "Rospatent / FIPS register",
    "US": "USPTO Orange Book / Patent Center",
    "JP": "JPO (Japan Patent Office) register",
    "KR": "KIPO (Korean Intellectual Property Office) register",
    "TW": "TIPO (Taiwan Intellectual Property Office) register",
    "CA": "Health Canada CSP (Certificate of Supplementary Protection) Registry",
    "MX": "IMPI records / court rulings",
}
