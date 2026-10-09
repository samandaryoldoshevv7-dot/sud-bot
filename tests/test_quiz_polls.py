"""Questions as native Telegram quiz polls (the "Quiz Bot" look) — same rules as the buttons."""

import pytest
from aiogram.methods import DeleteMessage
from sqlalchemy import func, select

from app.keyboards.callbacks import EmpCB
from app.models import AnswerReveal, AttemptStatus, QuizPoll, TestAttempt, TestQuestion, UserAnswer
from app.services.attempts import original_to_display
from app.services.settings_service import SPECS, SettingSpec
from tests.telegram_mock import callback_update, message_update, poll_answer_update, poll_id_of
from tests.test_group_flow import tg  # noqa: F401  (fixture)
from tests.test_mixed_and_timer import _two_source_test

LETTERS = "ABCDE"


@pytest.fixture(autouse=True)
def _quiz_on(monkeypatch):
    monkeypatch.setitem(SPECS, "quiz_polls", SettingSpec("quiz_polls", True, "bool"))


async def _start(tg, session_maker, user_id=7101, **kw):  # noqa: F811
    async with session_maker() as session:
        test = await _two_source_test(session_maker, session, **kw)
        test_id = test.id
    await tg(message_update(user_id, "/start", "Ali"))
    tg.session.clear()
    await tg(callback_update(user_id, EmpCB(a="start", id=test_id).pack(), "Ali"))
    async with session_maker() as session:
        attempt = (await session.execute(select(TestAttempt).where(TestAttempt.test_id == test_id))).scalar_one()
    return test_id, attempt.id


async def _correct_index(session_maker, attempt_id: int, pos: int) -> int:
    async with session_maker() as session:
        attempt = await session.get(TestAttempt, attempt_id)
        item = attempt.layout[pos]
        tq = await session.get(TestQuestion, item["tq"])
        return LETTERS.index(original_to_display(item, tq.correct_option))


async def test_questions_come_as_quiz_polls_and_answers_are_final(tg, session_maker):  # noqa: F811
    _test_id, attempt_id = await _start(tg, session_maker)
    polls = tg.session.polls()
    assert len(polls) == 1
    poll = polls[0]
    assert poll.question.startswith("[1/4] ") and poll.type == "quiz" and poll.is_anonymous is False
    assert len(poll.options) == 4 and poll.allows_multiple_answers is False
    assert poll.correct_option_ids == [await _correct_index(session_maker, attempt_id, 0)]
    assert poll.allows_revoting is False and poll.shuffle_options is False
    assert poll.explanation and poll.explanation.startswith("📚 ") and len(poll.explanation) <= 200
    assert [b.text for row in poll.reply_markup.inline_keyboard for b in row] == ["⏳ Qolgan vaqt"]
    assert not any("○ <b>A)</b>" in x for x in tg.session.texts())  # not the classic text question

    current = poll
    for pos in range(4):
        poll_id = poll_id_of(current)
        correct = await _correct_index(session_maker, attempt_id, pos)
        choice = (correct + 1) % 4 if pos == 0 else correct  # the first one wrong on purpose
        tg.session.clear()
        await tg(poll_answer_update(7101, poll_id, []))  # a retracted vote changes nothing
        assert not tg.session.requests
        await tg(poll_answer_update(7101, poll_id, [choice]))
        if pos < 3:
            current = tg.session.polls()[-1]
            assert current.question.startswith(f"[{pos + 2}/4] ")
        # Answering the same poll again (another variant) is ignored: the first answer is final.
        tg.session.clear()
        await tg(poll_answer_update(7101, poll_id, [(choice + 1) % 4]))
        assert not tg.session.polls() and not any("TEST YAKUNLANDI" in x for x in tg.session.texts())

    async with session_maker() as session:
        attempt = await session.get(TestAttempt, attempt_id)
        assert attempt.status == AttemptStatus.COMPLETED
        assert (attempt.correct_count, attempt.incorrect_count) == (3, 1)
        assert (await session.execute(select(func.count(UserAnswer.id)))).scalar_one() == 4
    # The result came right after the last answer.
    await tg(poll_answer_update(7101, "unknown-poll", [0]))  # unknown polls are ignored


async def test_last_answer_shows_the_result(tg, session_maker):  # noqa: F811
    _test_id, attempt_id = await _start(tg, session_maker, user_id=7102)
    for pos in range(4):
        poll_id = poll_id_of(tg.session.polls()[-1])
        tg.session.clear()
        await tg(poll_answer_update(7102, poll_id, [await _correct_index(session_maker, attempt_id, pos)]))
    assert any("TEST YAKUNLANDI" in x and "100" in x for x in tg.session.texts())


