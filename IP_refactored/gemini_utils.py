"""Shared Gemini API transport helpers.

Every Gemini call in the pipeline goes through this module (Vertex AI, using
Application Default Credentials and config.PROJECT_ID), so each one gets the
same retry / flex->standard fallback and is counted in the per-run usage stats
(get_gemini_call_stats -> bq_utils.append_gemini_stats_to_bigquery).

    gemini_generate(contents, model, ...)        -> GenerateContentResponse   (sync)
    gemini_generate_async(contents, model, ...)  -> GenerateContentResponse   (for async code)
    gemini_api_call(prompt, model, ...)          -> str                       (text in, text out; optional PDFs)
    gemini_embed(texts, model, ...)              -> list of embedding vectors
    gemini_embed_async(texts, model, ...)
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from functools import lru_cache

from google import genai
from google.genai import types

from . import config

logger = logging.getLogger(__name__)

_stats_lock = threading.Lock()
gemini_call_stats: dict[tuple[str, str | None], dict[str, object]] = {}


def _new_model_stats() -> dict[str, object]:
    return {
        "service_tier": None,
        "input_token_count": 0,
        "output_token_count": 0,
        "thought_token_count": 0,
        "tool_token_count": 0,
        "total_token_count": 0,
        "total_num_of_calls": 0,
        "num_of_search_enabled_calls": 0,
        "num_of_search_queries": 0,
    }


def reset_gemini_call_stats() -> None:
    """Reset usage counters before a new dimension-molecule run."""
    with _stats_lock:
        gemini_call_stats.clear()


def get_gemini_call_stats() -> list[dict[str, object]]:
    """Return one usage snapshot per model for the current run."""
    with _stats_lock:
        return [
            {"model_used": model, **stats}
            for (model, _service_tier), stats in sorted(
                gemini_call_stats.items(),
                key=lambda item: (item[0][0], item[0][1] or ""),
            )
        ]


def _get_model_stats(model: str, service_tier: str | None) -> dict[str, object]:
    return gemini_call_stats.setdefault(
        (model, service_tier),
        {**_new_model_stats(), "service_tier": service_tier},
    )


def _record_gemini_response_stats(
    response,
    model: str,
    requested_service_tier: str | None,
    google_search: bool,
) -> None:
    sdk_http_response = getattr(response, "sdk_http_response", None)
    headers = getattr(sdk_http_response, "headers", None)
    actual_tier = headers.get("x-gemini-service-tier") if headers else None
    usage = getattr(response, "usage_metadata", None)
    traffic_type = getattr(usage, "traffic_type", None)
    traffic_type_value = getattr(traffic_type, "value", traffic_type)
    if traffic_type_value == "ON_DEMAND_FLEX":
        actual_tier = "flex"
    elif traffic_type_value == "ON_DEMAND_STANDARD":
        actual_tier = "standard"
    input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
    candidate_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
    thought_tokens = int(getattr(usage, "thoughts_token_count", 0) or 0)
    tool_tokens = int(getattr(usage, "tool_use_prompt_token_count", 0) or 0)
    total_tokens = int(getattr(usage, "total_token_count", 0) or 0)
    search_query_values: set[str] = set()
    for candidate in getattr(response, "candidates", None) or []:
        grounding_metadata = getattr(candidate, "grounding_metadata", None)
        for query in getattr(grounding_metadata, "web_search_queries", None) or []:
            if isinstance(query, str) and query.strip():
                search_query_values.add(query.strip())
    search_queries = len(search_query_values)

    service_tier = actual_tier or requested_service_tier
    with _stats_lock:
        stats = _get_model_stats(model, service_tier)
        stats["total_num_of_calls"] = int(stats["total_num_of_calls"]) + 1
        stats["num_of_search_enabled_calls"] = int(stats["num_of_search_enabled_calls"]) + int(
            google_search
        )
        stats["input_token_count"] = int(stats["input_token_count"]) + input_tokens
        stats["output_token_count"] = int(stats["output_token_count"]) + candidate_tokens
        stats["thought_token_count"] = int(stats["thought_token_count"]) + thought_tokens
        stats["tool_token_count"] = int(stats["tool_token_count"]) + tool_tokens
        stats["total_token_count"] = int(stats["total_token_count"]) + total_tokens
        stats["num_of_search_queries"] = int(stats["num_of_search_queries"]) + search_queries


def _record_embedding_stats(response, model: str) -> None:
    """Count an embed_content call: one call, with the input tokens the API reports."""
    input_tokens = 0
    for embedding in getattr(response, "embeddings", None) or []:
        statistics = getattr(embedding, "statistics", None)
        input_tokens += int(getattr(statistics, "token_count", 0) or 0)
    with _stats_lock:
        stats = _get_model_stats(model, "standard")
        stats["total_num_of_calls"] = int(stats["total_num_of_calls"]) + 1
        stats["input_token_count"] = int(stats["input_token_count"]) + input_tokens
        stats["total_token_count"] = int(stats["total_token_count"]) + input_tokens


MAX_RETRIES = 5
INITIAL_BACKOFF = 4.0
REQUEST_TIMEOUT_MS = 900000


@lru_cache(maxsize=None)
def _client(request_mode: str | None, location: str) -> genai.Client:
    """One Vertex AI client per (request mode, location), reused across calls."""
    headers = (
        {
            "X-Vertex-AI-LLM-Request-Type": "shared",
            "X-Vertex-AI-LLM-Shared-Request-Type": request_mode,
        }
        if request_mode
        else {}
    )
    return genai.Client(
        vertexai=True,
        project=config.PROJECT_ID,
        location=location,
        http_options=types.HttpOptions(
            api_version="v1",
            headers=headers or None,
            timeout=REQUEST_TIMEOUT_MS,
        ),
    )


def _has_google_search(tools: list | None) -> bool:
    return any(getattr(tool, "google_search", None) is not None for tool in tools or [])


def gemini_generate(
    contents,
    model: str,
    temperature: float | None = None,
    max_output_tokens: int | None = None,
    google_search: bool = False,
    tools: list[types.Tool] | None = None,
    response_mime_type: str | None = None,
    system_instruction: str | None = None,
    thinking_level: str | None = None,
):
    """Call Gemini and return the full response (use `.text` for the text).

    contents:  a prompt string, a Part, or a list of Parts / strings (PDFs, images, URIs).
    tools:     extra tools (e.g. Vertex AI Search retrieval); google_search=True adds Google Search.
    thinking_level: MINIMAL / LOW / MEDIUM / HIGH; defaults to config.GEMINI_THINKING_LEVEL
                    (empty = the model's default, needed for models without thinking levels).

    Retries with exponential back-off; with GEMINI_SERVICE_TIER=flex it tries the
    flex tier first and falls back to standard. Every successful call is counted
    in the usage stats.
    """
    all_tools = list(tools or [])
    if google_search:
        all_tools.append(types.Tool(google_search=types.GoogleSearch()))
    search_enabled = _has_google_search(all_tools)

    config_kwargs: dict = {}
    if temperature is not None:
        config_kwargs["temperature"] = temperature
    if all_tools:
        config_kwargs["tools"] = all_tools
    level = (thinking_level or config.GEMINI_THINKING_LEVEL or "").strip().upper()
    if level:
        config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=types.ThinkingLevel[level])
    if max_output_tokens is not None:
        config_kwargs["max_output_tokens"] = max_output_tokens
    if response_mime_type:
        config_kwargs["response_mime_type"] = response_mime_type
    if system_instruction:
        config_kwargs["system_instruction"] = system_instruction
    generate_config = types.GenerateContentConfig(**config_kwargs)

    last_error = None
    configured_service_tier = (config.GEMINI_SERVICE_TIER or "").strip().lower()
    request_modes = ("flex", "standard") if configured_service_tier == "flex" else ("standard",)
    for request_mode in request_modes:
        client = _client(request_mode, config.GEMINI_LOCATION)
        retry_count = 0
        backoff_delay = INITIAL_BACKOFF
        while retry_count <= MAX_RETRIES:
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=generate_config,
                )
                _record_gemini_response_stats(response, model, request_mode, search_enabled)
                usage = getattr(response, "usage_metadata", None)
                traffic_type = getattr(usage, "traffic_type", None)
                mode = getattr(traffic_type, "value", traffic_type) or request_mode
                logger.debug("Gemini response served via mode: %s", mode)
                return response
            except Exception as error:
                last_error = error
                retry_count += 1
                if retry_count > MAX_RETRIES:
                    logger.warning(
                        "Gemini %s mode max retries exceeded: %s",
                        request_mode,
                        error,
                    )
                    if request_mode == "flex":
                        logger.warning("Falling back to Gemini standard mode")
                        break
                    raise

                logger.warning(
                    "Gemini %s request failed with %s; waiting %.1fs (retry %s/%s)...",
                    request_mode,
                    type(error).__name__,
                    backoff_delay,
                    retry_count,
                    MAX_RETRIES,
                )
                time.sleep(backoff_delay)
                backoff_delay *= 2

    if last_error is not None:
        raise last_error
    raise RuntimeError("Gemini call made no request")


async def gemini_generate_async(contents, model: str, **kwargs):
    """gemini_generate() for async code (runs in a worker thread)."""
    return await asyncio.to_thread(gemini_generate, contents, model, **kwargs)


def gemini_api_call(
    prompt: str,
    model: str,
    temperature: float = 0,
    max_output_tokens: int | None = None,
    google_search: bool = False,
    response_mime_type: str | None = None,
    system_instruction: str | None = None,
    pdf_attachments: list[tuple[str, bytes]] | None = None,
    thinking_level: str | None = None,
) -> str:
    """Call Gemini with optional search, response formatting, and PDF inputs; returns the text."""
    contents: str | list[types.Content] = prompt
    if pdf_attachments:
        parts = [types.Part.from_text(text=prompt)]
        parts.extend(
            types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")
            for _, pdf_bytes in pdf_attachments
        )
        contents = [types.Content(role="user", parts=parts)]
    response = gemini_generate(
        contents,
        model,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        google_search=google_search,
        response_mime_type=response_mime_type,
        system_instruction=system_instruction,
        thinking_level=thinking_level,
    )
    return response.text or ""


def gemini_embed(texts: list[str], model: str, task_type: str | None = None) -> list[list[float]]:
    """Embed *texts* (one call; the caller batches and retries) and return the vectors."""
    embed_config = types.EmbedContentConfig(task_type=task_type) if task_type else None
    response = _client(None, config.GEMINI_EMBEDDING_LOCATION).models.embed_content(
        model=model,
        contents=texts,
        config=embed_config,
    )
    _record_embedding_stats(response, model)
    return [embedding.values for embedding in response.embeddings]


async def gemini_embed_async(texts: list[str], model: str, task_type: str | None = None) -> list[list[float]]:
    """gemini_embed() for async code (runs in a worker thread)."""
    return await asyncio.to_thread(gemini_embed, texts, model, task_type)
