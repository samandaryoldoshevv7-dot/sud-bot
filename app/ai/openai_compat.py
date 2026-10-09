"""Chat completions of OpenAI-compatible APIs (Google Gemini, Cerebras, OpenRouter) over plain HTTP.

Each service is used with ONE official API key of its own; when its limit is reached the
``ChainProvider`` moves on to the next service. Nothing here tries to get around a limit.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass

import httpx

from app.ai import usage
from app.ai.base import AIProviderError, LLMProvider

logger = logging.getLogger(__name__)

RATE_LIMIT_RETRIES = 3
SERVER_ERROR_RETRIES = 2
REASONING_TOKEN_BUDGET = 3000
DEFAULT_LONG_WAIT_MINUTES = 60
_MODEL_GONE_MARKERS = (
    "decommissioned",
    "model_not_found",
    "does not exist",
    "not found",
    "no longer supported",
    "is not supported",
    "do not have access",
    "no endpoints found",
    "model_permission",
)
_LONG_LIMIT_MARKERS = ("per day", "perday", "per-day", "daily", "per_day", "(tpd)", "(rpd)")


_sleep = asyncio.sleep  # tests replace this, never the global asyncio.sleep


class CompatModelUnavailableError(AIProviderError):
    """The model cannot be used here (retired, unknown or not permitted)."""


@dataclass(frozen=True)
class CompatSpec:
    name: str  # "gemini", "cerebras", "openrouter"
    title: str  # shown to admins
    env_key: str  # environment variable holding the key (named in error messages, never the key)
    base_url: str
    api_key: str
    model: str
    fallback_models: tuple[str, ...] = ()
    light_model: str = ""  # cheaper model for checks; empty = the main model
    timeout: float = 60.0
    max_concurrency: int = 2


def _is_reasoning_model(model: str) -> bool:
    name = model.lower()
    return any(m in name for m in ("gpt-oss", "qwen3", "qwen-3", "deepseek-r1", "gemini-2.5", "gemini-3"))


def _retry_hint_seconds(response: httpx.Response) -> float:
    """Wait suggested by the service (Retry-After header or "retry in 7m12s" / "retry in 33.5s")."""
    wait = 0.0
    try:
        wait = float(response.headers.get("retry-after", "0"))
    except ValueError:
        pass
    text = response.text.lower()
    match = re.search(r"(?:try again|retry) in (?:(\d+)h)?\s*(?:(\d+)m(?!s))?\s*(?:([\d.]+)s)?", text)
    if match and any(match.groups()):
        h, m, s = (float(x) if x else 0.0 for x in match.groups())
        wait = max(wait, h * 3600 + m * 60 + s)
    reset = response.headers.get("x-ratelimit-reset", "")
    if reset.isdigit():  # OpenRouter: epoch milliseconds
        until = int(reset) / 1000 - time.time()
        if 0 < until < 2 * 86400:
            wait = max(wait, until)
    return wait


def _long_limit_minutes(response: httpx.Response) -> int | None:
    """Minutes until a daily/long limit resets; None for a short per-minute limit."""
    text = response.text.lower()
    wait = _retry_hint_seconds(response)
    if any(marker in text for marker in _LONG_LIMIT_MARKERS):
        return max(1, round(wait / 60)) if wait > 120 else DEFAULT_LONG_WAIT_MINUTES
    if wait > 120:
        return max(1, round(wait / 60))
    return None


def _is_model_gone(response: httpx.Response) -> bool:
    if response.status_code == 404:
        return True
    text = response.text.lower()
    return response.status_code in (400, 403) and "model" in text and any(m in text for m in _MODEL_GONE_MARKERS)


class OpenAICompatProvider(LLMProvider):
    def __init__(self, spec: CompatSpec, client: httpx.AsyncClient | None = None):
        self.spec = spec
        self.name = spec.name
        self._client = client or httpx.AsyncClient(timeout=spec.timeout)
        self._semaphore = asyncio.Semaphore(max(1, spec.max_concurrency))
        self._unavailable: set[str] = set()

    @property
    def default_model(self) -> str:
        return next((m for m in self._candidates(self.spec.model)), self.spec.model)

    @property
    def light_model(self) -> str:
        return self.spec.light_model or self.spec.model

    def _candidates(self, model: str) -> list[str]:
        ordered = [model, self.spec.model, *self.spec.fallback_models]
        seen: list[str] = []
        for name in ordered:
            if name and name not in seen and name not in self._unavailable:
                seen.append(name)
        return seen

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int = 2048,
    ) -> str:
        candidates = self._candidates(model or self.spec.model)
        if not candidates:
            raise CompatModelUnavailableError(f"{self.spec.title}: no usable model")
        last: CompatModelUnavailableError | None = None
        for candidate in candidates:
            try:
                return await self._with_retries(messages, candidate, temperature, max_tokens)
            except CompatModelUnavailableError as exc:
                self._unavailable.add(candidate)
                last = exc
                logger.error("AI model unavailable, trying the next one",
                             extra={"provider": self.name, "model": candidate, "detail": str(exc)[:200]})  # fmt: skip
        raise CompatModelUnavailableError(f"{self.spec.title}: models unavailable. Last error: {last}")

    async def _with_retries(
        self, messages: list[dict[str, str]], model: str, temperature: float | None, max_tokens: int
    ) -> str:
        rate_limited = 0
        server_errors = 0
        while True:
            try:
                response = await self._post(messages, model, temperature, max_tokens)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                server_errors += 1
                if server_errors > SERVER_ERROR_RETRIES:
                    logger.warning("AI connection problem", extra={"provider": self.name, "error": type(exc).__name__})
                    raise AIProviderError(f"AI provider is unreachable ({self.spec.title})") from exc
                await _sleep(2 * server_errors)
                continue
            status = response.status_code
            if status == 200:
                return self._parse(response, model)
            if status == 429:
                minutes = _long_limit_minutes(response)
                if minutes is not None:
                    logger.warning(
                        "AI daily/long rate limit reached", extra={"provider": self.name, "minutes": minutes}
                    )
                    raise AIProviderError(f"AI daily rate limit exceeded; retry in {minutes} min")
                rate_limited += 1
                if rate_limited > RATE_LIMIT_RETRIES:
                    raise AIProviderError(f"AI provider rate limit exceeded ({self.spec.title})")
                wait = min(max(_retry_hint_seconds(response), 5.0 * 2 ** (rate_limited - 1)), 60.0)
                await _sleep(wait + random.uniform(0, 1))
                continue
            if status in (401, 403) and not _is_model_gone(response):
                logger.error("AI authentication failed", extra={"provider": self.name, "status": status})
                raise AIProviderError(f"AI provider authentication failed (check {self.spec.env_key})")
            if _is_model_gone(response):
                raise CompatModelUnavailableError(f"model {model}: {response.text[:200]}")
            if status >= 500:
                server_errors += 1
                if server_errors <= SERVER_ERROR_RETRIES:
                    await _sleep(2 * server_errors)
                    continue
                logger.error("AI API error", extra={"provider": self.name, "status": status})
                raise AIProviderError(f"AI provider error (HTTP {status}, {self.spec.title})")
            logger.warning("AI rejected the request",
                           extra={"provider": self.name, "status": status, "detail": response.text[:300]})  # fmt: skip
            raise AIProviderError(f"AI provider rejected the request: {response.text[:200]}")

    async def _post(
        self, messages: list[dict[str, str]], model: str, temperature: float | None, max_tokens: int
    ) -> httpx.Response:
        body: dict = {
            "model": model,
            "messages": messages,
            "temperature": 0.3 if temperature is None else temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        if _is_reasoning_model(model):
            body["max_tokens"] = max_tokens + REASONING_TOKEN_BUDGET
            body["reasoning_effort"] = "low"
        headers = {"Authorization": f"Bearer {self.spec.api_key}"}
        async with self._semaphore:
            return await self._client.post(f"{self.spec.base_url.rstrip('/')}/chat/completions", json=body,
                                           headers=headers)  # fmt: skip

    def _parse(self, response: httpx.Response, model: str) -> str:
        try:
            data = response.json()
            if isinstance(data, list):  # some gateways wrap the object in a list
                data = data[0]
            choice = (data.get("choices") or [{}])[0]
            content = (choice.get("message") or {}).get("content") or ""
            stats = data.get("usage") or {}
        except (ValueError, AttributeError, IndexError, TypeError) as exc:
            raise AIProviderError(f"AI provider returned an unreadable answer ({self.spec.title})") from exc
        usage.record(f"{self.name}:{model}", int(stats.get("prompt_tokens") or 0),
                     int(stats.get("completion_tokens") or 0))  # fmt: skip
        return content

    async def close(self) -> None:
        await self._client.aclose()
