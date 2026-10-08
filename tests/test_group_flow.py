"""Tests running INSIDE a Telegram group: shared question messages, one answer per user/question."""

import asyncio
from datetime import timedelta

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.keyboards.callbacks import AdminCB, GroupAnsCB, GroupStartCB
from app.models import (
    AttemptStatus,
    DeliveryMode,
    GroupQuestionMessage,
    GroupTestPost,
    ParticipationStatus,
    Test,
    TestAttempt,
    TestStatus,
    UserAnswer,
)
from app.services import group_tests
from app.services import groups as group_service
from app.services import test_builder as tb
from app.services.group_tests import GroupAnswerCode, answer_alert
from app.statistics.participation import participation
from app.utils.time import utcnow
from tests.factories import make_employee, make_material_with_text, make_question, make_test, tg_user
from tests.telegram_mock import RecordingSession, callback_update

GROUP_CHAT = -1001234567890
ADMIN = 1000


async def _group_test(session_maker, session, n=3, reveal="after"):
    mid = await make_material_with_text(session_maker)
    for i in range(n + 1):
        await make_question(session, text_=f"Guruh savoli {i} qaysi qoida?", material_id=mid, correct="ABCD"[i % 4])
    admin = await make_employee(session, ADMIN, "Admin Bosh")
    group, _ = await group_service.register_group(session, GROUP_CHAT, "Sud xodimlari", admin)
    test = await make_test(session, question_count=n, group_id=group.id, delivery_mode=DeliveryMode.GROUP,
                           answer_reveal=reveal, randomize_options=True)  # fmt: skip
    await tb.assemble_questions(session_maker, test.id, None)
    await tb.mark_ready(session, test.id)
    await tb.publish(session, test.id)
    await session.refresh(test)
    post = await group_tests.ensure_post(session, test, group)
    msgs = list(
        (await session.execute(select(GroupQuestionMessage).where(GroupQuestionMessage.post_id == post.id)
                               .order_by(GroupQuestionMessage.position))).scalars().all()
    )  # fmt: skip
    return test, post, msgs


async def _correct_display(session, gqm):
    from app.models import TestQuestion

    tq = await session.get(TestQuestion, gqm.test_question_id)
    return "ABCD"[gqm.opts.index(tq.correct_option)]


async def test_join_answer_once_and_duplicate_rejected(session_maker, session):
    test, post, msgs = await _group_test(session_maker, session)
    # 1. user joins via ▶️ TESTNI BOSHLASH in the group
    code, attempt = await group_tests.group_start(session, tg_user(5001, "Ali", "Aliyev"), post.id)
    assert code == GroupAnswerCode.ACCEPTED and attempt.status == AttemptStatus.IN_PROGRESS
    # 2. user selects A
    first = await group_tests.submit_group_answer(session, tg_user(5001, "Ali", "Aliyev"), msgs[0].id, "A")
    assert first.code == GroupAnswerCode.ACCEPTED and first.selected == "A"
    # 3. second click on B is rejected
    second = await group_tests.submit_group_answer(session, tg_user(5001, "Ali", "Aliyev"), msgs[0].id, "B")
    assert second.code == GroupAnswerCode.DUPLICATE and second.selected == "A"
    assert answer_alert(second).startswith("⚠️ Bu savolga siz allaqachon javob bergansiz.")
    # 4. B was NOT stored
    rows = (await session.execute(select(UserAnswer).where(UserAnswer.test_id == test.id))).scalars().all()
    assert len(rows) == 1 and rows[0].selected_display == "A" and rows[0].telegram_id == 5001
    assert rows[0].selected_option == msgs[0].opts[0]  # real (snapshot) option behind display letter A
    assert rows[0].selected_option_id is not None


