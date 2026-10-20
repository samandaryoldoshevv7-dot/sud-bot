"""Where tests run: tests for everybody / a group are taken INSIDE the Telegram group as quiz polls;
only tests for selected employees go to the bot's private chat."""

from aiogram.methods import SendPoll
from sqlalchemy import select

from app.keyboards.callbacks import AdminCB
from app.models import DeliveryMode, Test, TestAttempt, TestAudience, UserAnswer
from app.services import assignments, group_tests
from app.services import groups as group_service
from app.services.notifications import remind_unfinished
from app.services.scheduler import launch_test
from tests.factories import make_employee
from tests.telegram_mock import callback_update, message_update, poll_answer_update, poll_id_of
from tests.test_group_flow import tg  # noqa: F401  (fixture)
from tests.test_mixed_and_timer import ADMIN, GROUP_CHAT, _two_source_test


def _buttons(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row] if markup else []


async def _setup(session_maker, **kw) -> int:
    async with session_maker() as session:
        admin = await make_employee(session, ADMIN, "Admin Bosh")
        group, _ = await group_service.register_group(session, GROUP_CHAT, "Sud xodimlari", admin)
        if kw.pop("for_group", False):
            kw.update(group_id=group.id, delivery_mode=DeliveryMode.GROUP)
        test = await _two_source_test(session_maker, session, **kw)
        return test.id


def _group_polls(tg) -> list[SendPoll]:  # noqa: F811
    return [m for m in tg.session.requests if isinstance(m, SendPoll) and int(m.chat_id) == GROUP_CHAT]


async def test_admin_panel_has_no_mode_buttons(tg, session_maker):  # noqa: F811
    async with session_maker() as session:
        await make_employee(session, ADMIN, "Admin Bosh")
    await tg(message_update(ADMIN, "/admin", "Admin"))
    menu = tg.session.sent_to(ADMIN)[-1]
    labels = _buttons(menu.reply_markup)
    assert "⚙️ Sozlamalar" in labels and not any("Guruhda ishlash" in x or "Shaxsiy chatda" in x for x in labels)


async def test_group_test_runs_in_the_group_as_visible_quiz_polls(tg, session_maker):  # noqa: F811
    await tg(message_update(7801, "/start", "Ali"))  # Ali has opened the bot (gets the final result there)
    test_id = await _setup(session_maker, for_group=True)
    tg.session.clear()
    await launch_test(tg.bot, session_maker, test_id)
    sent = tg.session.sent_to(GROUP_CHAT)
    assert len(sent) == 1 and "YANGI TEST" in sent[0].text and sent[0].reply_markup is None  # short header
    polls = _group_polls(tg)
    assert [p.question[:5] for p in polls] == ["[1/4]", "[2/4]", "[3/4]", "[4/4]"]
    # Not anonymous: once a member has answered, they see who chose what; nobody can change a vote.
    assert all(p.type == "quiz" and p.is_anonymous is False and p.allows_revoting is False for p in polls)
    assert all(not p.hide_results_until_closes for p in polls)
    assert all(p.explanation for p in polls)  # 💡 on the poll itself

    # Ali and Vali answer the same poll: each answer is written to the one who chose it.
    first = polls[0]
    correct = first.correct_option_ids[0]
    tg.session.clear()
    await tg(poll_answer_update(7801, poll_id_of(first), [correct], "Ali"))
    await tg(poll_answer_update(7802, poll_id_of(first), [(correct + 1) % 4], "Vali"))
    await tg(poll_answer_update(7801, poll_id_of(first), [(correct + 2) % 4], "Ali"))  # second try: ignored
    assert tg.session.sent_to(7801) == [] and tg.session.sent_to(GROUP_CHAT) == []  # no per-answer messages
    async with session_maker() as session:
        rows = (
            await session.execute(select(UserAnswer.telegram_id, UserAnswer.is_correct).order_by(UserAnswer.id))
        ).all()
        assert rows == [(7801, True), (7802, False)]

    # Ali finishes: the final result (with 📋 Xatolarni ko'rish when wrong) comes to the bot only.
    for poll in polls[1:]:
        await tg(poll_answer_update(7801, poll_id_of(poll), [(poll.correct_option_ids[0] + 1) % 4], "Ali"))
    result = tg.session.sent_to(7801)
    assert result and "TEST YAKUNLANDI" in result[-1].text
    assert "📋 Xatolarni ko'rish" in _buttons(result[-1].reply_markup)
    assert tg.session.sent_to(GROUP_CHAT) == []
    async with session_maker() as session:
        attempts = (await session.execute(select(TestAttempt).where(TestAttempt.test_id == test_id))).scalars().all()
        assert len(attempts) == 2  # one per member


