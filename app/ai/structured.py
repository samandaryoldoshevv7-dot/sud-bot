"""Robust JSON-mode calls: parse, validate with Pydantic, retry with feedback on failure."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import OrderedDict
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.ai import usage
from app.ai.base import AIProviderError, AIResponseError, LLMProvider

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_CACHE_MAX = 512
_cache: OrderedDict[str, tuple[float, BaseModel]] = OrderedDict()


def _cache_key(model: str | None, system: str, user: str, schema: type[BaseModel], max_tokens: int) -> str:
    raw = json.dumps([model, system, user, schema.__name__, max_tokens], ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _cache_get(key: str) -> BaseModel | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    expires, value = entry
    if expires < time.monotonic():
        _cache.pop(key, None)
        return None
    _cache.move_to_end(key)
    return value.model_copy(deep=True)


def _cache_put(key: str, value: BaseModel, ttl: float) -> None:
    _cache[key] = (time.monotonic() + ttl, value.model_copy(deep=True))
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


def clear_cache() -> None:
    _cache.clear()


def extract_json(raw: str) -> object:
    """Parse a JSON object from model output, tolerating code fences and leading prose."""
    text = _FENCE.sub("", raw.strip()).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise json.JSONDecodeError("no JSON object found", text, 0)


async def structured_call(
    provider: LLMProvider,
    system: str,
    user: str,
    schema: type[T],
    *,
    retries: int = 3,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int = 2048,
    purpose: str = "",
    cache_ttl: float = 0,
) -> T:
    """``cache_ttl`` > 0: an identical deterministic request (temperature 0) made again within that
    time is answered from memory instead of calling the API (e.g. an admin re-checking a question)."""
    key = _cache_key(model, system, user, schema, max_tokens) if cache_ttl > 0 and temperature == 0 else None
    if key is not None:
        cached = _cache_get(key)
        if cached is not None:
            usage.record_cache_hit()
            logger.info("AI result served from cache", extra={"purpose": purpose})
            return cached  # type: ignore[return-value]
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    last_error = ""
    for attempt in range(1, retries + 1):
        try:
            with usage.purpose(purpose):
                raw = await provider.complete_json(
                    messages, model=model, temperature=temperature, max_tokens=max_tokens
                )
        except AIProviderError as exc:
            last_error = str(exc)
            if "rejected the request" in last_error and attempt < retries:
                # Usually Groq's json_validate_failed – retry with a reminder.
                messages = messages[:2] + [
                    {"role": "user", "content": "Return ONLY one valid JSON object. No prose, no markdown."}
                ]
                continue
            raise
        try:
            data = extract_json(raw)
            result = schema.model_validate(data)
            if key is not None:
                _cache_put(key, result, cache_ttl)
            return result
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = str(exc)[:800]
            logger.warning(
                "AI returned invalid structured output",
                extra={"purpose": purpose, "attempt": attempt, "error": last_error[:300]},
            )
            messages = messages[:2] + [
                # Only the start of the bad answer: enough to fix it, far fewer tokens than all of it.
                {"role": "assistant", "content": raw[:1500]},
                {
                    "role": "user",
                    "content": (
                        "Your previous response was invalid for the required JSON schema. "
                        f"Errors: {last_error}\nReturn ONLY the corrected JSON object."
                    ),
                },
            ]
    raise AIResponseError(f"Invalid AI response after {retries} attempts ({purpose}): {last_error[:300]}")
