"""Groq (OpenAI-compatible) chat completion provider using the official SDK."""

from __future__ import annotations

import asyncio
import logging

import groq
from groq import AsyncGroq

from app.ai.base import AINotConfiguredError, AIProviderError, LLMProvider
from app.config import Settings

logger = logging.getLogger(__name__)

RATE_LIMIT_EXTRA_RETRIES = 3


def _retry_after_seconds(exc: groq.RateLimitError) -> float:
    try:
        value = float(exc.response.headers.get("retry-after", "0"))
    except (TypeError, ValueError, AttributeError):
        value = 0.0
    return min(max(value, 5.0), 60.0)


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

    @property
    def default_model(self) -> str:
        return self._settings.groq_model

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int = 2048,
    ) -> str:
        model = model or self.default_model
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
        try:
            response = await self._client.chat.completions.create(
                model=model,
                messages=messages,  # type: ignore[arg-type]
                temperature=self._settings.groq_temperature if temperature is None else temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
        except groq.AuthenticationError as exc:
            logger.error("Groq authentication failed (check GROQ_API_KEY)", extra={"status": exc.status_code})
            raise AIProviderError("AI provider authentication failed") from exc
        except groq.RateLimitError:
            raise  # handled (with longer waits) by complete_json
        except groq.BadRequestError as exc:
            # json_validate_failed: the model produced invalid JSON; the caller retries.
            logger.warning("Groq rejected the request", extra={"status": exc.status_code, "detail": str(exc)[:300]})
            raise AIProviderError(f"AI provider rejected the request: {str(exc)[:200]}") from exc
        except (groq.APITimeoutError, groq.APIConnectionError) as exc:
            logger.warning("Groq connection problem", extra={"error": type(exc).__name__})
            raise AIProviderError("AI provider is unreachable") from exc
        except groq.APIStatusError as exc:
            logger.error("Groq API error", extra={"status": exc.status_code})
            raise AIProviderError(f"AI provider error (HTTP {exc.status_code})") from exc

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