async def test_test_for_everybody_is_posted_into_the_group(tg, session_maker):  # noqa: F811
    await tg(message_update(7811, "/start", "Ali"))
    test_id = await _setup(session_maker)
    tg.session.clear()
    await launch_test(tg.bot, session_maker, test_id)
    assert len(_group_polls(tg)) == 4
    assert not any(b.url for m in tg.session.sent_to(GROUP_CHAT) for row in (m.reply_markup.inline_keyboard
                   if m.reply_markup else []) for b in row)  # fmt: skip
    dm = tg.session.sent_to(7811)
    assert dm and "Bu test guruhda ishlanadi" in dm[-1].text and dm[-1].reply_markup is None
    # The bot does not open it privately (Testlarim / an old link).
    tg.session.clear()
    await tg(message_update(7811, f"/start run_{test_id}", "Ali"))
    assert any("guruhda ishlanadi" in m.text for m in tg.session.sent_to(7811)) and not tg.session.polls()


async def test_test_for_selected_employees_stays_in_the_private_chat(tg, session_maker, monkeypatch):  # noqa: F811
    from app.services.settings_service import SPECS, SettingSpec

    monkeypatch.setitem(SPECS, "quiz_polls", SettingSpec("quiz_polls", True, "bool", visible=False))
    await tg(message_update(7821, "/start", "Ali"))
    test_id = await _setup(session_maker, audience=TestAudience.USERS)
    async with session_maker() as session:
        from app.models import User

        ali = (await session.execute(select(User).where(User.telegram_id == 7821))).scalar_one()
        await assignments.assign_users(session, test_id, [ali.id])
    tg.session.clear()
    await launch_test(tg.bot, session_maker, test_id)
    assert tg.session.sent_to(GROUP_CHAT) == [] and not _group_polls(tg)
    dm = tg.session.sent_to(7821)
    assert dm and _buttons(dm[-1].reply_markup) == ["▶️ TESTNI BOSHLASH"]
    tg.session.clear()
    await tg(callback_update(7821, dm[-1].reply_markup.inline_keyboard[0][0].callback_data, "Ali"))
    assert tg.session.polls() and tg.session.polls()[-1].question.startswith("[1/4]")


async def test_reminder_for_a_group_test_has_no_private_button(tg, session_maker):  # noqa: F811
    await tg(message_update(7831, "/start", "Ali"))
    test_id = await _setup(session_maker)
    tg.session.clear()
    async with session_maker() as session:
        await remind_unfinished(tg.bot, session, await session.get(Test, test_id))
    dm = tg.session.sent_to(7831)
    assert dm and "Bu test guruhda ishlanadi" in dm[-1].text and dm[-1].reply_markup is None


async def test_old_mode_buttons_are_harmless(tg, session_maker):  # noqa: F811
    """A menu sent before the update may still carry the old mode buttons: nothing breaks."""
    async with session_maker() as session:
        await make_employee(session, ADMIN, "Admin Bosh")
    tg.session.clear()
    await tg(callback_update(ADMIN, AdminCB(s="mode", v="group").pack(), "Admin"))
    assert not any("Kutilmagan xatolik" in x for x in tg.session.texts() + tg.session.alerts())
    assert group_tests  # module in use


