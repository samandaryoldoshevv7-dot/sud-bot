"""Scenarios of the second improvement round: auto activation, 📚 Testlarim, personal 24h timer,
quiz-style answering with ✖️ CHIQISH, mixed tests from several files with sources, distribution,
retakes, group start opening a private session, and many employees at once."""

import asyncio
import random
import re
from datetime import timedelta
from itertools import pairwise

from aiogram.methods import AnswerCallbackQuery
from sqlalchemy import select

from app.keyboards.callbacks import AdminCB, AnsCB, EmpCB, GroupStartCB
from app.models import (
    DURATION_CHOICES,
    AttemptStatus,
    DeliveryMode,
    GroupTestPost,
    Test,
    TestAttempt,
    TestAudience,
    TestQuestion,
    TestSource,
    TestStatus,
    UserAnswer,
)
from app.rag.retriever import Retriever
from app.services import assignments, group_tests
from app.services import attempts as attempt_service
from app.services import groups as group_service
from app.services import test_builder as tb
from app.services.attempts import AnswerOutcome, StartError, original_to_display
from app.services.mixed_tests import build_mixed_test, interleave, split_quota
from app.services.question_generation import QuestionGenerator
from app.utils.time import utcnow
from tests.factories import (
    HashEmbeddings,
    ScriptedLLM,
    make_employee,
    make_material_with_text,
    make_question,
    make_test,
)
from tests.telegram_mock import callback_update, document_update, message_update
from tests.test_group_flow import tg  # noqa: F401  (fixture)

ADMIN = 1000
GROUP_CHAT = -1005550001
SOURCES = [
    ("Konstitutsiya", "Konstitutsiya.pdf"),
    ("Ma'muriy kodeks", "Ma'muriy kodeks.pdf"),
    ("Mehnat kodeksi", "Mehnat kodeksi.docx"),
    ("Sug'urta qonunchiligi", "Sug'urta qonunchiligi.pdf"),
]


def _law(name: str) -> str:
    return (
        f"{name.upper()}\n\n"
        f"1-modda. {name} bo'yicha murojaat o'n kun ichida ko'rib chiqiladi va arizachiga yozma javob beriladi.\n\n"
        f"2-modda. {name} talablari buzilgan taqdirda mansabdor shaxs belgilangan tartibda javobgar bo'ladi.\n\n"
        f"3-modda. {name} asosidagi hujjatlar kamida besh yil davomida arxivda saqlanadi va ro'yxatga olinadi.\n\n"
        f"4-modda. {name} bo'yicha nizolar sud tomonidan uch oy muddat ichida hal qilinadi.\n"
    )


async def _materials(session_maker) -> list[int]:
    return [
        await make_material_with_text(session_maker, _law(name), title=name, file_name=file) for name, file in SOURCES
    ]


async def _two_source_test(session_maker, session, n_each=2, **kw) -> Test:
    ids = []
    for name, file in SOURCES[:2]:
        mid = await make_material_with_text(session_maker, _law(name), title=name, file_name=file)
        ids.append(mid)
        for i in range(n_each):
            await make_question(session, text_=f"{name}: {i + 1}-qoida nimani belgilaydi?", material_id=mid)
    kw.setdefault("deadline_at", utcnow() + tb.OPEN_WINDOW)
    test = await make_test(session, question_count=n_each * 2, material_ids=ids, use_all_materials=False,
                           answer_reveal="immediate", **kw)  # fmt: skip
    await tb.assemble_questions(session_maker, test.id, None)
    await tb.mark_ready(session, test.id)
    await tb.publish(session, test.id)
    return test


# ------------------------------------------------------------------------------ helpers


def test_split_and_interleave_never_repeat_a_source():
    assert split_quota(40, 4) == [10, 10, 10, 10]
    assert split_quota(10, 3) == [4, 3, 3]
    rng = random.Random(1)
    groups = [[("K", i) for i in range(10)], [("M", i) for i in range(10)], [("T", i) for i in range(10)],
              [("S", i) for i in range(10)]]  # fmt: skip
    order = interleave(groups, rng)
    assert len(order) == 40
    assert all(a[0] != b[0] for a, b in pairwise(order))


# ------------------------------------------------------------------------------ USER scenario