async def test_two_users_answers_are_separate(session_maker, session):
    _test, _post, msgs = await _group_test(session_maker, session)
    correct = await _correct_display(session, msgs[0])
    wrong = next(x for x in "ABCD" if x != correct)
    a = await group_tests.submit_group_answer(session, tg_user(5101, "Ali"), msgs[0].id, correct)
    b = await group_tests.submit_group_answer(session, tg_user(5102, "Vali"), msgs[0].id, wrong)
    assert a.is_correct is True and b.is_correct is False
    answers = (await session.execute(select(UserAnswer).order_by(UserAnswer.telegram_id))).scalars().all()
    assert [(x.telegram_id, x.selected_display, x.is_correct) for x in answers] == [
        (5101, correct, True), (5102, wrong, False)
    ]  # fmt: skip
    assert answers[0].attempt_id != answers[1].attempt_id


async def test_full_completion_scoring_and_feedback_modes(session_maker, session):
    test, _post, msgs = await _group_test(session_maker, session, n=3, reveal="immediate")
    user = tg_user(5201, "Karim")
    results = []
    for i, gqm in enumerate(msgs):
        correct = await _correct_display(session, gqm)
        letter = correct if i < 2 else next(x for x in "ABCD" if x != correct)
        results.append(await group_tests.submit_group_answer(session, user, gqm.id, letter))
    assert results[-1].finished
    alert = answer_alert(results[-1])
    assert "To'g'ri javob" in alert and "2/3" in alert and len(alert) <= 200
    attempt = results[-1].attempt
    assert attempt.status == AttemptStatus.COMPLETED and attempt.correct_count == 2 and attempt.incorrect_count == 1
    # Hidden mode: the correct answer is NOT revealed.
    test.answer_reveal = test.answer_reveal.__class__("after")
    await session.commit()
    r = await group_tests.submit_group_answer(session, tg_user(5202, "Hidden"), msgs[0].id, "A")
    assert answer_alert(r).startswith("✅ Javobingiz qabul qilindi.") and "To'g'ri javob" not in answer_alert(r)


async def test_deadline_closes_test_and_rejects_answers(session_maker, session):
    test, _post, msgs = await _group_test(session_maker, session)
    await group_tests.submit_group_answer(session, tg_user(5301, "Erta"), msgs[0].id, "A")
    later = test.deadline_at + timedelta(seconds=1)
    # 8. after end_at no answer is accepted (even before the scheduler runs)
    late = await group_tests.submit_group_answer(session, tg_user(5302, "Kech"), msgs[0].id, "A", now=later)
    assert late.code == GroupAnswerCode.EXPIRED and "TEST MUDDATI TUGADI" in answer_alert(late)
    # 7. the scheduler expires the test and the open attempt
    assert await tb.expire_due(session, later) == [test.id]
    await session.refresh(test)
    assert test.status == TestStatus.EXPIRED
    open_attempt = (await session.execute(select(TestAttempt).where(TestAttempt.test_id == test.id))).scalar_one()
    assert open_attempt.status == AttemptStatus.EXPIRED
    after = await group_tests.submit_group_answer(session, tg_user(5301, "Erta"), msgs[1].id, "A")
    assert after.code == GroupAnswerCode.EXPIRED
    not_yet = await group_tests.submit_group_answer(session, tg_user(5303, "Oldin"), msgs[0].id, "A",
                                                    now=test.starts_at - timedelta(minutes=1))  # fmt: skip
    assert not_yet.code in (GroupAnswerCode.NOT_STARTED, GroupAnswerCode.EXPIRED)
    summary = await participation(session, test, later)
    assert summary.count(ParticipationStatus.EXPIRED) == 1


async def test_duplicate_insert_and_update_blocked_by_database(session_maker, session):
    _test, _post, msgs = await _group_test(session_maker, session)
    await group_tests.submit_group_answer(session, tg_user(5401, "Db"), msgs[0].id, "A")
    answer = (await session.execute(select(UserAnswer))).scalar_one()
    answer_id = answer.id
    data = {c.name: getattr(answer, c.name) for c in UserAnswer.__table__.columns if c.name != "id"}
    session.add(UserAnswer(**{**data, "attempt_id": answer.attempt_id}))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()
    with pytest.raises(DBAPIError, match="immutable"):
        await session.execute(text("UPDATE user_answers SET selected_display = 'B' WHERE id = :i"), {"i": answer_id})
    await session.rollback()


