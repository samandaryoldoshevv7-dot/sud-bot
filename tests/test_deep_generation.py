"""🧠 Maksimal savol yaratish: up to 300 unique questions per material, never padded, no duplicates
(also across materials and after a restart), medium/hard half and half, a pause on an API limit
that resumes from the checkpoint without making saved questions again."""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter

from sqlalchemy import func, select

from app.ai.base import AIProviderError
from app.keyboards.callbacks import AdminCB
from app.models import Difficulty, MaterialGenerationJob, Question, SourceChunk
from app.rag.retriever import Retriever
from app.services import dedup
from app.services import deep_generation as deep
from app.services.question_generation import QuestionGenerator
from app.utils.text import text_hash
from tests.factories import HashEmbeddings, ScriptedLLM, make_employee, make_material_with_text, make_question
from tests.telegram_mock import callback_update
from tests.test_group_flow import tg  # noqa: F401  (fixture)

ADMIN = 1000
ORGANS = ["Sudya", "Kotib", "Ijrochi", "Prokuror", "Advokat", "Tergovchi", "Ekspert", "Notarius", "Mediator",
          "Hakam", "Tarjimon", "Arxivchi", "Rais", "Inspektor", "Hisobchi", "Kuryer", "Pristav"]  # fmt: skip
ACTIONS = ["arizani ko'rib chiqishi", "hujjatni arxivga topshirishi", "shikoyatni ro'yxatga olishi",
           "qarorni yuborishi", "dalillarni saqlashi", "bayonnomani rasmiylashtirishi", "nusxani berishi",
           "xulosani taqdim etishi", "chaqiruv qog'ozini jo'natishi", "hisobotni tayyorlashi",
           "ijro varaqasini qaytarishi", "so'rovga javob berishi", "materialni o'rganishi"]  # fmt: skip


