"""Several AI services in a row (Gemini → Groq → Cerebras → OpenRouter), each with its own key."""

import json

import httpx
import pytest

from app.ai import openai_compat, usage
from app.ai.base import AIProviderError, LLMProvider
from app.ai.chain import ChainProvider
from app.ai.openai_compat import CompatSpec, OpenAICompatProvider
from app.ai.structured import structured_call
from app.config.settings import Settings
from app.schemas.ai import ExplanationResponse

OK_JSON = json.dumps({"supported": True, "explanation": "Manbaga ko'ra shunday."})


def _ok(body: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"index": 0, "message": {"role": "assistant", "content": OK_JSON}}],
            "model": body["model"],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        },
    )


def _gemini(handler, **spec) -> OpenAICompatProvider:
    defaults = dict(
        name="gemini", title="Google Gemini", env_key="GEMINI_API_KEY",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai", api_key="AIza-test-key",
        model="gemini-2.5-flash", fallback_models=("gemini-2.5-flash-lite",), light_model="gemini-2.5-flash-lite",
    )  # fmt: skip
    defaults.update(spec)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAICompatProvider(CompatSpec(**defaults), client=client)


@pytest.fixture
def no_sleep(monkeypatch):
    waits: list[float] = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(openai_compat, "_sleep", fake_sleep)
    return waits


async def test_gemini_request_is_openai_compatible_json_mode():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok(json.loads(request.content))

    with usage.track() as spent:
        result = await structured_call(_gemini(handler), "Return JSON", "source", ExplanationResponse)
    assert result.supported and "Manbaga" in result.explanation
    request = seen[0]
    assert str(request.url) == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert request.headers["authorization"] == "Bearer AIza-test-key"
    body = json.loads(request.content)
    assert body["model"] == "gemini-2.5-flash" and body["response_format"] == {"type": "json_object"}
    assert body["reasoning_effort"] == "low" and body["max_tokens"] > 2048  # thinking tokens have room
    assert spent.requests == 1 and spent.input_tokens == 100 and spent.output_tokens == 20


async def test_daily_limit_is_reported_at_once(no_sleep):
    """Gemini wraps its error in a list and names the per-day quota."""
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, json=[{"error": {
            "code": 429, "status": "RESOURCE_EXHAUSTED",
            "message": "You exceeded your current quota. Quota exceeded for metric: "
                       "generativelanguage.googleapis.com/generate_content_free_tier_requests, "
                       "quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier. Please retry in 3h20m5s.",
        }}])  # fmt: skip

    with pytest.raises(AIProviderError, match=r"daily rate limit exceeded; retry in 200 min"):
        await _gemini(handler).complete_json([{"role": "user", "content": "x"}])
    assert len(calls) == 1 and not no_sleep


async def test_per_minute_limit_waits_and_retries(no_sleep):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": {"message": "Quota exceeded: GenerateRequestsPerMinute"}},
                                  headers={"retry-after": "7"})  # fmt: skip
        return _ok(json.loads(request.content))

    assert await _gemini(handler).complete_json([{"role": "user", "content": "x"}]) == OK_JSON
    assert len(calls) == 2 and no_sleep and 7 <= no_sleep[0] <= 61


async def test_retired_model_falls_back():
    models = []

    def handler(request):
        body = json.loads(request.content)
        models.append(body["model"])
        if body["model"] == "gemini-2.5-flash":
            return httpx.Response(404, json={"error": {"message": "models/gemini-2.5-flash is not found"}})
        return _ok(body)

    provider = _gemini(handler)
    assert await provider.complete_json([{"role": "user", "content": "x"}]) == OK_JSON
    await provider.complete_json([{"role": "user", "content": "x"}])
    assert models == ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-flash-lite"]


async def test_wrong_key_names_the_variable_never_the_key():
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "API key not valid"}})

    with pytest.raises(AIProviderError) as info:
        await _gemini(handler).complete_json([{"role": "user", "content": "x"}])
    assert "GEMINI_API_KEY" in str(info.value) and "AIza-test-key" not in str(info.value)