async def test_time_button_shows_the_personal_time_left(tg, session_maker):  # noqa: F811
    _test_id, attempt_id = await _start(tg, session_maker, user_id=7103)
    tg.session.clear()
    await tg(callback_update(7103, EmpCB(a="time", id=attempt_id).pack(), "Ali"))
    alert = tg.session.alerts()[-1]
    assert "Qolgan vaqt:" in alert and "Tugash:" in alert
    tg.session.clear()  # someone else's attempt: nothing is shown
    await tg(callback_update(7999, EmpCB(a="time", id=attempt_id).pack(), "Boshqa"))
    assert not any("Qolgan vaqt" in a for a in tg.session.alerts())


async def test_reopening_replaces_the_open_poll(tg, session_maker):  # noqa: F811
    test_id, attempt_id = await _start(tg, session_maker, user_id=7104)
    first = tg.session.polls()[-1]
    tg.session.clear()
    await tg(callback_update(7104, EmpCB(a="start", id=test_id).pack(), "Ali"))  # 🔄 continue
    assert any(isinstance(m, DeleteMessage) for m in tg.session.requests)
    second = tg.session.polls()[-1]
    assert second.question == first.question
    async with session_maker() as session:
        rows = (await session.execute(select(QuizPoll).where(QuizPoll.attempt_id == attempt_id))).scalars().all()
        assert [r.poll_id for r in rows] == [poll_id_of(second)]
    tg.session.clear()
    await tg(poll_answer_update(7104, poll_id_of(first), [0]))  # the deleted poll no longer counts
    assert not tg.session.requests


async def test_answers_revealed_later_use_a_plain_poll_without_revoting(tg, session_maker):  # noqa: F811
    _test_id, attempt_id = await _start(tg, session_maker, user_id=7105)
    async with session_maker() as session:  # switch the test to "answers at the end" and resend
        attempt = await session.get(TestAttempt, attempt_id)
        attempt.test.answer_reveal = AnswerReveal.AFTER_COMPLETION
        await session.commit()
    tg.session.clear()
    await tg(callback_update(7105, EmpCB(a="start", id=attempt.test_id).pack(), "Ali"))
    poll = tg.session.polls()[-1]
    assert poll.type == "regular" and poll.correct_option_ids is None and poll.explanation is None
    assert poll.allows_revoting is False  # the vote can't be changed
    tg.session.clear()
    await tg(poll_answer_update(7105, poll_id_of(poll), [2]))
    assert tg.session.polls()[-1].question.startswith("[2/4] ")


async def test_question_too_long_for_a_poll_is_shown_as_text(tg, session_maker):  # noqa: F811
    async with session_maker() as session:
        test = await _two_source_test(session_maker, session)
        rows = (await session.execute(select(TestQuestion).where(TestQuestion.test_id == test.id))).scalars().all()
        for tq in rows:  # an option longer than Telegram's 100 characters
            tq.options = {**tq.options, "A": "Juda uzun variant matni " * 6}
        await session.commit()
        test_id = test.id
    await tg(message_update(7106, "/start", "Ali"))
    tg.session.clear()
    await tg(callback_update(7106, EmpCB(a="start", id=test_id).pack(), "Ali"))
    assert not tg.session.polls()
    assert any("<b>[1/4]</b>" in x and "○ <b>A)</b>" in x for x in tg.session.texts())


async def test_long_explanation_is_sent_in_full_after_the_answer(tg, session_maker):  # noqa: F811
    async with session_maker() as session:
        test = await _two_source_test(session_maker, session)
        rows = (await session.execute(select(TestQuestion).where(TestQuestion.test_id == test.id))).scalars().all()
        for tq in rows:
            tq.explanation = "Bu qoida sud ishlarini ko'rib chiqish tartibini batafsil belgilaydi. " * 5
        await session.commit()
        test_id = test.id
    await tg(message_update(7107, "/start", "Ali"))
    tg.session.clear()
    await tg(callback_update(7107, EmpCB(a="start", id=test_id).pack(), "Ali"))
    poll = tg.session.polls()[-1]
    assert len(poll.explanation) <= 200 and poll.explanation.endswith("…")
    async with session_maker() as session:
        attempt_id = (await session.execute(select(TestAttempt.id).where(TestAttempt.test_id == test_id))).scalar_one()
    tg.session.clear()
    await tg(poll_answer_update(7107, poll_id_of(poll), [await _correct_index(session_maker, attempt_id, 0)]))
    full = [x for x in tg.session.texts() if "💡 <b>Izoh:</b>" in x]
    assert full and full[0].count("batafsil belgilaydi") == 5


def test_classic_mode_is_still_available(monkeypatch):
    from app.services import quiz_polls

    assert SPECS["quiz_polls"].default is True  # this module's fixture
    assert quiz_polls.QUESTION_MAX == 300 and quiz_polls.OPTION_MAX == 100 and quiz_polls.EXPLANATION_MAX == 200