async def test_user_flow_start_timer_answer_close_next_result(tg, session_maker):  # noqa: F811
    async with session_maker() as session:
        test = await _two_source_test(session_maker, session)
        test_id = test.id
    # /start → automatically active → 📚 Testlarim shows the test as 🟢 Ishlanmagan.
    await tg(message_update(7001, "/start", "Ali"))
    tg.session.clear()
    await tg(message_update(7001, "📚 Testlarim", "Ali"))
    listing = "\n".join(tg.session.texts())
    assert "📚 <b>TESTLARIM</b>" in listing and "🟢 <b>Sud amaliyoti — 2026</b>" in listing
    assert "📝 4 ta savol" in listing and "⏱ 24 soat" in listing and "Ishlanmagan" in listing
    # ▶️ TESTNI BOSHLASH → personal deadline = started_at + 24 hours.
    tg.session.clear()
    await tg(callback_update(7001, EmpCB(a="start", id=test_id).pack(), "Ali"))
    async with session_maker() as session:
        attempt = (await session.execute(select(TestAttempt).where(TestAttempt.test_id == test_id))).scalar_one()
        assert attempt.deadline_at - attempt.started_at == timedelta(hours=24)
        attempt_id = attempt.id
    texts = tg.session.texts()
    assert any("Boshlanish:" in x and "Tugash:" in x for x in texts)
    question = next(x for x in texts if "<b>[1/4]</b>" in x)
    assert "📚 Manba: " in question and "○ <b>A)</b>" in question
    buttons = [b.text for row in tg.session.last_markup().inline_keyboard for b in row]
    assert len(buttons) == 4 and buttons[0].startswith("A) ")  # one tappable button per variant
    # Answer every question; wrong on purpose for the first one.
    for pos in range(4):
        async with session_maker() as session:
            attempt = await session.get(TestAttempt, attempt_id)
            item = attempt.layout[pos]
            tq = await session.get(TestQuestion, item["tq"])
            correct = original_to_display(item, tq.correct_option)
            source = tq.source_name
        letter = next(x for x in "ABCD" if x != correct) if pos == 0 else correct
        tg.session.clear()
        await tg(callback_update(7001, AnsCB(at=attempt_id, pos=pos, o=letter).pack(), "Ali"))
        card = tg.session.texts()[-1]
        if pos == 0:
            assert "❌ <b>NOTO'G'RI JAVOB</b>" in card and f"Sizning javobingiz:\n{letter})" in card
            assert f"✅ <b>TO'G'RI JAVOB:</b>\n{correct})" in card and f"📚 Manba: {source}" in card
        else:
            assert "✅ <b>TO'G'RI JAVOB</b>" in card and f"Sizning javobingiz:\n{letter})" in card
        assert not re.search(r"barakalla|ajoyib|zo'r|super|tabrik|👏", card, re.I)
        assert [b.text for row in tg.session.last_markup().inline_keyboard for b in row] == ["✖️ CHIQISH"]
        # The same question cannot be answered again (B after A).
        other = next(x for x in "ABCD" if x != letter)
        tg.session.clear()
        await tg(callback_update(7001, AnsCB(at=attempt_id, pos=pos, o=other).pack(), "Ali"))
        assert any("allaqachon" in a for a in tg.session.alerts())
        # ✖️ CHIQISH closes the answer window only: the next question comes, the attempt goes on.
        tg.session.clear()
        await tg(callback_update(7001, EmpCB(a="next", id=attempt_id).pack(), "Ali"))
        if pos < 3:
            assert any(f"<b>[{pos + 2}/4]</b>" in x for x in tg.session.texts())
            async with session_maker() as session:
                assert (await session.get(TestAttempt, attempt_id)).status == AttemptStatus.IN_PROGRESS
    final = tg.session.texts()[-1]
    assert "🎯 <b>TEST YAKUNLANDI</b>" in final and "📝 Savollar: 4" in final
    assert "✅ To'g'ri: 3" in final and "❌ Noto'g'ri: 1" in final and "75%" in final and "Sarflangan vaqt" in final
    assert "MANBA BO'YICHA NATIJA" in final and "Konstitutsiya" in final and "Ma'muriy kodeks" in final
    async with session_maker() as session:
        answers = (await session.execute(select(UserAnswer).where(UserAnswer.attempt_id == attempt_id))).scalars().all()
        assert len(answers) == 4 and sum(a.is_correct for a in answers) == 3
    # 🔵 Tugatilgan in the list.
    tg.session.clear()
    await tg(message_update(7001, "📚 Testlarim", "Ali"))
    assert any("🔵" in x and "Tugatilgan" in x for x in tg.session.texts()), tg.session.texts()


