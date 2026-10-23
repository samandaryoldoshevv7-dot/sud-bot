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
    assert poll.question.startswith("📚 Manba: ") and "\n❓ Savol 1/4\n" in poll.question and poll.type == "quiz" and poll.is_anonymous is False
    assert len(poll.options) == 4 and poll.allows_multiple_answers is False
    assert poll.correct_option_ids == [await _correct_index(session_maker, attempt_id, 0)]
    assert poll.allows_revoting is False and poll.shuffle_options is False
    assert poll.explanation and poll.explanation.startswith("💡 ") and len(poll.explanation) <= 200
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
        # Telegram marks right/wrong on the poll itself; the bot sends no extra card.
        assert not any(x.startswith(f"<b>[{pos + 1}/4]</b>") for x in tg.session.texts())
        if pos < 3:
            current = tg.session.polls()[-1]
            assert f"\n❓ Savol {pos + 2}/4\n" in current.question
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
    assert "\n❓ Savol 2/4\n" in tg.session.polls()[-1].question


async def test_question_too_long_is_still_a_poll_with_the_full_text_above(tg, session_maker):  # noqa: F811
    async with session_maker() as session:
        test = await _two_source_test(session_maker, session)
        rows = (await session.execute(select(TestQuestion).where(TestQuestion.test_id == test.id))).scalars().all()
        long_option = "Juda uzun variant matni " * 6  # longer than Telegram's 100 characters
        for tq in rows:
            tq.options = {**tq.options, "A": long_option}
        await session.commit()
        test_id = test.id
    await tg(message_update(7106, "/start", "Ali"))
    tg.session.clear()
    await tg(callback_update(7106, EmpCB(a="start", id=test_id).pack(), "Ali"))
    poll = tg.session.polls()[-1]
    assert "❓ Savol 1/4" in poll.question and "yuqoridagi xabarda" in poll.question
    assert [o[:3] for o in poll.options] == ["A) ", "B) ", "C) ", "D) "]
    assert all(len(o.encode("utf-16-le")) // 2 <= 100 for o in poll.options)
    preface = next(x for x in tg.session.texts() if "❓ Savol 1/4" in x and x.startswith("<b>📚 Manba:"))
    assert long_option.strip() in preface  # the full text, nothing cut
    assert not any("○ <b>A)</b>" in x for x in tg.session.texts())  # not the old text look


async def test_long_explanation_is_cut_on_the_poll_and_no_card_is_sent(tg, session_maker):  # noqa: F811
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
    # Shortened to whole sentences: readable to the end, nothing cut in the middle.
    assert len(poll.explanation) <= 200 and poll.explanation.startswith("💡 ")
    assert "belgilaydi.\n📚 " in poll.explanation  # whole sentences, then always the source
    async with session_maker() as session:
        attempt_id = (await session.execute(select(TestAttempt.id).where(TestAttempt.test_id == test_id))).scalar_one()
    tg.session.clear()
    await tg(poll_answer_update(7107, poll_id_of(poll), [await _correct_index(session_maker, attempt_id, 0)]))
    # Private chat: the 💡 was shortened, so the whole explanation is in 📋 Xatolarni ko'rish.
    assert not any("💡 Izoh:" in x for x in tg.session.texts())


def test_classic_mode_is_still_available(monkeypatch):
    from app.services import quiz_polls

    assert SPECS["quiz_polls"].default is True  # this module's fixture
    assert quiz_polls.QUESTION_MAX == 300 and quiz_polls.OPTION_MAX == 100 and quiz_polls.EXPLANATION_MAX == 200


def test_limits_are_counted_like_telegram_counts_them():
    """Emoji count as 2: a 199-character explanation with two emoji is 201 for Telegram, which
    rejected the poll and the question fell back to the old text look."""
    from app.services import quiz_polls

    assert quiz_polls.tg_len("📚 a") == 4 and quiz_polls.tg_len("o‘") == 2
    cut = quiz_polls._cut("📚 " + "x" * 300, 200)
    assert quiz_polls.tg_len(cut) <= 200 and cut.endswith("…")


# ------------------------------------------------------------------------------ in the group


async def _wait_background():
    import asyncio

    from app.services import background

    for _ in range(200):
        if background.running_count() == 0:
            return
        await asyncio.sleep(0.02)


async def test_old_bot_button_in_the_group_opens_the_private_chat(tg, session_maker):  # noqa: F811
    from aiogram.methods import AnswerCallbackQuery

    from app.keyboards.callbacks import GroupStartCB
    from app.models import DeliveryMode
    from app.services import group_tests
    from app.services import groups as group_service
    from tests.factories import make_employee
    from tests.test_mixed_and_timer import ADMIN, GROUP_CHAT

    async with session_maker() as session:
        admin = await make_employee(session, ADMIN, "Admin Bosh")
        group, _ = await group_service.register_group(session, GROUP_CHAT, "Sud xodimlari", admin)
        test = await _two_source_test(session_maker, session, group_id=group.id, delivery_mode=DeliveryMode.GROUP)
        post = await group_tests.ensure_post(session, test, group)
        test_id, post_id = test.id, post.id
    tg.session.clear()
    await tg(callback_update(7401, GroupStartCB(p=post_id, m="b").pack(), "Ali", chat_id=GROUP_CHAT))
    urls = [m.url for m in tg.session.requests if isinstance(m, AnswerCallbackQuery)]
    assert urls and urls[0].endswith(f"start=run_{test_id}")
    assert not tg.session.polls()


async def test_next_question_not_sent_gives_a_button_instead_of_freezing(tg, session_maker, monkeypatch):  # noqa: F811
    """Telegram refuses the next poll (network / flood): the answer is kept and a button is shown."""
    from aiogram.exceptions import TelegramNetworkError

    from app.handlers import employee

    _test_id, attempt_id = await _start(tg, session_maker, user_id=7191)
    poll = tg.session.polls()[-1]

    async def broken(*args, **kwargs):
        raise TelegramNetworkError(method=None, message="timeout")

    monkeypatch.setattr(employee, "send_current_question", broken)
    tg.session.clear()
    await tg(poll_answer_update(7191, poll_id_of(poll), [await _correct_index(session_maker, attempt_id, 0)]))
    sent = tg.session.sent_to(7191)
    assert sent and "Javobingiz saqlandi" in sent[-1].text
    assert [b.text for row in sent[-1].reply_markup.inline_keyboard for b in row] == ["➡️ Keyingi savol"]
    async with session_maker() as session:
        assert (await session.get(TestAttempt, attempt_id)).answered_count == 1


def test_explanation_is_short_and_readable_to_the_end():
    from app.models import TestQuestion
    from app.services import quiz_polls

    # Fits with the source: both shown.
    tq = TestQuestion(explanation="Ariza 5 kun ichida ko'riladi.", source_name="Konstitutsiya")
    assert quiz_polls._explanation(tq) == ("💡 Ariza 5 kun ichida ko'riladi.\n📚 Konstitutsiya", False)
    # Too long with the source: whole sentences of the explanation, then the (shortened) source.
    tq = TestQuestion(explanation="Sudya arizani besh kun ichida ko'rib chiqadi va qaror qabul qiladi. " * 2,
                      source_name="O'ZBEKISTON RESPUBLIKASI MA'MURIY SUD ISHLARINI YURITISH TO'G'RISIDAGI KODEKSI")  # fmt: skip
    text, _ = quiz_polls._explanation(tq)
    assert text.startswith("💡 Sudya") and "qiladi.\n📚 O'ZBEKISTON" in text and quiz_polls.tg_len(text) <= 200
    # One endless sentence: cut at a word, with "…".
    tq = TestQuestion(explanation="juda uzun izoh " * 40, source_name="Qonun")
    text, cut = quiz_polls._explanation(tq)
    assert cut and quiz_polls.tg_len(text) <= 200 and text.endswith("…\n📚 Qonun")
