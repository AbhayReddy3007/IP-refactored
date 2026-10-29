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

# Regulatory exclusivity rules quoted in the PTE report, by jurisdiction code.
EXCLUSIVITY_RULES = {
    "US": "5-yr NCE / 3-yr supplemental exclusivity (Hatch-Waxman); PTE up to 5 yrs (max 14 yrs from approval)",
    "EU": "8+2+1 yr data/market exclusivity; SPC up to 5 yrs extra",
    "CN": "6-yr data exclusivity (innovative drugs); PTE up to 5 yrs (effective patent term capped at 14 yrs from approval)",
    "IN": "No PTE; no data exclusivity under current law",
    "BR": "No PTE; pipeline patents allowed (Art. 230 now struck down); 5-yr data exclusivity for agrochemicals",
    "AU": "5-yr data exclusivity; PTE up to 5 yrs (15-yr effective cap from first TGA approval)",
    "RU": "6-yr data exclusivity; PTE up to 5 yrs",
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