# ------------------------------------------------------------------------------ MULTI USER


async def test_three_users_answer_the_same_question_at_once(session_maker):
    async with session_maker() as session:
        test = await _two_source_test(session_maker, session)
        users = [await make_employee(session, 7100 + i, f"Xodim {i} Familiya") for i in range(3)]
    attempts = []
    for user in users:
        async with session_maker() as session:
            result = await attempt_service.start_attempt(session, user, test.id)
            attempts.append(result.attempt)

    async def answer(user, attempt, letter):
        async with session_maker() as session:
            return await attempt_service.submit_answer(session, user, attempt.id, 0, letter)

    results = await asyncio.gather(*(answer(u, a, x) for u, a, x in zip(users, attempts, "ABC", strict=True)))
    assert all(r.outcome == AnswerOutcome.ACCEPTED for r in results)
    async with session_maker() as session:
        rows = (
            await session.execute(
                select(UserAnswer.telegram_id, UserAnswer.selected_display, UserAnswer.attempt_id)
                .where(UserAnswer.test_id == test.id)
                .order_by(UserAnswer.telegram_id)
            )
        ).all()
    assert [(r[0], r[1]) for r in rows] == [(7100, "A"), (7101, "B"), (7102, "C")]
    assert len({r[2] for r in rows}) == 3  # three separate sessions (attempts)
    # User A cannot answer with User B's attempt.
    async with session_maker() as session:
        forged = await attempt_service.submit_answer(session, users[0], attempts[1].id, 1, "A")
        assert forged.outcome == AnswerOutcome.INVALID


# ------------------------------------------------------------------------------ GROUP


async def test_group_start_identifies_user_and_opens_private_session(tg, session_maker):  # noqa: F811
    async with session_maker() as session:
        admin = await make_employee(session, ADMIN, "Admin Bosh")
        group, _ = await group_service.register_group(session, GROUP_CHAT, "Sud xodimlari", admin)
        test = await _two_source_test(session_maker, session, group_id=group.id, delivery_mode=DeliveryMode.GROUP)
        post = await group_tests.ensure_post(session, test, group)
        test_id, post_id = test.id, post.id
    assert await group_tests.send_post(tg.bot, session_maker, post_id)
    header = tg.session.sent_to(GROUP_CHAT)
    assert len(header) == 1 and "📚 <b>YANGI TEST</b>" in header[0].text and "📝 4 ta savol" in header[0].text
    assert "⏱ 24 soat" in header[0].text
    # Ali and Vali press ▶️ TESTNI BOSHLASH at the same time: each gets their own attempt.
    tg.session.clear()
    await asyncio.gather(
        tg(callback_update(7201, GroupStartCB(p=post_id).pack(), "Ali", chat_id=GROUP_CHAT)),
        tg(callback_update(7202, GroupStartCB(p=post_id).pack(), "Vali", chat_id=GROUP_CHAT)),
    )
    urls = [m.url for m in tg.session.requests if isinstance(m, AnswerCallbackQuery)]
    assert urls and all(u and u.endswith(f"start=run_{test_id}") for u in urls)
    async with session_maker() as session:
        attempts = (await session.execute(select(TestAttempt).where(TestAttempt.test_id == test_id))).scalars().all()
        assert sorted(a.user_id for a in attempts) and len(attempts) == 2
    # The deep link opens the private chat and shows Ali's first question there (not in the group).
    tg.session.clear()
    await tg(message_update(7201, f"/start run_{test_id}", "Ali"))
    assert any("<b>[1/4]</b>" in x for x in (m.text for m in tg.session.sent_to(7201)))
    assert tg.session.sent_to(GROUP_CHAT) == []


# ------------------------------------------------------------------------------ ADMIN: 4 files → mixed test


