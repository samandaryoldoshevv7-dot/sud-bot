from __future__ import annotations

from app.ai.base import AINotConfiguredError, LLMProvider
from app.config import Settings, get_settings

_provider: LLMProvider | None = None

TITLES = {"gemini": "Google Gemini", "groq": "Groq", "cerebras": "Cerebras", "openrouter": "OpenRouter"}
_BASE_URLS = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "cerebras": "https://api.cerebras.ai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}


def _split(value: str) -> tuple[str, ...]:
    return tuple(m.strip() for m in value.split(",") if m.strip())


def _build(name: str, settings: Settings) -> LLMProvider:
    if name == "groq":
        from app.ai.groq_provider import GroqProvider

        return GroqProvider(settings)
    from app.ai.openai_compat import CompatSpec, OpenAICompatProvider

    model, fallbacks, light = {
        "gemini": (settings.gemini_model, settings.gemini_fallback_models, settings.gemini_validation_model),
        "cerebras": (settings.cerebras_model, settings.cerebras_fallback_models, ""),
        "openrouter": (settings.openrouter_model, settings.openrouter_fallback_models, ""),
    }[name]
    spec = CompatSpec(
        name=name,
        title=TITLES[name],
        env_key=f"{name.upper()}_API_KEY",
        base_url=_BASE_URLS[name],
        api_key=settings.provider_key(name),
        model=model,
        fallback_models=_split(fallbacks),
        light_model=light,
        timeout=settings.groq_timeout_seconds,
        max_concurrency=settings.groq_max_concurrency,
    )
    return OpenAICompatProvider(spec)


def get_llm_provider(settings: Settings | None = None) -> LLMProvider:
    """The configured provider (singleton): one service, or a chain that moves on to the next
    service when one reaches its limit. Raises ``AINotConfiguredError`` if no key is set."""
    global _provider
    if _provider is None:
        settings = settings or get_settings()
        names = settings.ai_providers
        if not names:
            raise AINotConfiguredError("No AI API key is configured")
        providers = [_build(name, settings) for name in names]
        if len(providers) == 1:
            _provider = providers[0]
        else:
            from app.ai.chain import ChainProvider

            _provider = ChainProvider(providers, TITLES, light_model_alias=settings.validation_model)
    return _provider


def set_llm_provider(provider: LLMProvider | None) -> None:
    global _provider
    _provider = provider