async def test_concurrent_users_and_double_clicks(session_maker, session):
    _test, _post, msgs = await _group_test(session_maker, session)

    async def click(tg_id: int, letter: str):
        async with session_maker() as s:
            return await group_tests.submit_group_answer(s, tg_user(tg_id, f"U{tg_id}"), msgs[0].id, letter)

    # 40 different employees at once + the same employee double-clicking A and B simultaneously.
    results = await asyncio.gather(
        *[click(6000 + i, "ABCD"[i % 4]) for i in range(40)], click(7000, "A"), click(7000, "B")
    )
    assert sum(r.code == GroupAnswerCode.ACCEPTED for r in results[:40]) == 40
    assert sorted(r.code for r in results[40:]) == sorted([GroupAnswerCode.ACCEPTED, GroupAnswerCode.DUPLICATE])
    total = (await session.execute(select(func.count(UserAnswer.id)))).scalar_one()
    assert total == 41
    per_user = (
        await session.execute(select(func.count(UserAnswer.id)).where(UserAnswer.telegram_id == 7000))
    ).scalar_one()
    assert per_user == 1
    assert (await session.execute(select(func.count(TestAttempt.id)))).scalar_one() == 41


# ------------------------------------------------------------------------------ through the real Dispatcher


_DP = None


@pytest_asyncio.fixture
async def tg(session_maker):
    global _DP
    from tests.test_handlers import _dispatcher

    session = RecordingSession()
    bot = Bot(token="123456:TEST-TOKEN-not-real", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    dp = _dispatcher()
    dp.fsm.storage.storage.clear()

    async def feed(update):
        await dp.feed_update(bot, update)
        return session

    feed.session = session
    feed.bot = bot
    yield feed


async def test_group_messages_and_callbacks_end_to_end(tg, session_maker):
    async with session_maker() as session:
        test, post, msgs = await _group_test(session_maker, session, n=3)
        test_id, post_id = test.id, post.id
    # The bot posts the header and every question with ONLY A/B/C/D buttons.
    assert await group_tests.send_post(tg.bot, session_maker, post_id)
    sent = tg.session.sent_to(GROUP_CHAT)
    assert len(sent) == 4 and "YANGI TEST BOSHLANDI" in sent[0].text
    for msg in sent[1:]:
        buttons = [b.text for row in msg.reply_markup.inline_keyboard for b in row]
        assert buttons == ["A", "B", "C", "D"]
        assert "A)" in msg.text and "D)" in msg.text  # full option texts are in the message body
    assert "1 / 3" in sent[1].text
    # Re-running is idempotent (nothing is posted twice).
    tg.session.clear()
    await group_tests.send_post(tg.bot, session_maker, post_id)
    assert tg.session.sent_to(GROUP_CHAT) == []

    gqm_id = msgs[0].id
    await tg(callback_update(8001, GroupStartCB(p=post_id).pack(), "Aliyev", chat_id=GROUP_CHAT))
    assert any("Test boshlandi" in a for a in tg.session.alerts())
    tg.session.clear()
    await tg(callback_update(8001, GroupAnsCB(m=gqm_id, o="B").pack(), "Aliyev", chat_id=GROUP_CHAT))
    assert any("qabul qilindi" in a for a in tg.session.alerts())
    tg.session.clear()
    await tg(callback_update(8001, GroupAnsCB(m=gqm_id, o="C").pack(), "Aliyev", chat_id=GROUP_CHAT))
    assert any("allaqachon javob bergansiz" in a for a in tg.session.alerts())
    # 11. A different Telegram user pressing the same button can only answer for THEMSELVES.
    tg.session.clear()
    await tg(callback_update(8002, GroupAnsCB(m=gqm_id, o="D").pack(), "Valiyev", chat_id=GROUP_CHAT))
    async with session_maker() as session:
        rows = (await session.execute(select(UserAnswer.telegram_id, UserAnswer.selected_display)
                                      .order_by(UserAnswer.telegram_id))).all()  # fmt: skip
        assert rows == [(8001, "B"), (8002, "D")]
    # Garbage / stale callback data is rejected.
    tg.session.clear()
    await tg(callback_update(8003, GroupAnsCB(m=999999, o="A").pack(), "X", chat_id=GROUP_CHAT))
    assert any("eskirgan" in a for a in tg.session.alerts())

    # Live counter refresh edits the question message.
    async with session_maker() as session:
        await session.execute(text("UPDATE group_question_messages SET rendered_at = NULL"))
        await session.commit()
    tg.session.clear()
    assert await group_tests.refresh_counters(tg.bot, session_maker) >= 1
    assert any("Javob berdi: 2" in t for t in tg.session.texts())

    # 9. Admin sees exactly who chose what.
    tg.session.clear()
    await tg(callback_update(ADMIN, AdminCB(s="tst_ans", id=test_id, p=1).pack()))
    page = "\n".join(tg.session.texts())
    assert "tanladi" in page and page.count("tanladi") == 2
    tg.session.clear()
    await tg(callback_update(ADMIN, AdminCB(s="tst_qs", id=test_id).pack()))
    stats_page = "\n".join(tg.session.texts())
    assert "1-savol" in stats_page and "/2)" in stats_page  # both answers to question 1 are counted
    tg.session.clear()
    await tg(callback_update(ADMIN, AdminCB(s="tst_part", id=test_id, v="all").pack()))
    assert any("TEST STATISTIKASI" in t for t in tg.session.texts())
    # 10. An ordinary employee cannot open admin statistics.
    tg.session.clear()
    await tg(callback_update(8001, AdminCB(s="tst_ans", id=test_id, p=1).pack(), "Aliyev"))
    assert any("faqat administratorlar" in a for a in tg.session.alerts())
    assert not tg.session.texts()

    # End of the 24h window: answering is closed and poll-like final results are shown.
    async with session_maker() as session:
        test = await session.get(Test, test_id)
        await tb.expire_due(session, test.deadline_at + timedelta(seconds=1))
    tg.session.clear()
    assert await group_tests.finalize_posts(tg.bot, session_maker) == 1
    texts = tg.session.texts()
    assert any("YAKUNIY NATIJA" in t for t in texts) and any("TEST MUDDATI TUGADI" in t for t in texts)
    tg.session.clear()
    await tg(callback_update(8004, GroupAnsCB(m=msgs[1].id, o="A").pack(), "Late", chat_id=GROUP_CHAT))
    assert any("TEST MUDDATI TUGADI" in a for a in tg.session.alerts())
    async with session_maker() as session:
        post = await session.get(GroupTestPost, post_id)
        assert post.finalized_at is not None and post.finalized_at <= utcnow()


async def test_scheduler_starts_group_test_automatically(tg, session_maker):
    """A scheduled group test is posted to its group by the scheduler at start time."""
    from app.services.scheduler import run_tick

    async with session_maker() as session:
        mid = await make_material_with_text(session_maker)
        for i in range(2):
            await make_question(session, text_=f"Rejali guruh savoli {i}?", material_id=mid)
        admin = await make_employee(session, ADMIN, "Admin Bosh")
        group, _ = await group_service.register_group(session, GROUP_CHAT, "Sud xodimlari", admin)
        now = utcnow()
        test = await make_test(session, question_count=2, group_id=group.id, delivery_mode=DeliveryMode.GROUP,
                               starts_at=now + timedelta(hours=1), deadline_at=now + timedelta(hours=25))  # fmt: skip
        await tb.assemble_questions(session_maker, test.id, None)
        await tb.mark_ready(session, test.id)
        await tb.publish(session, test.id)
        test_id = test.id
    await run_tick(tg.bot, session_maker)
    assert tg.session.sent_to(GROUP_CHAT) == []  # not yet
    async with session_maker() as session:
        test = await session.get(Test, test_id)
        test.starts_at = utcnow() - timedelta(minutes=1)
        await session.commit()
    await run_tick(tg.bot, session_maker)
    sent = tg.session.sent_to(GROUP_CHAT)
    assert len(sent) == 3 and "YANGI TEST BOSHLANDI" in sent[0].text
    await run_tick(tg.bot, session_maker)  # idempotent: nothing posted twice
    assert len(tg.session.sent_to(GROUP_CHAT)) == 3


async def test_new_member_is_told_about_running_test(tg, session_maker):
    from datetime import UTC, datetime

    from aiogram.types import Chat, Message, Update, User

    async with session_maker() as session:
        _test, post, _msgs = await _group_test(session_maker, session, n=2)
        post_id = post.id
    await group_tests.send_post(tg.bot, session_maker, post_id)
    tg.session.clear()
    newcomer = User(id=9101, is_bot=False, first_name="Yangi", last_name="Xodim")
    service = Update(
        update_id=990001,
        message=Message(
            message_id=990001, date=datetime.now(UTC), chat=Chat(id=GROUP_CHAT, type="supergroup", title="Sud"),
            from_user=newcomer, new_chat_members=[newcomer],
        ),
    )  # fmt: skip
    await tg(service)
    sent = tg.session.sent_to(GROUP_CHAT)
    assert len(sent) == 1 and "Xush kelibsiz" in sent[0].text and "faol testlar" in sent[0].text
    url = sent[0].reply_markup.inline_keyboard[0][0].url
    assert url.endswith(f"start=test_{_test.id}")
    # The same join arriving again (chat_member update) does not greet twice.
    await tg(service.model_copy(update={"update_id": 990002}))
    assert len(tg.session.sent_to(GROUP_CHAT)) == 1
    # /test in the group lists running tests with bot links.
    tg.session.clear()
    from tests.telegram_mock import message_update

    await tg(message_update(9101, "/test", "Yangi", chat_type="supergroup", chat_id=GROUP_CHAT))
    assert any("faol testlar" in t for t in tg.session.texts())


async def test_group_test_can_be_continued_in_private_chat(tg, session_maker):
    from app.keyboards.callbacks import AnsCB, EmpCB
    from app.services import settings_service

    async with session_maker() as session:
        test, post, msgs = await _group_test(session_maker, session, n=3)
        test_id, post_id, first_gqm = test.id, post.id, msgs[0].id
        await settings_service.set_value(session, "auto_approve_all", True)
        user = await make_employee(session, 9201, "Shaxsiy Davom")
    await group_tests.send_post(tg.bot, session_maker, post_id)
    # Answer question 1 in the group...
    await tg(callback_update(9201, GroupAnsCB(m=first_gqm, o="A").pack(), "Shaxsiy", chat_id=GROUP_CHAT))
    # ...then continue privately: the bot asks question 2 (the first unanswered), not question 1 again.
    tg.session.clear()
    await tg(callback_update(9201, EmpCB(a="start", id=test_id).pack(), "Shaxsiy"))
    assert any("Savol <b>2/3</b>" in t for t in tg.session.texts())
    async with session_maker() as session:
        attempt = (await session.execute(select(TestAttempt).where(TestAttempt.user_id == user.id))).scalar_one()
        attempt_id = attempt.id
        assert attempt.current_index == 1
    # Answering question 1 again via a stale private button is rejected; question 2 is accepted.
    tg.session.clear()
    await tg(callback_update(9201, AnsCB(at=attempt_id, pos=0, o="B").pack(), "Shaxsiy"))
    assert any("allaqachon" in a for a in tg.session.alerts())
    await tg(callback_update(9201, AnsCB(at=attempt_id, pos=1, o="B").pack(), "Shaxsiy"))
    await tg(callback_update(9201, AnsCB(at=attempt_id, pos=2, o="C").pack(), "Shaxsiy"))
    assert any("TEST YAKUNLANDI" in t for t in tg.session.texts())
    async with session_maker() as session:
        answers = (
            (
                await session.execute(
                    select(UserAnswer).where(UserAnswer.user_id == user.id).order_by(UserAnswer.position)
                )
            )
            .scalars()
            .all()
        )
        assert [a.position for a in answers] == [0, 1, 2]
        attempt = await session.get(TestAttempt, attempt_id)
        assert attempt.status == AttemptStatus.COMPLETED and attempt.answered_count == 3
    # And the group buttons now say the questions were already answered.
    tg.session.clear()
    await tg(callback_update(9201, GroupAnsCB(m=msgs[1].id, o="A").pack(), "Shaxsiy", chat_id=GROUP_CHAT))
    assert any("allaqachon" in a for a in tg.session.alerts())