async def test_mixed_test_from_four_files_keeps_sources_and_mixes_them(session_maker):
    ids = await _materials(session_maker)
    llm = ScriptedLLM()
    async with session_maker() as session:
        admin = await make_employee(session, ADMIN, "Admin Bosh")
        a = await make_employee(session, 7301, "Tanlangan Birinchi")
        b = await make_employee(session, 7302, "Tanlangan Ikkinchi")
        outsider = await make_employee(session, 7303, "Tanlanmagan Xodim")
        now = utcnow()
        test = await tb.create_test(
            session,
            tb.TestDraftData(title="Huquq bo'yicha aralash test", question_count=8, starts_at=now,
                             deadline_at=now + tb.OPEN_WINDOW, material_ids=ids, answer_reveal="immediate",
                             audience=TestAudience.USERS),
            admin.id,
        )  # fmt: skip
        await assignments.assign_users(session, test.id, [a.id, b.id])
        test_id = test.id

    report = await build_mixed_test(
        session_maker,
        test_id,
        lambda: QuestionGenerator(llm, Retriever(HashEmbeddings()), random.Random(3)),
        rng=random.Random(5),
    )
    assert report.created == 8 and report.missing == 0, report
    assert set(report.per_source.values()) == {2}  # 8 questions over 4 files
    async with session_maker() as session:
        sources = (await session.execute(select(TestSource).where(TestSource.test_id == test_id))).scalars().all()
        assert sorted((s.source_name, s.original_file_name) for s in sources) == sorted(SOURCES)
        tqs = (
            (
                await session.execute(
                    select(TestQuestion).where(TestQuestion.test_id == test_id).order_by(TestQuestion.position)
                )
            )
            .scalars()
            .all()
        )
        by_id = {s.id: s for s in sources}
        for tq in tqs:  # every question carries its real source
            assert tq.source_id in by_id and tq.source_name == by_id[tq.source_id].source_name
            assert tq.source_file == by_id[tq.source_id].original_file_name
        assert all(x.source_id != y.source_id for x, y in pairwise(tqs))  # mixed order
        await tb.mark_ready(session, test_id)
        await tb.publish(session, test_id)
        # Distribution: only the selected employees see it.
        visible_a = [m.test.id for m in await attempt_service.my_tests(session, a)]
        visible_out = [m.test.id for m in await attempt_service.my_tests(session, outsider)]
        assert test_id in visible_a and test_id not in visible_out
        assert (await attempt_service.start_attempt(session, outsider, test_id)).error == StartError.NOT_ASSIGNED
        attempt = (await attempt_service.start_attempt(session, a, test_id, rng=random.Random(2))).attempt
        order = []
        for item in attempt.layout:  # each employee's own order is still mixed by source
            order.append((await session.get(TestQuestion, item["tq"])).source_id)
        assert all(x != y for x, y in pairwise(order))


async def test_ai_answer_with_wrong_or_missing_source_is_not_used(session_maker):
    ids = await _materials(session_maker)
    from app.rag.vector_store import SourceScope
    from app.services.question_generation import GenerationRequest

    async with session_maker() as session:
        gen = QuestionGenerator(ScriptedLLM(wrong_source=True), Retriever(HashEmbeddings()))
        result = await gen.generate(session, GenerationRequest(scope=SourceScope(material_ids=[ids[0]]), count=2))
        assert result.created == 0 and result.rejected["source_mismatch"] > 0


