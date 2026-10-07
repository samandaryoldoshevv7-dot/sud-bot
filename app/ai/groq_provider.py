"""Groq (OpenAI-compatible) chat completion provider using the official SDK.

Groq retires models regularly (e.g. ``llama-3.3-70b-versatile`` was shut down on 2026-08-16).
When the configured model is unavailable (decommissioned / not found / no access) the provider
automatically switches to the next model from ``GROQ_FALLBACK_MODELS`` and remembers it.
"""

from __future__ import annotations

import asyncio
import logging

import groq
from groq import AsyncGroq

from app.ai.base import AINotConfiguredError, AIProviderError, LLMProvider
from app.config import Settings

logger = logging.getLogger(__name__)

RATE_LIMIT_EXTRA_RETRIES = 3
# Extra completion budget for reasoning models (hidden reasoning tokens count toward the limit).
REASONING_TOKEN_BUDGET = 3000
_MODEL_GONE_MARKERS = (
    "decommissioned",
    "model_not_found",
    "does not exist",
    "not found",
    "no longer supported",
    "do not have access",
    "model_permission",
)


class ModelUnavailableError(AIProviderError):
    """The requested model cannot be used (retired, unknown or not permitted)."""


def _retry_after_seconds(exc: groq.RateLimitError) -> float:
    try:
        value = float(exc.response.headers.get("retry-after", "0"))
    except (TypeError, ValueError, AttributeError):
        value = 0.0
    return min(max(value, 5.0), 60.0)


def _is_model_gone(exc: groq.APIStatusError) -> bool:
    if exc.status_code == 404:
        return True
    text = str(exc).lower()
    return exc.status_code in (400, 403) and any(marker in text for marker in _MODEL_GONE_MARKERS)


def _is_reasoning_model(model: str) -> bool:
    name = model.lower()
    return "gpt-oss" in name or "qwen3" in name or "deepseek-r1" in name


class GroqProvider(LLMProvider):
    name = "groq"

    def __init__(self, settings: Settings):
        if not settings.ai_enabled:
            raise AINotConfiguredError("GROQ_API_KEY is not configured")
        self._settings = settings
        # The SDK retries 408/409/429/5xx with exponential backoff and honours Retry-After.
        self._client = AsyncGroq(
            api_key=settings.groq_api_key.get_secret_value(),  # type: ignore[union-attr]
            timeout=settings.groq_timeout_seconds,
            max_retries=4,
        )
        self._unavailable: set[str] = set()
        self._replacement: dict[str, str] = {}

    @property
    def default_model(self) -> str:
        return self._replacement.get(self._settings.groq_model, self._settings.groq_model)

    def _candidates(self, model: str) -> list[str]:
        ordered = [self._replacement.get(model, model), model, *self._settings.fallback_models]
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
        requested = model or self._settings.groq_model
        candidates = self._candidates(requested)
        if not candidates:
            raise ModelUnavailableError(
                "No usable Groq model: set GROQ_MODEL to a model listed at https://console.groq.com/docs/models"
            )
        last_error: ModelUnavailableError | None = None
        for candidate in candidates:
            try:
                content = await self._with_rate_limit(messages, candidate, temperature, max_tokens)
            except ModelUnavailableError as exc:
                self._unavailable.add(candidate)
                last_error = exc
                logger.error(
                    "Groq model unavailable, trying fallback",
                    extra={"model": candidate, "detail": str(exc)[:200]},
                )
                continue
            if candidate != requested and self._replacement.get(requested) != candidate:
                self._replacement[requested] = candidate
                logger.warning("Using fallback Groq model", extra={"requested": requested, "model": candidate})
            return content
        raise ModelUnavailableError(
            f"Groq models unavailable ({', '.join(candidates)}). Set GROQ_MODEL to a current model. "
            f"Last error: {last_error}"
        )

    async def _with_rate_limit(
        self, messages: list[dict[str, str]], model: str, temperature: float | None, max_tokens: int
    ) -> str:
        for attempt in range(1, RATE_LIMIT_EXTRA_RETRIES + 2):
            try:
                return await self._complete(messages, model, temperature, max_tokens)
            except groq.RateLimitError as exc:
                # The SDK already retried with backoff; free-tier token-per-minute limits can need longer.
                if attempt > RATE_LIMIT_EXTRA_RETRIES:
                    logger.warning("Groq rate limit exhausted after retries")
                    raise AIProviderError("AI provider rate limit exceeded") from exc
                wait = _retry_after_seconds(exc)
                logger.info("Groq rate limited, waiting", extra={"seconds": wait, "attempt": attempt})
                await asyncio.sleep(wait)
        raise AIProviderError("AI provider rate limit exceeded")  # pragma: no cover

    async def _complete(
        self, messages: list[dict[str, str]], model: str, temperature: float | None, max_tokens: int
    ) -> str:
        kwargs: dict = {}
        if _is_reasoning_model(model):
            max_tokens += REASONING_TOKEN_BUDGET
            if "gpt-oss" in model.lower():
                kwargs["reasoning_effort"] = "low"
        try:
            response = await self._client.chat.completions.create(
                model=model,
                messages=messages,  # type: ignore[arg-type]
                temperature=self._settings.groq_temperature if temperature is None else temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
                **kwargs,
            )
        except groq.AuthenticationError as exc:
            logger.error("Groq authentication failed (check GROQ_API_KEY)", extra={"status": exc.status_code})
            raise AIProviderError("AI provider authentication failed (check GROQ_API_KEY)") from exc
        except groq.RateLimitError:
            raise  # handled (with longer waits) by _with_rate_limit
        except groq.APIStatusError as exc:
            if _is_model_gone(exc):
                raise ModelUnavailableError(f"model {model}: {str(exc)[:200]}") from exc
            if isinstance(exc, groq.BadRequestError):
                # json_validate_failed: the model produced invalid JSON; the caller retries.
                logger.warning("Groq rejected the request", extra={"status": exc.status_code, "detail": str(exc)[:300]})
                raise AIProviderError(f"AI provider rejected the request: {str(exc)[:200]}") from exc
            logger.error("Groq API error", extra={"status": exc.status_code, "detail": str(exc)[:300]})
            raise AIProviderError(f"AI provider error (HTTP {exc.status_code})") from exc
        except (groq.APITimeoutError, groq.APIConnectionError) as exc:
            logger.warning("Groq connection problem", extra={"error": type(exc).__name__})
            raise AIProviderError("AI provider is unreachable") from exc
        except groq.APIError as exc:
            logger.error("Groq client error", extra={"error": type(exc).__name__, "detail": str(exc)[:300]})
            raise AIProviderError(f"AI provider error: {str(exc)[:200]}") from exc

        choice = response.choices[0] if response.choices else None
        content = choice.message.content if choice and choice.message else None
        usage = getattr(response, "usage", None)
        logger.debug(
            "groq completion",
            extra={
                "model": model,
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
            },
        )
        return content or ""

    async def close(self) -> None:
        await self._client.close()
