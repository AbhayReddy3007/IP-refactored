"""Prompts for patent litigation analysis."""

BRAND_AND_INNOVATOR_PROMPT = """Find the brand names and the innovator (original patent holder) for this drug: {drug_name}

Return ONLY valid JSON, with no other text:
{{"brand_names": ["Brand1", "Brand2"], "innovator": "Company name"}}

If something cannot be determined, use an empty list or an empty string. Do not guess."""


INNOVATOR_PROMPT = """Find the innovator (original patent holder) for this drug: {drug_name}

Return ONLY valid JSON, with no other text:
{{"innovator": "Company name"}}

If it cannot be determined, use an empty string. Do not guess."""


LITIGATION_SEARCH_PROMPT = """Find all patent litigation and court cases for the following:

Drug: {drug_name}
Innovator: {innovator}
Patent Numbers:
{patent_numbers}

Search for:
- ANDA Paragraph IV patent challenges (US)
- Inter Partes Review (IPR) at PTAB
- European Patent Office (EPO) oppositions
- India patent litigation (Delhi High Court)
- Compounding pharmacy lawsuits

Return ONLY a valid JSON array, with no other text:
[
  {{
    "patent_number": "Copy EXACTLY as written in the provided list",
    "case_number": "Case number if available, else empty string",
    "court": "Court or tribunal name",
    "case_type": "ANDA|IPR|EPO|India|Compounding",
    "status": "Pending|Settled|Decided|Invalidated|Upheld",
    "outcome": "Brief outcome",
    "challenger": "Company challenging the patent (see rules)",
    "source_url": "URL of the source where this case was found"
  }}
]

Rules:
- Return a flat list with zero, one, or many rows per patent.
- Every row must include a patent_number copied exactly from the provided list.
- Do not include rows for patents that are not in the provided list.
- case_type must be exactly one of: ANDA, IPR, EPO, India, Compounding.
- challenger means: the generic/ANDA filer (ANDA), the petitioner (IPR), the opponent (EPO),
  the challenger or defendant company (India), the compounding pharmacy that was sued (Compounding).
- Only include cases you actually found in a source. Use an empty string for any field you cannot
  confirm. Never invent case numbers, courts, or companies.

If no cases are found, return: []"""