async def test_admin_creates_mixed_test_through_the_bot(tg, session_maker):  # noqa: F811
    """➕ Test yaratish → name → files → 🔀 → 40/count → 6/12/24/2 kun → selected employees → results."""
    from app.services import background

    async with session_maker() as session:
        await make_employee(session, ADMIN, "Admin Bosh")
        a = await make_employee(session, 7401, "Birinchi Xodim")
    await tg(callback_update(ADMIN, AdminCB(s="ct").pack()))
    assert any("Test nomini kiriting" in x for x in tg.session.texts())
    await tg(message_update(ADMIN, "Huquq bo'yicha umumiy test"))
    assert any("Bir nechta fayl yuborishingiz mumkin" in x for x in tg.session.texts())
    # The admin sends four files; each becomes a source (name, original file name, Telegram file_id).
    for i, (name, file_name) in enumerate(SOURCES):
        content = _law(name).encode()
        tg.session.files[f"file{i}"] = content
        txt_name = file_name.rsplit(".", 1)[0] + ".txt"
        await tg(document_update(ADMIN, f"file{i}", txt_name, len(content)))
    for _ in range(200):
        if background.running_count() == 0:
            break
        await asyncio.sleep(0.05)
    async with session_maker() as session:
        test = (await session.execute(select(Test))).scalar_one()
        test_id = test.id
        from app.models import Material, MaterialStatus

        sources = (await session.execute(select(TestSource).where(TestSource.test_id == test_id))).scalars().all()
        assert sorted(s.source_name for s in sources) == sorted(n for n, _ in SOURCES)
        assert all(s.file_id and s.original_file_name.endswith(".txt") for s in sources)
        for src in sources:  # files were extracted, chunked and indexed
            assert (await session.get(Material, src.material_id)).status == MaterialStatus.READY
            for i in range(2):  # verified questions of each file in the bank (AI is not reachable in tests)
                await make_question(
                    session, text_=f"{src.source_name}: {i + 1}-modda nimani belgilaydi?", material_id=src.material_id
                )
    assert any("Qabul qilingan fayllar: 4" in x for x in tg.session.texts())
    tg.session.clear()
    await tg(callback_update(ADMIN, AdminCB(s="ct_mix", id=test_id).pack()))
    assert any(
        "Konstitutsiya + Ma'muriy kodeks + Mehnat kodeksi + Sug'urta qonunchiligi" in x for x in tg.session.texts()
    )
    await tg(callback_update(ADMIN, AdminCB(s="ct_n", id=test_id, v="8").pack()))
    durations = [b.text for row in tg.session.last_markup().inline_keyboard for b in row if b.text != "❌ Bekor qilish"]
    assert durations[:4] == ["6 soat", "12 soat", "24 soat", "2 kun"] and len(DURATION_CHOICES) == 4
    await tg(callback_update(ADMIN, AdminCB(s="ct_d", id=test_id, v=str(12 * 3600)).pack()))
    await tg(callback_update(ADMIN, AdminCB(s="ct_a", id=test_id, v="one").pack()))
    tg.session.clear()
    await tg(callback_update(ADMIN, AdminCB(s="ct_u", id=test_id, v=str(a.id)).pack()))
    assert any("📝 8 ta savol" in x and "⏱ 12 soat" in x and "Birinchi Xodim" in x for x in tg.session.texts())
    await tg(callback_update(ADMIN, AdminCB(s="ct_go", id=test_id).pack()))
    for _ in range(200):
        if background.running_count() == 0:
            break
        await asyncio.sleep(0.05)
    assert any("Test tayyor va yuborildi" in x for x in tg.session.texts())
    async with session_maker() as session:
        test = await session.get(Test, test_id)
        assert test.status == TestStatus.ACTIVE and test.duration_seconds == 12 * 3600
        assert test.audience == TestAudience.USERS
    # The selected employee was notified with a START button.
    assert any("Sizga yangi test berildi" in m.text for m in tg.session.sent_to(7401))
    # 📋 Natijalar after the employee answered.
    async with session_maker() as session:
        attempt = (await attempt_service.start_attempt(session, a, test_id)).attempt
        await attempt_service.submit_answer(session, a, attempt.id, 0, "A")
        attempt_id = attempt.id
    tg.session.clear()
    await tg(callback_update(ADMIN, AdminCB(s="res_t", id=test_id).pack()))
    page = "\n".join(tg.session.texts())
    assert "Birinchi Xodim" in page and "8 ta savol" in page
    tg.session.clear()
    await tg(callback_update(ADMIN, AdminCB(s="res_a", id=attempt_id).pack()))
    detail = "\n".join(tg.session.texts())
    assert "1-savol" in detail and "Tanladi: A)" in detail and "Manba:" in detail and "Natija:" in detail


# ------------------------------------------------------------------------------ TIMER


