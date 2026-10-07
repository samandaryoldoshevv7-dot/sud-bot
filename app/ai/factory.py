from __future__ import annotations

from app.ai.base import LLMProvider
from app.config import Settings, get_settings

_provider: LLMProvider | None = None


def get_llm_provider(settings: Settings | None = None) -> LLMProvider:
    """Return the configured provider (singleton). Raises ``AINotConfiguredError`` if no key."""
    global _provider
    if _provider is None:
        settings = settings or get_settings()
        if settings.ai_provider == "groq":
            from app.ai.groq_provider import GroqProvider

            _provider = GroqProvider(settings)
        else:  # pragma: no cover - guarded by Settings Literal
            raise ValueError(f"Unknown AI provider: {settings.ai_provider}")
    return _provider


def set_llm_provider(provider: LLMProvider | None) -> None:
    global _provider
    _provider = provider
