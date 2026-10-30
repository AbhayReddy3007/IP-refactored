"""JSON helpers for parsing Gemini output (stdlib only)."""

import json
import logging
import re

logger = logging.getLogger(__name__)


def extract_json_from_response(text: str) -> str:
    """Extract the first JSON object or array from a Gemini response string."""
    if not text or not text.strip():
        return ""

    text = text.strip()

    # Strip markdown code fences
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()

    obj_start = text.find("{")
    arr_start = text.find("[")

    if obj_start == -1 and arr_start == -1:
        return ""
    elif obj_start == -1:
        start, opening, closing = arr_start, "[", "]"
    elif arr_start == -1:
        start, opening, closing = obj_start, "{", "}"
    else:
        start = min(obj_start, arr_start)
        opening, closing = ("{", "}") if start == obj_start else ("[", "]")

    depth = 0
    in_string = False
    escape_next = False

    for i in range(start, len(text)):
        char = text[i]
        if escape_next:
            escape_next = False
            continue
        if char == "\\":
            escape_next = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if not in_string:
            if char == opening:
                depth += 1
            elif char == closing:
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]

    # Unbalanced: return the remainder so the repair step can try to salvage it
    return text[start:]


def safe_parse_json(text: str):
    """Parse a JSON string, tolerating common Gemini output quirks.

    Handles trailing commas, single-quoted JSON, and truncated arrays.
    Returns the parsed object (dict/list) or None on failure.
    """
    if not text or not text.strip():
        return None

    # 1. Straight parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. Remove trailing commas before } or ]
    cleaned = re.sub(r",\s*([}\]])", r"\1", text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 3. Single quotes -> double quotes, only when the text has no double
    #    quotes at all (otherwise apostrophes inside strings get corrupted)
    if '"' not in cleaned:
        try:
            return json.loads(cleaned.replace("'", '"'))
        except json.JSONDecodeError:
            pass

    # 4. Truncated array of objects: keep every complete object, close the array
    stripped = cleaned.lstrip()
    if stripped.startswith("["):
        last_close = stripped.rfind("}")
        while last_close != -1:
            candidate = stripped[: last_close + 1].rstrip().rstrip(",") + "]"
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                last_close = stripped.rfind("}", 0, last_close)

    # 5. Truncated single object: try closing it
    if stripped.startswith("{"):
        for suffix in ("}", "]}", "}]}"):
            try:
                return json.loads(cleaned + suffix)
            except json.JSONDecodeError:
                continue

    logger.warning("Could not parse JSON (first 200 chars): %s", text[:200])
    return None