async def test_deadline_survives_restart_and_is_enforced_by_server_time(tg, session_maker):  # noqa: F811
    from app.services.scheduler import run_tick

    async with session_maker() as session:
        test = await _two_source_test(session_maker, session)
        user = await make_employee(session, 7501, "Taymer Xodim")
        start = utcnow()
        first = await attempt_service.start_attempt(session, user, test.id, now=start)
        deadline, attempt_id = first.attempt.deadline_at, first.attempt.id
        await attempt_service.submit_answer(session, user, attempt_id, 0, "A", now=start + timedelta(minutes=5))
    # "Restart": a brand-new session; the employee comes back hours later.
    async with session_maker() as session:
        user = await session.get(type(user), user.id)
        again = await attempt_service.start_attempt(session, user, test.id, now=start + timedelta(hours=5))
        assert again.resumed and again.attempt.id == attempt_id and again.attempt.deadline_at == deadline
        assert again.attempt.current_index == 1  # continues from question 2
        late = await attempt_service.submit_answer(
            session, user, attempt_id, 1, "A", now=deadline + timedelta(seconds=1)
        )
        assert late.outcome == AnswerOutcome.EXPIRED  # the server decides, not the client
    # The scheduler expires overdue attempts and tells the employee.
    async with session_maker() as session:
        other = await make_employee(session, 7502, "Ikkinchi Taymer")
        attempt = (await attempt_service.start_attempt(session, other, test.id)).attempt
        attempt.deadline_at = utcnow() - timedelta(seconds=1)
        await session.commit()
    await run_tick(tg.bot, session_maker)
    assert any("TEST VAQTI TUGADI" in m.text for m in tg.session.sent_to(7502))
    # Admin: +12 soat reopens the unfinished attempt; time options are only 6/12/24/48 hours.
    async with session_maker() as session:
        reopened = await attempt_service.add_time(session, attempt.id, 12 * 3600)
        assert reopened.status == AttemptStatus.IN_PROGRESS and reopened.deadline_at > utcnow() + timedelta(hours=11)
        try:
            await tb.set_duration(session, test.id, 3600)
        except tb.TestStateError as exc:
            assert exc.code == "bad_duration"
        else:
            raise AssertionError("1 hour must not be accepted")
        await tb.set_duration(session, test.id, 6 * 3600)
        assert (await session.get(Test, test.id)).duration_seconds == 6 * 3600


async def test_retake_only_with_admin_permission(session_maker):
    async with session_maker() as session:
        test = await _two_source_test(session_maker, session, n_each=1)
        user = await make_employee(session, 7601, "Qayta Topshiruvchi")
        attempt = (await attempt_service.start_attempt(session, user, test.id)).attempt
        for pos in range(2):
            await attempt_service.submit_answer(session, user, attempt.id, pos, "A")
        assert (await attempt_service.start_attempt(session, user, test.id)).error == StartError.ALREADY_COMPLETED
        await assignments.allow_retake(session, test.id, user.id)
        second = await attempt_service.start_attempt(session, user, test.id)
        assert second.error is None and second.attempt.attempt_no == 2
        await attempt_service.submit_answer(session, user, second.attempt.id, 0, "B")
        answers = (await session.execute(select(UserAnswer).where(UserAnswer.user_id == user.id))).scalars().all()
        assert len(answers) == 3  # the first attempt's answers stay untouched
        for pos in (1,):
            await attempt_service.submit_answer(session, user, second.attempt.id, pos, "B")
        # The permission was used once.
        assert (await attempt_service.start_attempt(session, user, test.id)).error == StartError.ALREADY_COMPLETED
        # Taking the test away hides it from the employee.
        await assignments.remove_from_test(session, test.id, user.id)
        assert not await attempt_service.user_in_test_audience(session, test, user)


async def test_paused_test_blocks_start_and_answers(session_maker):
    async with session_maker() as session:
        test = await _two_source_test(session_maker, session, n_each=1)
        user = await make_employee(session, 7701, "Pauza Xodim")
        attempt = (await attempt_service.start_attempt(session, user, test.id)).attempt
        await tb.set_paused(session, test.id, True)
        assert (await attempt_service.submit_answer(session, user, attempt.id, 0, "A")).outcome == AnswerOutcome.PAUSED
        other = await make_employee(session, 7702, "Boshqa Pauza")
        assert (await attempt_service.start_attempt(session, other, test.id)).error == StartError.PAUSED
        await tb.set_paused(session, test.id, False)
        assert (
            await attempt_service.submit_answer(session, user, attempt.id, 0, "A")
        ).outcome == AnswerOutcome.ACCEPTED
        # Force end, then reopen.
        await tb.close_test(session, test.id)
        assert (await session.get(Test, test.id)).status == TestStatus.CLOSED
        await tb.reopen_test(session, test.id)
        assert (await session.get(Test, test.id)).status == TestStatus.ACTIVE