class _Fake(LLMProvider):
    def __init__(self, name: str, errors: list[str] | None = None):
        self.name = name
        self.errors = list(errors or [])
        self.calls: list[str | None] = []
        self.light_model = f"{name}-light"

    @property
    def default_model(self) -> str:
        return f"{self.name}-main"

    async def complete_json(self, messages, *, model=None, temperature=None, max_tokens=2048):
        self.calls.append(model)
        if self.errors:
            raise AIProviderError(self.errors.pop(0))
        return OK_JSON


def _chain(*providers):
    titles = {"gemini": "Google Gemini", "groq": "Groq", "cerebras": "Cerebras"}
    return ChainProvider(list(providers), titles, light_model_alias="openai/gpt-oss-20b")


async def test_switches_to_next_service_when_daily_limit_is_reached():
    gemini = _Fake("gemini", ["AI daily rate limit exceeded; retry in 90 min"])
    groq = _Fake("groq")
    chain = _chain(gemini, groq)
    for _ in range(3):
        assert await chain.complete_json([{"role": "user", "content": "x"}]) == OK_JSON
    assert len(gemini.calls) == 1  # skipped while its limit resets
    assert len(groq.calls) == 3
    assert chain.active_title == "Groq"
    assert chain.status()[0] == ("Google Gemini", 90) and chain.status()[1] == ("Groq", 0)


async def test_check_model_is_mapped_to_each_services_light_model():
    gemini, groq = _Fake("gemini"), _Fake("groq")
    chain = _chain(gemini, groq)
    await chain.complete_json([], model="openai/gpt-oss-20b")  # validation model (a Groq name)
    await chain.complete_json([])
    assert gemini.calls == ["gemini-light", None]
    gemini.errors = ["AI provider is unreachable (Google Gemini)"]
    await chain.complete_json([], model="openai/gpt-oss-20b")
    assert groq.calls == ["openai/gpt-oss-20b"]  # Groq keeps its own model names


async def test_all_services_out_tells_when_to_retry():
    chain = _chain(
        _Fake("gemini", ["AI daily rate limit exceeded; retry in 90 min"]),
        _Fake("groq", ["AI daily rate limit exceeded; retry in 30 min"]),
        _Fake("cerebras", ["AI provider rate limit exceeded (Cerebras)"]),
    )
    with pytest.raises(AIProviderError, match=r"daily rate limit exceeded; retry in 1 min"):
        await chain.complete_json([])
    with pytest.raises(AIProviderError, match=r"daily rate limit exceeded; retry in 1 min"):
        await chain.complete_json([])  # all skipped: nothing is called again


async def test_invalid_json_is_retried_by_the_caller_not_switched():
    gemini = _Fake("gemini", ["AI provider rejected the request: json_validate_failed"])
    groq = _Fake("groq")
    chain = _chain(gemini, groq)
    with pytest.raises(AIProviderError, match="rejected"):
        await chain.complete_json([])
    assert not groq.calls
    assert await chain.complete_json([]) == OK_JSON and len(gemini.calls) == 2


def test_only_services_with_a_key_take_part_in_order():
    base = dict(bot_token="x", database_url="postgresql://a@b/c")
    assert Settings(**base, groq_api_key="").ai_providers == []
    assert not Settings(**base, groq_api_key="").ai_enabled
    settings = Settings(**base, groq_api_key="gsk_abc", gemini_api_key="AIza_abc", openrouter_api_key="sk-or-abc")
    assert settings.ai_providers == ["gemini", "groq", "openrouter"]
    assert {"gsk_abc", "AIza_abc", "sk-or-abc"} <= set(settings.secret_values())
    custom = Settings(**base, groq_api_key="gsk_abc", gemini_api_key="AIza_abc", ai_provider_order="groq, gemini")
    assert custom.ai_providers == ["groq", "gemini"]


def test_factory_builds_a_chain_for_several_keys():
    from app.ai import factory

    settings = Settings(bot_token="x", database_url="postgresql://a@b/c", groq_api_key="gsk_abc",
                        gemini_api_key="AIza_abc")  # fmt: skip
    factory.set_llm_provider(None)
    try:
        provider = factory.get_llm_provider(settings)
        assert isinstance(provider, ChainProvider)
        assert [p.name for p in provider.providers] == ["gemini", "groq"]
        assert provider.default_model == "gemini-2.5-flash"
    finally:
        factory.set_llm_provider(None)
