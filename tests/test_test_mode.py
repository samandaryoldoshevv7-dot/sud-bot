"""Admin's test mode: 👥 Guruhda ishlash (questions + A/B/C/D buttons in the group) or
💬 Shaxsiy chatda ishlash (the bot's private chat, as before)."""

import asyncio

from aiogram.methods import AnswerCallbackQuery
from sqlalchemy import select

from app.keyboards.callbacks import AdminCB, GroupAnsCB, GroupStartCB
from app.models import AttemptStatus, BotSetting, DeliveryMode, GroupQuestionMessage, TestAttempt, UserAnswer
from app.services import group_tests, settings_service
from app.services import groups as group_service
from tests.factories import make_employee
from tests.telegram_mock import callback_update, message_update
from tests.test_group_flow import tg  # noqa: F401  (fixture)
from tests.test_mixed_and_timer import ADMIN, GROUP_CHAT, _two_source_test


def _buttons(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


async def _set_mode(session_maker, in_group: bool) -> None:
    async with session_maker() as session:
        await settings_service.set_value(session, "tests_in_group", in_group)


async def _group_test(session_maker):
    async with session_maker() as session:
        admin = await make_employee(session, ADMIN, "Admin Bosh")
        group, _ = await group_service.register_group(session, GROUP_CHAT, "Sud xodimlari", admin)
        test = await _two_source_test(session_maker, session, group_id=group.id, delivery_mode=DeliveryMode.GROUP)
        return test.id, group.id


async def test_admin_chooses_the_mode_and_it_is_stored(tg, session_maker):  # noqa: F811
    async with session_maker() as session:
        await make_employee(session, ADMIN, "Admin Bosh")
    await tg(message_update(ADMIN, "/admin", "Admin"))
    menu = tg.session.sent_to(ADMIN)[-1]
    assert "Testlar rejimi: <b>💬 Shaxsiy chatda ishlash</b>" in menu.text  # default: as before
    assert _buttons(menu.reply_markup)[-2:] == ["👥 Guruhda ishlash", "✅ 💬 Shaxsiy chatda ishlash"]

    tg.session.clear()
    await tg(callback_update(ADMIN, AdminCB(s="mode", v="group").pack(), "Admin"))
    assert any("Rejim: 👥 Guruhda ishlash" in a for a in tg.session.alerts())
    edited = tg.session.texts()[-1]
    assert "Testlar rejimi: <b>👥 Guruhda ishlash</b>" in edited
    assert _buttons(tg.session.last_markup())[-2:] == ["✅ 👥 Guruhda ishlash", "💬 Shaxsiy chatda ishlash"]
    async with session_maker() as session:  # stored in the database: survives a restart
        assert (await session.get(BotSetting, "tests_in_group")).value is True

    tg.session.clear()
    await tg(callback_update(ADMIN, AdminCB(s="mode", v="private").pack(), "Admin"))
    assert any("Rejim: 💬 Shaxsiy chatda ishlash" in a for a in tg.session.alerts())
    async with session_maker() as session:
        assert await settings_service.get_value(session, "tests_in_group") is False
    # ⚙️ Sozlamalar does not list it a second time.
    tg.session.clear()
    await tg(callback_update(ADMIN, AdminCB(s="set").pack(), "Admin"))
    assert not any("tests_in_group" in x for x in _buttons(tg.session.last_markup()))


async def test_group_mode_runs_the_test_inside_the_group(tg, session_maker):  # noqa: F811
    await _set_mode(session_maker, True)
    test_id, _group_id = await _group_test(session_maker)
    tg.session.clear()
    post_id, ok = await group_tests.start_in_group(tg.bot, session_maker, test_id)
    assert ok
    sent = tg.session.sent_to(GROUP_CHAT)
    # Header + every question with A/B/C/D buttons, all in the group.
    assert len(sent) == 1 + 4
    assert "Test shu guruhda ishlanadi" in sent[0].text and _buttons(sent[0].reply_markup) == ["▶️ TESTNI BOSHLASH"]
    assert all(_buttons(m.reply_markup) == ["A", "B", "C", "D"] for m in sent[1:])
    assert not tg.session.polls()

    # ▶️ TESTNI BOSHLASH: the test starts right here — no link to the private chat.
    tg.session.clear()
    await tg(callback_update(7501, GroupStartCB(p=post_id).pack(), "Ali", chat_id=GROUP_CHAT))
    calls = [m for m in tg.session.requests if isinstance(m, AnswerCallbackQuery)]
    assert calls and calls[0].url is None and "Test boshlandi" in (calls[0].text or "")
    assert not [m for m in tg.session.requests if getattr(m, "chat_id", None) == 7501]

    async with session_maker() as session:
        messages = (
            await session.execute(
                select(GroupQuestionMessage).where(GroupQuestionMessage.post_id == post_id)
                .order_by(GroupQuestionMessage.position)
            )
        ).scalars().all()  # fmt: skip
        gqm_ids = [m.id for m in messages]
        corrects = [await _correct(session, m) for m in messages]

    # Ali and Vali answer the same question at the same moment: each answer is written to its own author.
    tg.session.clear()
    wrong = next(x for x in "ABCD" if x != corrects[0])
    await asyncio.gather(
        tg(callback_update(7501, GroupAnsCB(m=gqm_ids[0], o=corrects[0]).pack(), "Ali", chat_id=GROUP_CHAT)),
        tg(callback_update(7502, GroupAnsCB(m=gqm_ids[0], o=wrong).pack(), "Vali", chat_id=GROUP_CHAT)),
    )
    alerts = tg.session.alerts()
    assert any("TO'G'RI JAVOB" in a for a in alerts) and any("NOTO'G'RI JAVOB" in a for a in alerts)
    tg.session.clear()  # a second click on the same question changes nothing
    await tg(callback_update(7501, GroupAnsCB(m=gqm_ids[0], o=wrong).pack(), "Ali", chat_id=GROUP_CHAT))
    assert any("allaqachon javob bergansiz" in a for a in tg.session.alerts())

    # Ali finishes the whole test in the group.
    tg.session.clear()
    for gqm_id, correct in zip(gqm_ids[1:], corrects[1:], strict=True):
        await tg(callback_update(7501, GroupAnsCB(m=gqm_id, o=correct).pack(), "Ali", chat_id=GROUP_CHAT))
    assert any("Test yakunlandi: 4/4" in a for a in tg.session.alerts())
    async with session_maker() as session:
        rows = (
            await session.execute(
                select(UserAnswer.telegram_id, UserAnswer.is_correct).order_by(UserAnswer.telegram_id, UserAnswer.id)
            )
        ).all()
        assert [r for r in rows if r[0] == 7501] == [(7501, True)] * 4
        assert [r for r in rows if r[0] == 7502] == [(7502, False)]
        statuses = dict(
            (await session.execute(select(TestAttempt.user_id, TestAttempt.status))).all()  # one attempt each
        )
        assert sorted(s.value for s in statuses.values()) == sorted(
            [AttemptStatus.COMPLETED.value, AttemptStatus.IN_PROGRESS.value]
        )

    # A member's /start in the group is not sent to the private chat either.
    tg.session.clear()
    await tg(message_update(7503, "/start", "Soli", chat_type="supergroup", chat_id=GROUP_CHAT))
    reply = tg.session.sent_to(GROUP_CHAT)[-1]
    assert "A/B/C/D tugmalari orqali javob bering" in reply.text and reply.reply_markup is None


async def test_private_mode_keeps_the_private_chat(tg, session_maker):  # noqa: F811
    await _set_mode(session_maker, False)
    test_id, _group_id = await _group_test(session_maker)
    tg.session.clear()
    post_id, ok = await group_tests.start_in_group(tg.bot, session_maker, test_id)
    assert ok
    sent = tg.session.sent_to(GROUP_CHAT)
    assert len(sent) == 1 and "bot bilan shaxsiy chatda ochiladi" in sent[0].text  # header only, as before
    tg.session.clear()
    await tg(callback_update(7601, GroupStartCB(p=post_id).pack(), "Ali", chat_id=GROUP_CHAT))
    urls = [m.url for m in tg.session.requests if isinstance(m, AnswerCallbackQuery)]
    assert urls and urls[0].endswith(f"start=run_{test_id}")
    assert tg.session.sent_to(GROUP_CHAT) == []
    # The deep link opens the test privately, exactly as before.
    await tg(message_update(7601, f"/start run_{test_id}", "Ali"))
    assert any("<b>[1/4]</b>" in m.text for m in tg.session.sent_to(7601))


async def test_switching_to_group_mode_posts_the_questions_of_a_running_test(tg, session_maker):  # noqa: F811
    """A test already posted in private mode (header only): after the admin switches to the group
    mode, ▶️ TESTNI BOSHLASH puts its questions into the group once."""
    from tests.test_quiz_polls import _wait_background

    await _set_mode(session_maker, False)
    test_id, _group_id = await _group_test(session_maker)
    post_id, _ = await group_tests.start_in_group(tg.bot, session_maker, test_id)
    await _set_mode(session_maker, True)
    tg.session.clear()
    await tg(callback_update(7701, GroupStartCB(p=post_id).pack(), "Ali", chat_id=GROUP_CHAT))
    await tg(callback_update(7702, GroupStartCB(p=post_id).pack(), "Vali", chat_id=GROUP_CHAT))
    await _wait_background()
    questions = [m for m in tg.session.sent_to(GROUP_CHAT) if m.reply_markup is not None]
    assert len(questions) == 4 and all(_buttons(m.reply_markup) == ["A", "B", "C", "D"] for m in questions)


async def _correct(session, gqm) -> str:
    from app.models import TestQuestion

    tq = await session.get(TestQuestion, gqm.test_question_id)
    return "ABCD"[gqm.opts.index(tq.correct_option)]