def test_no_praise_words_in_texts():
    from app.locales.uz import TEXTS

    joined = "\n".join(str(v) for v in TEXTS.values())
    for word in ("Barakalla", "Ajoyib", "Zo'r", "Super", "Juda yaxshi", "Tabriklaymiz", "👏"):
        assert word.lower() not in joined.lower(), word


async def test_group_post_records_only_header_and_test_appears_for_group(session_maker):
    async with session_maker() as session:
        admin = await make_employee(session, ADMIN, "Admin Bosh")
        group, _ = await group_service.register_group(session, GROUP_CHAT, "Sud xodimlari", admin)
        test = await _two_source_test(session_maker, session, group_id=group.id, delivery_mode=DeliveryMode.GROUP)
        post = await group_tests.ensure_post(session, test, group)
        assert not await group_tests.post_has_questions(session, post.id)
        assert (await session.execute(select(GroupTestPost))).scalar_one().id == post.id
        user = await make_employee(session, 7801, "Guruh Azosi")
        await group_service.mark_membership(session, group, user, True)
        assert test.id in [m.test.id for m in await attempt_service.my_tests(session, user)]
        start = await attempt_service.start_attempt(session, user, test.id)
        assert start.error is None


async def test_material_cut_off_by_restart_is_processed_again(tg, session_maker):  # noqa: F811
    """A restart during processing used to leave the material "⏳ PROCESSING" forever."""
    from app.handlers.admin.materials import resume_interrupted_materials
    from app.models import FileType, Material, MaterialStatus
    from app.services import materials as material_service

    content = _law("Konstitutsiya").encode()
    tg.session.files["stuckfile"] = content
    async with session_maker() as session:
        await make_employee(session, ADMIN, "Admin Bosh")
        stuck = await material_service.create_material(
            session, title="Konstitutsiya", file_type=FileType.TXT, uploaded_by_id=None,
            file_name="Konstitutsiya.txt", telegram_file_id="stuckfile",
        )  # fmt: skip
        stuck.status = MaterialStatus.PROCESSING  # the previous process died here
        await session.commit()
        await session.refresh(stuck)
        stuck_id = stuck.id
        assert not material_service.is_stale(stuck)  # just started: the admin cannot restart it yet
        assert material_service.is_stale(stuck, utcnow() + timedelta(minutes=16))
    assert await resume_interrupted_materials(tg.bot, session_maker, delay=0) == 1
    async with session_maker() as session:
        material = await session.get(Material, stuck_id)
        assert material.status == MaterialStatus.READY and material.chunk_count > 0
    assert any("qayta o'qildi va tayyor" in m.text for m in tg.session.sent_to(ADMIN))


async def test_material_that_crashes_the_bot_again_is_not_retried_forever(tg, session_maker):  # noqa: F811
    from app.handlers.admin.materials import resume_interrupted_materials
    from app.models import BotSetting, FileType, Material, MaterialStatus
    from app.services import materials as material_service

    async with session_maker() as session:
        await make_employee(session, ADMIN, "Admin Bosh")
        big = await material_service.create_material(
            session, title="Juda katta", file_type=FileType.TXT, uploaded_by_id=None, telegram_file_id="bigfile"
        )
        big.status = MaterialStatus.PROCESSING
        session.add(BotSetting(key=f"material_resume:{big.id}", value=1))  # the retry after a crash also died
        await session.commit()
        big_id = big.id
    assert await resume_interrupted_materials(tg.bot, session_maker, delay=0) == 1
    async with session_maker() as session:
        material = await session.get(Material, big_id)
        assert material.status == MaterialStatus.FAILED and material.error_message.startswith("crashed")
        assert await session.get(BotSetting, f"material_resume:{big_id}") is None
    assert any("ikki marta ishdan chiqdi" in m.text for m in tg.session.sent_to(ADMIN))


