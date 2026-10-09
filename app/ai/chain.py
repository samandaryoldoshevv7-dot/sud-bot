"""Several AI services one after another: when one is out of its limit, the next one answers.

Order comes from ``AI_PROVIDER_ORDER`` (default Gemini → Groq → Cerebras → OpenRouter); only
services whose API key is set take part. A service that hit its daily limit (or failed: bad key,
unreachable) is skipped until its cool-down ends, then tried again. Every service gets the same
prompts and every question still passes the same checks, so quality does not depend on which
one answered.
"""

from __future__ import annotations

import logging
import re
import time

from app.ai.base import AIProviderError, LLMProvider

logger = logging.getLogger(__name__)

SHORT_COOLDOWN = 60.0  # per-minute limit still busy after retries / unreachable / 5xx
AUTH_COOLDOWN = 6 * 3600.0  # wrong key: retried after a while (the admin may fix it on Railway)
MODEL_COOLDOWN = 6 * 3600.0


def _cooldown(error: str) -> float | None:
    """Seconds to skip a service after this error; None = not the service's fault (bad JSON etc.)."""
    lowered = error.lower()
    if "rejected the request" in lowered:
        return None
    daily = re.search(r"daily rate limit exceeded; retry in (\d+) min", lowered)
    if daily:
        return int(daily.group(1)) * 60.0
    if "authentication" in lowered:
        return AUTH_COOLDOWN
    if "model" in lowered and "unavailable" in lowered:
        return MODEL_COOLDOWN
    return SHORT_COOLDOWN


class ChainProvider(LLMProvider):
    name = "chain"

    def __init__(self, providers: list[LLMProvider], titles: dict[str, str], light_model_alias: str = ""):
        if not providers:
            raise ValueError("at least one provider is required")
        self.providers = providers
        self.titles = titles
        # Callers ask for the (Groq) validation model by name for checks; other services map it to
        # their own light model.
        self._light_alias = light_model_alias
        self._blocked_until: dict[str, float] = {}
        self._last_used: str | None = None

    @property
    def default_model(self) -> str:
        return self.providers[0].default_model

    @property
    def active_title(self) -> str:
        name = self._last_used or self.providers[0].name
        return self.titles.get(name, name)

    def status(self) -> list[tuple[str, int]]:
        """(title, minutes until usable again; 0 = ready) for the settings screen."""
        now = time.monotonic()
        return [
            (self.titles.get(p.name, p.name), max(0, round((self._blocked_until.get(p.name, 0) - now) / 60)))
            for p in self.providers
        ]

    def _model_for(self, provider: LLMProvider, model: str | None) -> str | None:
        if provider.name == "groq" or model is None:
            return model
        if model == self._light_alias:
            return getattr(provider, "light_model", None)
        return None  # a Groq model name means nothing to another service: use its main model

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int = 2048,
    ) -> str:
        now = time.monotonic()
        ready = [p for p in self.providers if self._blocked_until.get(p.name, 0) <= now]
        last_error: AIProviderError | None = None
        any_limit = False
        for provider in ready:
            try:
                content = await provider.complete_json(
                    messages, model=self._model_for(provider, model), temperature=temperature, max_tokens=max_tokens
                )
            except AIProviderError as exc:
                cooldown = _cooldown(str(exc))
                if cooldown is None:
                    raise  # invalid JSON and similar: the caller retries with the same service
                self._blocked_until[provider.name] = time.monotonic() + cooldown
                last_error = exc
                any_limit = any_limit or "rate limit" in str(exc)
                logger.warning(
                    "AI service unavailable, switching to the next one",
                    extra={"provider": provider.name, "minutes": round(cooldown / 60), "error": str(exc)[:200]},
                )
                continue
            if provider.name != self._last_used:
                if self._last_used is not None:
                    logger.warning("AI service switched", extra={"provider": provider.name})
                self._last_used = provider.name
            return content
        # Every service is out: say when the first one is back, so the admin knows how long to wait.
        waits = [self._blocked_until.get(p.name, 0) - time.monotonic() for p in self.providers]
        soonest = max(1, round(min(waits) / 60))
        if last_error is not None and not any_limit and len(ready) == len(self.providers):
            raise last_error  # e.g. a wrong key or no connection everywhere: the admin needs that reason
        raise AIProviderError(f"AI daily rate limit exceeded; retry in {soonest} min")

    async def close(self) -> None:
        for provider in self.providers:
            await provider.close()
