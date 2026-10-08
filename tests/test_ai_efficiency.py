"""Groq request/token savings: batching, caching, pacing, concurrency, token accounting."""

import asyncio

from sqlalchemy import select

from app.ai import groq_provider, usage
from app.ai.structured import structured_call
from app.models import Question
from app.rag.retriever import Retriever
from app.rag.vector_store import SourceScope
from app.schemas.ai import ExplanationResponse
from app.services.question_generation import GenerationRequest, QuestionGenerator
from tests.factories import HashEmbeddings, ScriptedLLM, make_material_with_text, make_question
from tests.test_groq_provider import _provider, groq_server  # noqa: F401  (fixture)


async def test_generation_uses_three_requests_per_chunk_instead_of_six(session_maker):
    """Before: per chunk of 2 questions = 1 generation + 2 source checks + 2 quality reviews + 1 topic
    classification (6 requests). Now the checks are batched per chunk and the topic comes with the
    generation: 3 requests, same checks."""
    mid = await make_material_with_text(session_maker)
    llm = ScriptedLLM()
    async with session_maker() as session:
        gen = QuestionGenerator(llm, Retriever(HashEmbeddings()))
        result = await gen.generate(session, GenerationRequest(scope=SourceScope(material_ids=[mid]), count=4))
    assert result.created == 4
    assert llm.calls.count("generation") == 2
    assert len(llm.calls) == 6  # was 12


class _BrokenBatchLLM(ScriptedLLM):
    """Answers only the first question of a batched source check."""

    async def complete_json(self, messages, **kw):
        raw = await super().complete_json(messages, **kw)
        if "QUESTION 0:" in messages[1]["content"] and "fact checker" in messages[0]["content"]:
            import json

            data = json.loads(raw)
            data["results"] = data["results"][:1]
            return json.dumps(data)
        return raw


async def test_question_missing_from_a_batch_is_still_checked_alone(session_maker):
    mid = await make_material_with_text(session_maker)
    llm = _BrokenBatchLLM()
    async with session_maker() as session:
        gen = QuestionGenerator(llm, Retriever(HashEmbeddings()))
        result = await gen.generate(session, GenerationRequest(scope=SourceScope(material_ids=[mid]), count=2))
    assert result.created == 2  # nothing skipped without a check
    assert llm.calls.count("verification") == 2  # the batch + one single re-check


async def test_identical_deterministic_check_is_served_from_cache(session_maker):
    llm = ScriptedLLM()
    gen = QuestionGenerator(llm, Retriever(HashEmbeddings()))
    options = {"A": "o'n kun", "B": "bir oy", "C": "uch kun", "D": "besh yil"}
    with usage.track() as spent:
        first = await gen.verify_existing("Matn.", "Muddat qancha?", options, "A")
        second = await gen.verify_existing("Matn.", "Muddat qancha?", options, "A")
    assert first == second and llm.calls.count("verification") == 1 and spent.cache_hits == 1


async def test_tokens_are_counted_per_job_and_per_day(groq_server):  # noqa: F811
    _state, url = groq_server
    before = usage.today().requests
    with usage.track() as spent:
        await structured_call(_provider(url), "Return JSON", "source", ExplanationResponse, purpose="explanation")
    assert (spent.requests, spent.input_tokens, spent.output_tokens) == (1, 120, 30)
    assert spent.by_purpose == {"explanation": 1}
    assert usage.today().requests == before + 1


async def test_pauses_when_groq_says_the_minute_budget_is_used_up(groq_server, monkeypatch):  # noqa: F811
    state, url = groq_server
    state["headers"] = {"x-ratelimit-remaining-tokens": "100", "x-ratelimit-reset-tokens": "1.5s"}
    monkeypatch.setattr(groq_provider, "_pacer", groq_provider._Pacer())
    slept = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds, *a, **k):
        slept.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(groq_provider.asyncio, "sleep", fake_sleep)
    provider = _provider(url, max_retries=0)
    await structured_call(provider, "Return JSON", "a", ExplanationResponse)
    await structured_call(provider, "Return JSON", "b", ExplanationResponse)
    assert slept and 1.0 < slept[0] <= 1.5  # waited for the reset instead of getting a 429


async def test_concurrent_requests_are_limited(groq_server, monkeypatch):  # noqa: F811
    state, url = groq_server
    state["delay"] = 0.2
    monkeypatch.setattr(groq_provider, "_pacer", groq_provider._Pacer())
    provider = _provider(url, max_retries=0)
    provider._settings = provider._settings.model_copy(update={"groq_max_concurrency": 1})
    await asyncio.gather(*(structured_call(provider, "Return JSON", f"s{i}", ExplanationResponse) for i in range(3)))
    assert state["calls"] == 3 and state["max_active"] == 1


async def test_reprocessing_a_material_does_not_generate_questions_again(tg, session_maker, monkeypatch):  # noqa: F811
    from app.handlers.admin import materials as handler

    mid = await make_material_with_text(session_maker)
    async with session_maker() as session:
        for i in range(20):
            await make_question(session, text_=f"Bankdagi savol {i} nima?", material_id=mid)
    calls = []

    async def fake_generate(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(handler, "_generate_to_bank", fake_generate)
    monkeypatch.setattr(handler, "ai_available", lambda: True)
    msg = await tg.bot.send_message(1000, "…")
    await handler._process_and_report(session_maker, tg.bot, mid, None, 1000, msg.message_id)
    assert calls == []  # the bank already has the 20 questions
    async with session_maker() as session:
        assert len((await session.execute(select(Question))).scalars().all()) == 20


from tests.test_group_flow import tg  # noqa: E402,F401  (fixture)