class _RateLimitedLLM(ScriptedLLM):
    """Answers normally for the first ``ok_calls`` generation calls, then hits a daily limit."""

    def __init__(self, ok_calls: int):
        super().__init__()
        self.ok_calls = ok_calls

    async def complete_json(self, messages, **kw):
        from app.ai.base import AIProviderError

        if "exam author" in messages[0]["content"]:
            if self.calls.count("generation") >= self.ok_calls:
                self.calls.append("limited")
                raise AIProviderError("AI daily rate limit exceeded; retry in 7 min")
        return await super().complete_json(messages, **kw)


async def test_rate_limit_stops_build_quickly_and_resume_reuses_questions(tg, session_maker, monkeypatch):  # noqa: F811
    """Was: "8/30" for an hour while every request waited on the AI limit."""
    from app.handlers.admin.create_test import build_and_send, notify_interrupted_tests

    ids = await _materials(session_maker)
    async with session_maker() as session:
        admin = await make_employee(session, ADMIN, "Admin Bosh")
        now = utcnow()
        test = await tb.create_test(
            session,
            tb.TestDraftData(title="Limitli test", question_count=8, starts_at=now, deadline_at=now + tb.OPEN_WINDOW,
                             material_ids=ids, answer_reveal="immediate"),
            admin.id,
        )  # fmt: skip
        test_id = test.id
    limited = _RateLimitedLLM(ok_calls=2)
    report = await build_mixed_test(
        session_maker, test_id, lambda: QuestionGenerator(limited, Retriever(HashEmbeddings())), rng=random.Random(1)
    )
    assert 0 < report.created < 8 and "rate limit" in report.error
    assert limited.calls.count("limited") == 1  # stopped at the first limit, no waiting loop
    # The admin sees why and can continue later; questions made so far are reused.
    msg = await tg.bot.send_message(ADMIN, "…")
    import app.handlers.admin.create_test as ct

    monkeypatch.setattr(ct, "make_generator", lambda: QuestionGenerator(ScriptedLLM(), Retriever(HashEmbeddings())))
    monkeypatch.setattr(ct, "ai_available", lambda: True)
    tg.session.clear()
    await build_and_send(session_maker, tg.bot, test_id, ADMIN, msg.message_id)
    assert any("Test tayyor va yuborildi" in x for x in tg.session.texts())
    async with session_maker() as session:
        assert (await session.get(Test, test_id)).status == TestStatus.ACTIVE
    # After a restart the admins are told which builds stopped, with a continue button.
    async with session_maker() as session:
        draft = await make_test(session, title="Uzilgan", question_count=3, material_ids=ids[:1],
                                use_all_materials=False)  # fmt: skip
    tg.session.clear()
    await notify_interrupted_tests(tg.bot, session_maker, [draft.id])
    sent = tg.session.sent_to(ADMIN)
    assert sent and "to'xtadi" in sent[0].text
    assert sent[0].reply_markup.inline_keyboard[0][0].text == "🔄 Davom ettirish"


def test_log_calls_never_crash_on_reserved_extra_keys():
    """Was: test building failed with "Attempt to overwrite 'created' in LogRecord"."""
    import logging
    import pathlib

    from app.services import mixed_tests

    records = []

    class Keep(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler, old_level, old_disabled = Keep(), mixed_tests.logger.level, mixed_tests.logger.disabled
    mixed_tests.logger.addHandler(handler)
    mixed_tests.logger.setLevel(logging.INFO)
    mixed_tests.logger.disabled = False  # alembic's fileConfig (test DB setup) disables existing loggers
    try:
        mixed_tests.logger.info("x", extra={"created": 1, "message": "m", "name": "n"})
    finally:
        mixed_tests.logger.removeHandler(handler)
        mixed_tests.logger.setLevel(old_level)
        mixed_tests.logger.disabled = old_disabled
    assert records and records[-1].created_ == 1
    reserved = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__)
    for path in pathlib.Path("app").rglob("*.py"):
        for match in re.finditer(r"extra=\{([^}]*)\}", path.read_text(), re.S):
            keys = set(re.findall(r'"([a-zA-Z_]+)"\s*:', match.group(1)))
            assert not keys & reserved, (path, keys & reserved)