async def test_group_votes_never_start_a_retake_which_runs_privately(tg, session_maker):  # noqa: F811
    """Ali finished the group test; the admin allows a retake: a vote on a group poll does not start
    it (every poll there is already voted), the retake opens in the bot's private chat."""
    from app.models import User
    from app.services import test_builder as tb

    await tg(message_update(7831, "/start", "Ali"))
    test_id = await _setup(session_maker, for_group=True)
    await launch_test(tg.bot, session_maker, test_id)
    polls = _group_polls(tg)
    assert all(p.protect_content for p in polls)  # a forwarded poll can't be answered by outsiders
    for poll in polls[:3]:
        await tg(poll_answer_update(7831, poll_id_of(poll), [poll.correct_option_ids[0]], "Ali"))
    async with session_maker() as session:
        ali = (await session.execute(select(User).where(User.telegram_id == 7831))).scalar_one()
        attempt = (await session.execute(select(TestAttempt).where(TestAttempt.user_id == ali.id))).scalar_one()
        from app.models import AttemptStatus
        from app.services.attempts import finalize_attempt

        await finalize_attempt(session, attempt, AttemptStatus.COMPLETED, tb.utcnow())
        await session.commit()
        await assignments.allow_retake(session, test_id, ali.id)
        ali_id = ali.id
    await tg(poll_answer_update(7831, poll_id_of(polls[3]), [0], "Ali"))  # the last, not yet voted poll
    async with session_maker() as session:
        attempts = (await session.execute(select(TestAttempt).where(TestAttempt.user_id == ali_id))).scalars().all()
        assert len(attempts) == 1  # no retake started by a group vote
    tg.session.clear()
    await tg(message_update(7831, f"/start run_{test_id}", "Ali"))
    sent = [m.text for m in tg.session.sent_to(7831)]
    assert sent and not any("guruhda ishlanadi" in x for x in sent), sent
    async with session_maker() as session:
        attempts = (await session.execute(select(TestAttempt).where(TestAttempt.user_id == ali_id))).scalars().all()
        assert sorted(a.attempt_no for a in attempts) == [1, 2]


async def test_vote_while_paused_is_kept_and_a_refused_vote_is_explained_once(tg, session_maker):  # noqa: F811
    from app.models import User, UserStatus
    from app.services import test_builder as tb

    await tg(message_update(7841, "/start", "Ali"))
    await tg(message_update(7842, "/start", "Vali"))
    test_id = await _setup(session_maker, for_group=True)
    await launch_test(tg.bot, session_maker, test_id)
    polls = _group_polls(tg)
    async with session_maker() as session:
        await tb.set_paused(session, test_id, True)
        vali = (await session.execute(select(User).where(User.telegram_id == 7842))).scalar_one()
        vali.status = UserStatus.BLOCKED
        await session.commit()
    # Telegram can't refuse a vote: a first vote during a pause is kept, not lost.
    await tg(poll_answer_update(7841, poll_id_of(polls[0]), [polls[0].correct_option_ids[0]], "Ali"))
    async with session_maker() as session:
        rows = (await session.execute(select(UserAnswer.telegram_id))).scalars().all()
        assert rows == [7841]
    # A vote that can't count is explained privately, once (not for every poll).
    tg.session.clear()
    for poll in polls[:3]:
        await tg(poll_answer_update(7842, poll_id_of(poll), [0], "Vali"))
    notes = [m.text for m in tg.session.sent_to(7842) if "hisobga olinmadi" in m.text]
    assert len(notes) == 1


def test_corrections_show_the_letter_of_this_layout():
    """Answered in another group's shuffle: the letter shown follows the option, not the old letter."""
    from app.handlers.formatting import corrections_text
    from app.models import TestQuestion

    tq = TestQuestion(id=1, question_text="Savol?", options={"A": "bir", "B": "ikki", "C": "uch", "D": "to'rt"},
                      correct_option="C", explanation="Izoh", source_name="Qonun")  # fmt: skip
    answer = UserAnswer(position=0, selected_option="B", selected_display="D")  # "D" in the other group
    layout = [{"tq": 1, "opts": ["C", "B", "A", "D"]}]  # here: A)=uch B)=ikki C)=bir D)=to'rt
    text = corrections_text([(answer, tq)], layout)[0]
    assert "B) ikki" in text and "A) uch" in text
