"""Personal feedback privacy, rankings, common deadline and durable notifications."""

from datetime import timedelta

from aiogram.methods import SendMessage
from sqlalchemy import select

from app.keyboards.callbacks import GroupExplanationCB, GroupResultCB
from app.models import AnswerReveal, AttemptStatus, TestAttempt, TestQuestion
from app.services import group_results, group_tests
from app.services import test_builder as tb
from app.services.scheduler import run_tick
from app.utils.time import utcnow
from tests.factories import make_employee, tg_user
from tests.telegram_mock import callback_update
from tests.test_group_flow import GROUP_CHAT, _correct_display, _group_test, tg  # noqa: F401


async def test_result_is_only_the_clickers_and_never_public(tg, session_maker, session):  # noqa: F811
    test, post, msgs = await _group_test(session_maker, session, n=1, reveal="immediate")
    correct = await _correct_display(session, msgs[0])
    wrong = next(c for c in "ABCD" if c != correct)
    await group_tests.submit_group_answer(session, tg_user(9101), msgs[0].id, correct)
    await group_tests.submit_group_answer(session, tg_user(9102), msgs[0].id, wrong)
    for uid, expected in [(9101, "To'g'ri: 1"), (9102, "To'g'ri: 0")]:
        tg.session.clear()
        await tg(callback_update(uid, GroupResultCB(p=post.id).pack(), chat_id=GROUP_CHAT))
        # A popup only the clicker sees; nothing is posted into the group.
        assert not [r for r in tg.session.requests if isinstance(r, SendMessage)]
        assert f"{expected}," in tg.session.alerts()[-1]
    # Forged callback from another group is rejected without leaking any result.
    tg.session.clear()
    await tg(callback_update(9101, GroupResultCB(p=post.id).pack(), chat_id=GROUP_CHAT - 1))
    assert not [r for r in tg.session.requests if isinstance(r, SendMessage)]
    await session.refresh(test)
    assert test.deadline_at == test.starts_at + timedelta(seconds=test.duration_seconds)


async def test_full_reason_and_shuffled_letters_with_reveal_guards(tg, session_maker, session):  # noqa: F811
    test, _post, msgs = await _group_test(session_maker, session, n=2, reveal="immediate")
    gqm = msgs[0]
    tq = await session.get(TestQuestion, gqm.test_question_id)
    tq.explanation = "Aniq sabab. " * 80
    await session.commit()
    correct = await _correct_display(session, gqm)
    wrong = next(c for c in "ABCD" if c != correct)
    await group_tests.submit_group_answer(session, tg_user(9201), gqm.id, wrong)
    tg.session.clear()
    await make_employee(session, 9201, "Izoh Oluvchi")
    await tg(callback_update(9201, GroupExplanationCB(m=gqm.id).pack(), chat_id=GROUP_CHAT))
    sent = [r for r in tg.session.requests if isinstance(r, SendMessage)]
    assert sent and all(int(m.chat_id) == 9201 for m in sent)  # old button: private chat only
    content = "\n".join(m.text for m in sent)
    assert f"To'g'ri javob: {correct})" in content and tq.explanation.strip() in content
    test.answer_reveal = AnswerReveal.AFTER_COMPLETION
    await session.commit()
    assert "keyin ochiladi" in await group_results.own_explanation(session, gqm, 9201)
    test.answer_reveal = AnswerReveal.NEVER
    await session.commit()
    assert "yashirilgan" in await group_results.own_explanation(session, gqm, 9201)
    assert "boshlamagansiz" not in await group_results.own_explanation(session, gqm, 9999)


async def test_group_expiry_results_retried_once_and_ranked(tg, session_maker, session):  # noqa: F811
    test, post, msgs = await _group_test(session_maker, session, n=2, reveal="immediate")
    for uid, name in [(9301, "Birinchi"), (9302, "Ikkinchi"), (9303, "Teng")]:
        await make_employee(session, uid, name)
        for index, gqm in enumerate(msgs):
            if uid != 9301 and index == 1:
                break
            correct = await _correct_display(session, gqm)
            await group_tests.submit_group_answer(session, tg_user(uid, name), gqm.id, correct)
    test.announced_at = test.reminder_sent_at = utcnow()
    test.starts_at = utcnow() - timedelta(minutes=2)
    test.deadline_at = utcnow() - timedelta(seconds=1)
    await session.commit()
    tg.session.clear()
    await run_tick(tg.bot, session_maker)
    pages = [m for m in tg.session.sent_to(GROUP_CHAT) if "Yakuniy reyting" in m.text]
    assert len(pages) == 1
    assert pages[0].text.index("Birinchi") < pages[0].text.index("Ikkinchi")
    assert "2. Ikkinchi" in pages[0].text and "2. Teng" in pages[0].text
    assert pages[0].reply_markup.inline_keyboard[0][0].callback_data == GroupResultCB(p=post.id).pack()
    for uid in [9302, 9303]:
        assert any("TEST VAQTI TUGADI" in m.text for m in tg.session.sent_to(uid))
    await session.refresh(post)
    assert post.finalized_at and post.result_message_ids
    tg.session.clear()
    await run_tick(tg.bot, session_maker)
    assert not tg.session.sent_to(GROUP_CHAT) and not tg.session.sent_to(9302)


async def test_failed_ranking_delivery_is_retried(tg, session_maker, session):  # noqa: F811
    test, post, _ = await _group_test(session_maker, session)
    await tb.close_test(session, test.id)
    tg.session.forbidden_chats.add(GROUP_CHAT)
    assert await group_tests.finalize_posts(tg.bot, session_maker) == 0
    await session.refresh(post)
    assert post.finalized_at is None
    tg.session.forbidden_chats.clear()
    assert await group_tests.finalize_posts(tg.bot, session_maker) == 1
    assert await group_tests.finalize_posts(tg.bot, session_maker) == 0


async def test_personal_result_expires_even_before_scheduler(session_maker, session):
    test, _post, msgs = await _group_test(session_maker, session)
    result = await group_tests.submit_group_answer(session, tg_user(9401), msgs[0].id, "A")
    result.attempt.deadline_at = utcnow() - timedelta(seconds=1)
    await session.commit()
    attempt = await group_results.own_attempt(session, test.id, 9401)
    assert attempt.status == AttemptStatus.EXPIRED
    assert "TEST VAQTI TUGADI" in group_results.personal_result(test, attempt)
    assert (
        await session.execute(select(TestAttempt).where(TestAttempt.id == attempt.id))
    ).scalar_one().expiry_notified_at is None
