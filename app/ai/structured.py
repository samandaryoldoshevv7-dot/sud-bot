"""Robust JSON-mode calls: parse, validate with Pydantic, retry with feedback on failure."""

from __future__ import annotations

import json
import logging
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.ai.base import AIProviderError, AIResponseError, LLMProvider

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


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
) -> T:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    last_error = ""
    for attempt in range(1, retries + 1):
        try:
            raw = await provider.complete_json(messages, model=model, temperature=temperature, max_tokens=max_tokens)
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
            return schema.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = str(exc)[:800]
            logger.warning(
                "AI returned invalid structured output",
                extra={"purpose": purpose, "attempt": attempt, "error": last_error[:300]},
            )
            messages = messages[:2] + [
                {"role": "assistant", "content": raw[:4000]},
                {
                    "role": "user",
                    "content": (
                        "Your previous response was invalid for the required JSON schema. "
                        f"Errors: {last_error}\nReturn ONLY the corrected JSON object."
                    ),
                },
            ]
    raise AIResponseError(f"Invalid AI response after {retries} attempts ({purpose}): {last_error[:300]}")