def law_text(n: int, start: int = 1) -> str:
    """``n`` articles, each one distinct rule (another body, action and deadline)."""
    lines = []
    for i in range(start, start + n):
        organ, action = ORGANS[i % len(ORGANS)], ACTIONS[(i // len(ORGANS)) % len(ACTIONS)]
        lines.append(f"{i}-modda. {organ} {action} uchun {i + 4} kunlik muddat belgilanadi.")
    return "\n".join(lines)


RULE = re.compile(r"(\d+)-modda\. (\w+) (.+?) uchun (\d+) kunlik muddat belgilanadi\.")


class FactLLM(ScriptedLLM):
    """A model with a finite amount to say: one question per article of the chunk it gets, never one
    the prompt lists as already covered, and "no new facts" when the chunk has nothing left.

    ``paraphrase``: also re-asks one covered rule in other words (a duplicate the server must catch).
    ``limit_after``: the API answers "daily rate limit" after that many generation requests."""

    def __init__(self, paraphrase: bool = False, limit_after: int | None = None):
        super().__init__()
        self.paraphrase = paraphrase
        self.limit_after = limit_after

    async def complete_json(self, messages, **kw) -> str:
        if "exam author" in messages[0]["content"] and self.limit_after is not None:
            if self.calls.count("generation") >= self.limit_after:
                raise AIProviderError("AI daily rate limit exceeded; retry in 42 min")
        return await super().complete_json(messages, **kw)

    def _generate(self, user: str) -> str:
        count = int(re.search(r"Write (\d+) multiple-choice", user).group(1))
        levels = re.search(r"in order\): ([a-z, ]+)\.", user).group(1).split(", ")
        chunk_id, attrs, chunk_text = re.search(r'<chunk id="(\d+)"([^>]*)>\n(.*?)\n</chunk>', user, re.S).groups()
        source = re.search(r'source="([^"]*)"', attrs).group(1)
        source_file = re.search(r'source_file="([^"]*)"', attrs).group(1)
        covered = set()
        if "ALREADY COVERED" in user:
            block = user.split("ALREADY COVERED", 1)[1].split("\n")[1:]
            covered = {line[2:].strip() for line in block if line.startswith("- ")}
        rules = [m for m in RULE.finditer(chunk_text) if m.group(0) not in covered]
        picks: list[tuple[re.Match, bool]] = []
        if self.paraphrase and covered:
            old = next((m for m in RULE.finditer(chunk_text) if m.group(0) in covered), None)
            if old is not None:
                picks.append((old, True))
        picks += [(m, False) for m in rules]
        picks = picks[:count]
        if not picks or all(p for _m, p in picks):
            return json.dumps({"status": "rejected", "rejection_reason": "no new facts", "questions": []})
        questions = []
        for k, (m, paraphrased) in enumerate(picks):
            article, organ, action, days = m.group(1), m.group(2), m.group(3), int(m.group(4))
            if paraphrased:  # the same rule and answer, other words
                qtext = f"Qaysi muddat ichida {organ.lower()} {action} lozim ({article}-modda)?"
                fact = f"{action} uchun {organ} {days} kunlik muddat {article}-modda belgilanadi"
            else:
                qtext = f"{article}-moddaga ko'ra {organ.lower()} {action} uchun qancha muddat belgilanadi?"
                fact = m.group(0)
            values = [f"{days} kun", f"{days + 2} kun", f"{days + 5} kun", f"{days + 9} kun"]
            options = dict(zip("ABCD", values, strict=True))
            self._answers[qtext] = "A"
            questions.append({
                "question": qtext, "options": options, "correct_answer": "A",
                "explanation": f"{article}-moddada {days} kunlik muddat belgilangan.",
                "topic": "Muddatlar", "difficulty": levels[k % len(levels)], "fact": fact,
                "article": f"{article}-modda", "source_chunk_id": int(chunk_id), "source_reference": "ref",
                "source_excerpt": m.group(0), "source": source, "source_file": source_file,
            })  # fmt: skip
        return json.dumps({"status": "ok", "questions": questions, "rejection_reason": None}, ensure_ascii=False)


def factory(llm):
    return lambda: QuestionGenerator(llm, Retriever(HashEmbeddings()))


async def _questions(session_maker, material_id=None) -> list[Question]:
    async with session_maker() as session:
        stmt = select(Question).order_by(Question.id)
        if material_id is not None:
            stmt = stmt.where(Question.source_material_id == material_id)
        return list((await session.execute(stmt)).unique().scalars())


async def test_small_material_stops_at_what_it_really_has(session_maker):
    mid = await make_material_with_text(session_maker, law_text(40), title="Kichik qonun", file_name="kichik.pdf")
    llm = FactLLM(paraphrase=True)
    progress_calls = []

    async def progress(have, target, done, chunks):
        progress_calls.append((have, target))

    report = await deep.run(session_maker, mid, factory(llm), progress)
    assert report.status == deep.EXHAUSTED
    rows = await _questions(session_maker, mid)
    assert report.total == len(rows) == 40  # every article once, nothing padded
    assert len({q.fact_hash for q in rows}) == 40
    assert report.duplicates_now > 0  # the re-worded questions were caught on the server
    assert report.duplicate_reasons["same_rule"] > 0
    assert progress_calls and progress_calls[-1][1] == deep.MAX_QUESTIONS

    # Format: 4 different options, one correct one, not always in the same place; source and article.
    assert len({q.correct_option for q in rows}) >= 3
    for q in rows:
        assert set(q.options) == set("ABCD") and len(set(q.options.values())) == 4
        assert q.options[q.correct_option] == f"{int(q.article_ref.split('-')[0]) + 4} kun"
        assert q.validation["verifier_answer"] == q.correct_option
        assert q.source_material_id == mid and q.source_chunk_id and q.fact and q.answer_hash
        assert q.difficulty in (Difficulty.MEDIUM, Difficulty.HARD)
    levels = Counter(q.difficulty for q in rows)
    assert abs(levels[Difficulty.MEDIUM] - levels[Difficulty.HARD]) <= 2  # half and half

    async with session_maker() as session:
        job = await session.get(MaterialGenerationJob, mid)
        assert job.status == deep.EXHAUSTED and job.created == 40 and job.finished_at
        assert job.duplicates == report.duplicates_now
        chunks = (await session.execute(select(SourceChunk).where(SourceChunk.material_id == mid))).scalars().all()
        assert all(c.gen_exhausted for c in chunks)

    # Running again adds nothing (the material has no unique questions left) and asks almost nothing.
    before = llm.calls.count("generation")
    again = await deep.run(session_maker, mid, factory(llm))
    assert again.status == deep.EXHAUSTED and again.created_now == 0 and again.total == 40
    assert llm.calls.count("generation") == before


async def test_never_more_than_300(session_maker):
    mid = await make_material_with_text(session_maker, law_text(330), title="Katta kodeks", file_name="katta.pdf")
    report = await deep.run(session_maker, mid, factory(FactLLM()), target=1000)
    assert report.target == deep.MAX_QUESTIONS == 300
    assert report.status == deep.DONE
    rows = await _questions(session_maker, mid)
    assert len(rows) == report.total == 300
    assert len({q.text_hash for q in rows}) == 300 and len({q.fact_hash for q in rows}) == 300
    levels = Counter(q.difficulty for q in rows)
    assert levels[Difficulty.MEDIUM] == levels[Difficulty.HARD] == 150


async def test_api_limit_pauses_and_resume_does_not_repeat(session_maker):
    mid = await make_material_with_text(session_maker, law_text(60), title="Qonun", file_name="qonun.pdf")
    llm = FactLLM(limit_after=3)
    report = await deep.run(session_maker, mid, factory(llm))
    # Not "exhausted": the quota ran out, the material did not.
    assert report.status == deep.PAUSED and "rate limit" in report.error
    saved = await _questions(session_maker, mid)
    assert len(saved) == report.total == 9  # 3 requests × 3 questions, all kept
    async with session_maker() as session:
        job = await session.get(MaterialGenerationJob, mid)
        assert job.status == deep.PAUSED and job.cursor >= 0
        stats = await deep.material_stats(session, mid)
        assert stats.remaining_chunks > 0

    # Quota is back: the run continues from the checkpoint.
    llm.limit_after = None
    resumed = await deep.run(session_maker, mid, factory(llm))
    assert resumed.status == deep.EXHAUSTED and resumed.total == 60
    rows = await _questions(session_maker, mid)
    assert len(rows) == 60 and len({q.fact_hash for q in rows}) == 60
    assert [q.id for q in rows[:9]] == [q.id for q in saved]  # the first 9 were not made again


async def test_restart_marks_run_interrupted_and_it_resumes(session_maker):
    mid = await make_material_with_text(session_maker, law_text(30), title="Qonun", file_name="q.pdf")
    async with session_maker() as session:
        job = await deep.claim(session, mid)
        assert job is not None and job.status == deep.RUNNING
        assert await deep.claim(session, mid) is None  # one run per material at a time
    busy = await deep.run(session_maker, mid, factory(FactLLM()))
    assert busy.error == "already_running"
    async with session_maker() as session:
        assert await deep.mark_interrupted(session) == [mid]  # what startup does after a restart
        assert (await session.get(MaterialGenerationJob, mid)).status == deep.INTERRUPTED
    report = await deep.run(session_maker, mid, factory(FactLLM()))
    assert report.status == deep.EXHAUSTED and report.total == 30


async def test_same_rules_in_another_material_are_not_repeated(session_maker):
    first = await make_material_with_text(session_maker, law_text(20), title="Asl", file_name="asl.pdf")
    await deep.run(session_maker, first, factory(FactLLM()))
    # The second file repeats articles 11–20 and adds 21–30.
    second = await make_material_with_text(session_maker, law_text(20, start=11), title="Nusxa", file_name="n.pdf")
    report = await deep.run(session_maker, second, factory(FactLLM()))
    assert report.total == 10 and report.duplicates_now >= 10
    facts = [q.fact for q in await _questions(session_maker)]
    assert len(facts) == len(set(facts)) == 30


async def test_concurrent_runs_on_two_materials_do_not_save_the_same_rule(session_maker):
    a = await make_material_with_text(session_maker, law_text(15), title="A", file_name="a.pdf")
    b = await make_material_with_text(session_maker, law_text(15), title="B", file_name="b.pdf")
    await asyncio.gather(deep.run(session_maker, a, factory(FactLLM())), deep.run(session_maker, b, factory(FactLLM())))
    facts = [q.fact_hash for q in await _questions(session_maker)]
    assert len(facts) == len(set(facts)) == 15


def test_duplicate_rules_are_precise():
    def e(q, a, f):
        return dedup.entry_for(q, a, f, text_hash(q))

    index = dedup.DedupIndex()
    index.add(e("5-moddaga ko'ra sudya arizani qancha muddatda ko'rib chiqadi?", "10 kun",
                "5-modda. Sudya arizani ko'rib chiqishi uchun 10 kunlik muddat belgilanadi."))  # fmt: skip
    # Re-worded, same rule and answer: a duplicate.
    assert index.find(e("Sudya arizani necha kun ichida ko'rib chiqishi kerak (5-modda)?", "10 kun",
                        "Arizani ko'rib chiqishi uchun sudya 10 kunlik muddat 5-modda belgilanadi."))  # fmt: skip
    # The same words, another deadline: not a duplicate.
    assert index.find(e("6-moddaga ko'ra sudya arizani qancha muddatda ko'rib chiqadi?", "15 kun",
                        "6-modda. Sudya arizani ko'rib chiqishi uchun 15 kunlik muddat belgilanadi.")) is None  # fmt: skip
    # The same words, another body: not a duplicate.
    assert index.find(e("5-moddaga ko'ra prokuror arizani qancha muddatda ko'rib chiqadi?", "10 kun",
                        "5-modda. Prokuror arizani ko'rib chiqishi uchun 10 kunlik muddat belgilanadi.")) is None  # fmt: skip
    # Same question, another answer: not a duplicate either.
    assert index.find(e("5-moddaga ko'ra sudya arizani qayerga yuboradi?", "arxivga",
                        "5-modda. Sudya arizani arxivga yuboradi.")) is None  # fmt: skip


async def test_database_blocks_a_second_copy_of_the_same_rule(session_maker):
    from sqlalchemy.exc import IntegrityError

    mid = await make_material_with_text(session_maker)
    async with session_maker() as session:
        q1 = await make_question(session, text_="Birinchi savol matni?", material_id=mid)
        q1.fact_hash = dedup.key_hash("Ariza 3 kunda ko'riladi")
        await session.commit()
        q2 = await make_question(session, text_="Boshqa savol matni?", material_id=mid)
        q2.fact_hash = q1.fact_hash
        try:
            await session.commit()
            raised = False
        except IntegrityError:
            raised = True
        assert raised


async def test_material_page_shows_question_stats_and_deep_button(tg, session_maker, monkeypatch):  # noqa: F811
    mid = await make_material_with_text(session_maker, law_text(12), title="Qonun", file_name="qonun.pdf")
    await deep.run(session_maker, mid, factory(FactLLM(limit_after=2)))  # paused by the API limit
    async with session_maker() as session:
        await make_employee(session, ADMIN, "Admin Bosh")
    await tg(callback_update(ADMIN, AdminCB(s="mat_v", id=mid).pack()))
    text = tg.session.texts()[-1]
    assert "Jami: <b>6</b> ta (o'rtacha 3 · qiyin 3)" in text
    assert "To'xtatilgan" in text and "tugaganini anglatmaydi" in text  # paused ≠ exhausted
    assert "Takror deb topilib saqlanmagan: 0" in text and "Qamrov:" in text
    buttons = [b.text for row in tg.session.last_markup().inline_keyboard for b in row]
    assert "🔄 Savol yaratishni davom ettirish" in buttons
    async with session_maker() as session:
        total = (await session.execute(select(func.count(Question.id)))).scalar_one()
    assert total == 6


async def test_pack_makes_unique_questions_then_splits_them_into_tests(tg, session_maker, monkeypatch):  # noqa: F811
    """📦 300 tagacha, 20 tadan: two files hold 55 unique rules → 2 full tests of 20 are sent, the rest
    (15) is offered as a part-filled third; no question repeats between the tests."""
    from app.handlers.admin import create_test as ct
    from app.models import BotSetting, Material, Test, TestQuestion, TestStatus
    from app.services import test_builder as tb

    llm = FactLLM()
    monkeypatch.setattr(ct, "make_generator", factory(llm))
    monkeypatch.setattr(ct, "ai_available", lambda: True)
    a = await make_material_with_text(session_maker, law_text(30), title="Kodeks", file_name="kodeks.pdf")
    b = await make_material_with_text(session_maker, law_text(25, start=31), title="Qonun", file_name="qonun.pdf")
    async with session_maker() as session:
        admin = await make_employee(session, ADMIN, "Admin Bosh")
        now = tb.utcnow()
        draft = await tb.create_test(
            session,
            tb.TestDraftData(title="Katta test", question_count=20, starts_at=now, deadline_at=now + tb.OPEN_WINDOW,
                             material_ids=[a, b], answer_reveal="immediate"),
            admin.id,
        )  # fmt: skip
        for mid in (a, b):
            await tb.ensure_source(session, draft.id, await session.get(Material, mid))
        await session.commit()
        draft_id = draft.id

    # The admin's choice: 📦 300 tagacha, 15 ta test × 20 ta.
    await tg(callback_update(ADMIN, AdminCB(s="ct_n", id=draft_id, v="20").pack()))
    buttons = [x.text for row in tg.session.last_markup().inline_keyboard for x in row]
    assert "📦 300 tagacha: 15 ta test × 20 ta = 300 ta savol" in buttons
    await tg(callback_update(ADMIN, AdminCB(s="ct_vn", id=draft_id, v="max").pack()))
    async with session_maker() as session:
        assert await ct.is_pack(session, draft_id) and await ct.variant_count(session, draft_id) == 15

    msg = await tg.bot.send_message(ADMIN, "⏳")
    tg.session.clear()
    await ct.build_and_send(session_maker, tg.bot, draft_id, ADMIN, msg.message_id)
    summary = tg.session.texts()[-1]
    assert "✅ 1-variant — 20 ta savol" in summary and "✅ 2-variant — 20 ta savol" in summary
    assert "3-variant — 15/20" in summary  # the rest, not sent: the admin may send it as is
    assert "jami <b>55 ta</b> noyob savol" in summary
    assert any("noyob savollar tuzilmoqda" in x for x in tg.session.texts())

    async with session_maker() as session:
        assert len((await session.execute(select(Question))).unique().scalars().all()) == 55
        tests = (
            (await session.execute(select(Test).where(Test.title.like("Katta test — %")).order_by(Test.id)))
            .scalars()
            .all()
        )
        assert len(tests) == 3  # no empty 4th…15th tests
        assert [x.status for x in tests[:2]] == [TestStatus.ACTIVE, TestStatus.ACTIVE]
        seen: set[int] = set()
        for x in tests:
            ids = list(
                (await session.execute(select(TestQuestion.question_id).where(TestQuestion.test_id == x.id))).scalars()
            )
            assert not seen & set(ids)
            seen |= set(ids)
        assert len(seen) == 55
        assert await session.get(BotSetting, f"ct_pack:{draft_id}") is None
