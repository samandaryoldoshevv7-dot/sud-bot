"""Provider-agnostic LLM interface."""

from __future__ import annotations

from abc import ABC, abstractmethod


class AIError(Exception):
    """Base class for AI failures."""


class AIProviderError(AIError):
    """Network/API failure (rate limit, timeout, auth, 5xx)."""


class AIResponseError(AIError):
    """The model kept returning output that does not match the expected schema."""


class AINotConfiguredError(AIError):
    """No API key configured."""


class LLMProvider(ABC):
    """Minimal interface every LLM provider implements (Groq today, others later)."""

    name: str = "base"

    @property
    @abstractmethod
    def default_model(self) -> str: ...

    @abstractmethod
    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int = 2048,
    ) -> str:
        """Return the raw text of a completion that is requested in JSON mode."""

    async def close(self) -> None:  # pragma: no cover - optional
        return None
