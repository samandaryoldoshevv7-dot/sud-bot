"""Exercises the real GroqProvider/SDK code path against a local Groq-compatible HTTP server."""

import json

import pytest
from aiohttp import web
from groq import AsyncGroq

from app.ai import groq_provider
from app.ai.base import AIProviderError
from app.ai.groq_provider import GroqProvider
from app.ai.structured import structured_call
from app.config.settings import Settings
from app.schemas.ai import ExplanationResponse


@pytest.fixture
async def groq_server(unused_tcp_port_factory=None):
    state = {"calls": 0, "mode": "ok", "bodies": []}

    async def chat(request):
        body = await request.json()
        state["calls"] += 1
        state["bodies"].append(body)
        if state["mode"] == "auth":
            return web.json_response({"error": {"message": "Invalid API Key"}}, status=401)
        if state["mode"] == "ratelimit" and state["calls"] == 1:
            return web.json_response({"error": {"message": "Rate limit"}}, status=429, headers={"retry-after": "0"})
        content = json.dumps({"supported": True, "explanation": "Manbaga ko'ra shunday."})
        return web.json_response(
            {
                "id": "x",
                "object": "chat.completion",
                "created": 1,
                "model": body["model"],
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )

    app = web.Application()
    app.router.add_post("/openai/v1/chat/completions", chat)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield state, f"http://127.0.0.1:{port}"
    await runner.cleanup()


def _provider(base_url: str, max_retries: int = 2) -> GroqProvider:
    settings = Settings(bot_token="x", database_url="postgresql://a@b/c", groq_api_key="gsk_local_test_key")
    provider = GroqProvider(settings)
    provider._client = AsyncGroq(api_key="gsk_local_test_key", base_url=base_url, max_retries=max_retries)
    return provider


async def test_json_mode_request_and_parsing(groq_server):
    state, url = groq_server
    result = await structured_call(_provider(url), "Return JSON", "source", ExplanationResponse)
    assert result.supported and "Manbaga" in result.explanation
    assert state["bodies"][0]["response_format"] == {"type": "json_object"}
    assert state["bodies"][0]["model"] == "llama-3.3-70b-versatile"


async def test_rate_limit_is_retried(groq_server, monkeypatch):
    state, url = groq_server
    state["mode"] = "ratelimit"
    monkeypatch.setattr(groq_provider, "_retry_after_seconds", lambda exc: 0)
    result = await structured_call(_provider(url, max_retries=0), "Return JSON", "source", ExplanationResponse)
    assert result.supported and state["calls"] == 2


async def test_auth_error_is_reported(groq_server):
    state, url = groq_server
    state["mode"] = "auth"
    with pytest.raises(AIProviderError, match="authentication"):
        await structured_call(_provider(url, max_retries=0), "Return JSON", "source", ExplanationResponse)
