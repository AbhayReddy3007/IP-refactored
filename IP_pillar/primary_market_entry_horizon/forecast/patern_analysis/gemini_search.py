"""
gemini_search.py
────────────────
Gemini call with Google Search grounding and JSON parsing.
"""

from ....gemini_utils import gemini_generate_async
from .constants import _GEMINI_MODEL


# ─────────────────────────────────────────────
# Gemini call helpers (inlined from test_innovator_filing_patterns)
# ─────────────────────────────────────────────

async def _gemini_call_with_search(prompt: str) -> str:
    """Gemini call with Google Search grounding (gemini_utils retries with back-off)."""
    try:
        response = await gemini_generate_async(prompt, _GEMINI_MODEL, google_search=True)
    except Exception as e:
        print(f"  Gemini API error: {e}")
        raise
    return (response.text or "").strip()


def _parse_json_response(response: str) -> dict:
    """Parse JSON from Gemini response, tolerating markdown fences and preambles."""
    if not response:
        return {}
    import json
    response = response.strip()
    if "```json" in response:
        response = response.split("```json")[1].split("```")[0].strip()
    elif "```" in response:
        response = response.split("```")[1].split("```")[0].strip()
    json_start = response.find('{')
    if json_start > 0:
        response = response[json_start:]
    try:
        return json.loads(response)
    except json.JSONDecodeError as e:
        print(f"  JSON parse error: {e}")
        print(f"  Response preview: {response[:300]}...")
        return {}
