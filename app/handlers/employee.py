"""Employee flows: 📚 Testlarim, taking a test (quiz style), results and corrections.

Everything is driven by the database (attempt row + answers), never by process memory, so a restart
or the employee leaving Telegram changes nothing: the personal deadline keeps running on the server
and the employee continues from the first unanswered question.
"""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, PollAnswer
from sqlalchemy.ext.asyncio import AsyncSession

from app.handlers.formatting import (
    answer_result_text,
    corrections_text,
    employee_test_card,
    poll_verdict_text,
    question_text,
    result_text,
)
from app.handlers.test_listing import available_tests_view
from app.keyboards.callbacks import AnsCB, EmpCB
from app.keyboards.common import kb
from app.keyboards.employee import answer_kb
from app.locales import t
from app.models import AnswerReveal, AttemptStatus, Test, TestAttempt, TestStatus, User, UserStatus
from app.schemas.ai import LETTERS
from app.services import attempts as attempt_service
from app.services import quiz_polls, settings_service
from app.services.attempts import AnswerOutcome, MyTestState, StartError
from app.services.test_builder import source_names
from app.statistics.sources import attempt_source_breakdown
from app.utils.text import esc, pct, split_message, truncate
from app.utils.time import fmt_dt, fmt_span, utcnow

logger = logging.getLogger(__name__)
router = Router(name="employee")
router.message.filter(F.chat.type == "private")

START_ERRORS = {
    StartError.NOT_FOUND: "emp.start_error.not_found",
    StartError.NOT_ACTIVE: "emp.start_error.not_active",
    StartError.NOT_STARTED_YET: "emp.start_error.not_started_yet",
    StartError.DEADLINE_PASSED: "emp.start_error.deadline_passed",
    StartError.USER_NOT_ACTIVE: "emp.start_error.user_not_active",
    StartError.NOT_IN_GROUP: "emp.start_error.not_in_group",
    StartError.ALREADY_COMPLETED: "emp.start_error.already_completed",
    StartError.NO_QUESTIONS: "emp.start_error.no_questions",
    StartError.PAUSED: "emp.start_error.paused",
    StartError.NOT_ASSIGNED: "emp.start_error.not_assigned",
}
MY_TESTS_TEXTS = {t("emp.btn.my_tests"), t("emp.btn.my_tests_old")}


def _can_use(user: User | None, is_admin: bool) -> bool:
    return user is not None and (is_admin or user.status == UserStatus.ACTIVE)


async def _find_my_test(session: AsyncSession, user: User, test_id: int) -> attempt_service.MyTest | None:
    return next((m for m in await attempt_service.my_tests(session, user, limit=100) if m.test.id == test_id), None)


def _card_keyboard(item: attempt_service.MyTest | None) -> InlineKeyboardMarkup | None:
    if item is None:
        return None
    rows = []
    if item.state == MyTestState.IN_PROGRESS and item.can_start:
        rows.append([(t("emp.btn.continue_test"), EmpCB(a="start", id=item.test.id))])
    elif item.state == MyTestState.NOT_STARTED and item.can_start:
        rows.append([(t("emp.btn.start_test"), EmpCB(a="start", id=item.test.id))])
    elif item.can_start:
        rows.append([(t("emp.btn.retake"), EmpCB(a="start", id=item.test.id))])
    if item.attempt is not None and item.attempt.status != AttemptStatus.IN_PROGRESS:
        rows.append([(t("emp.btn.view_result"), EmpCB(a="res", id=item.attempt.id))])
    return kb(*rows) if rows else None


async def send_test_card(message: Message, session: AsyncSession, user: User, test_id: int) -> None:
    test = await session.get(Test, test_id)
    if test is None or test.status == TestStatus.DRAFT:
        await message.answer(t("emp.start_error.not_found"))
        return
    item = await _find_my_test(session, user, test_id)
    if item is None:
        if test.status == TestStatus.READY:
            await message.answer(t("emp.start_error.not_started_yet"))
        elif test.status != TestStatus.ACTIVE:
            await message.answer(t("emp.start_error.deadline_passed"))
        else:
            await message.answer(t("emp.start_error.not_assigned"))
        return
    await message.answer(
        employee_test_card(test, item.attempt, await source_names(session, test.id)),
        reply_markup=_card_keyboard(item),
    )


@router.message(F.text.in_(MY_TESTS_TEXTS))
async def my_tests(message: Message, session: AsyncSession, bot: Bot, user: User | None, is_admin: bool) -> None:
    if not _can_use(user, is_admin):
        await message.answer(
            t("register.pending") if user and user.status == UserStatus.PENDING else t("start.inactive")
        )
        return
    assert user is not None
    view = await available_tests_view(bot, session, user)
    if view is None:
        await message.answer(t("emp.no_tests"))
        return
    await message.answer(view[0], reply_markup=view[1])


@router.message(F.text == t("emp.btn.my_results"))
async def my_results(message: Message, session: AsyncSession, user: User | None, is_admin: bool) -> None:
    if not _can_use(user, is_admin):
        await message.answer(t("start.inactive"))
        return
    assert user is not None
    history = await attempt_service.user_attempt_history(session, user.id, limit=15)
    if not history:
        await message.answer(t("emp.no_results"))
        return
    lines = [t("emp.results.title"), ""]
    rows = []
    for attempt in history:
        status = t(f"attempt_status.{attempt.status.value}")
        lines.append(
            f"• <b>{esc(attempt.test.title)}</b> — {pct(attempt.score_percent)} "
            f"({attempt.correct_count}/{attempt.total_questions}), {status}, {fmt_dt(attempt.started_at)}"
        )
        rows.append(
            [(f"{truncate(attempt.test.title, 35)} — {pct(attempt.score_percent)}", EmpCB(a="res", id=attempt.id))]
        )
    await message.answer("\n".join(lines), reply_markup=kb(*rows[:10]))


@router.callback_query(EmpCB.filter(F.a == "card"))
async def cb_card(
    callback: CallbackQuery, callback_data: EmpCB, session: AsyncSession, user: User | None, is_admin: bool
) -> None:
    if not _can_use(user, is_admin) or not isinstance(callback.message, Message):
        await callback.answer(t("start.inactive"), show_alert=True)
        return
    assert user is not None
    await callback.answer()
    await send_test_card(callback.message, session, user, callback_data.id)


# ------------------------------------------------------------------------------ taking a test


async def _question_view(session: AsyncSession, attempt: TestAttempt) -> tuple[str, InlineKeyboardMarkup] | None:
    current = await attempt_service.current_question(session, attempt)
    if current is None:
        return None
    tq, item = current
    position = attempt.current_index
    return question_text(attempt, tq, item, position), answer_kb(attempt.id, position, tq, item)


async def _send_poll(bot: Bot, chat_id: int, session: AsyncSession, attempt: TestAttempt) -> bool:
    """The current question as a native Telegram quiz poll. False: show it the classic way."""
    if not await settings_service.get_value(session, "quiz_polls"):
        return False
    current = await attempt_service.current_question(session, attempt)
    if current is None:
        return False
    tq, item = current
    position = attempt.current_index
    test = await session.get(Test, attempt.test_id)
    spec = quiz_polls.build(attempt, tq, item, position, test.answer_reveal if test else AnswerReveal.IMMEDIATE)
    if spec is None:
        return False
    for old in await quiz_polls.take_open(session, attempt.id, position):  # resumed: one live poll only
        try:
            await bot.delete_message(old.chat_id, old.message_id)
        except TelegramBadRequest:
            pass
    try:
        sent = await bot.send_poll(
            chat_id,
            **quiz_polls.send_kwargs(spec),
            reply_markup=kb([(t("quiz.btn.time"), EmpCB(a="time", id=attempt.id))]),
        )
    except TelegramBadRequest as exc:
        logger.warning("Quiz poll rejected; showing the question as text", extra={"error": str(exc)[:200]})
        return False
    if sent.poll is None:
        return False
    await quiz_polls.remember(session, sent.poll.id, attempt.id, position, chat_id, sent.message_id,
                              spec.explanation_cut)  # fmt: skip
    await attempt_service.set_message_ref(session, attempt.id, chat_id, sent.message_id)
    return True


async def send_current_question(bot: Bot, chat_id: int, session: AsyncSession, attempt: TestAttempt) -> None:
    if await _send_poll(bot, chat_id, session, attempt):
        return
    view = await _question_view(session, attempt)
    if view is None:
        return
    sent = await bot.send_message(chat_id, view[0], reply_markup=view[1])
    await attempt_service.set_message_ref(session, attempt.id, chat_id, sent.message_id)


async def _final_result(
    session: AsyncSession, test: Test, attempt: TestAttempt
) -> tuple[str, InlineKeyboardMarkup | None]:
    user = await session.get(User, attempt.user_id)
    text = result_text(test, attempt, user, await attempt_source_breakdown(session, attempt.id))
    markup = None
    if test.answer_reveal != AnswerReveal.NEVER and attempt.incorrect_count:
        markup = kb([(t("emp.btn.corrections"), EmpCB(a="corr", id=attempt.id))])
    return text, markup


async def open_test_in_private(
    bot: Bot, chat_id: int, session: AsyncSession, user: User, test_id: int
) -> tuple[StartError | None, bool]:
    """Start (or resume) the employee's own attempt and show the current question here.

    Returns ``(error, resumed)``. Used by ▶️ TESTNI BOSHLASH in the private chat and by the deep link
    the group button opens."""
    result = await attempt_service.start_attempt(session, user, test_id, chat_id=chat_id)
    if result.error is not None:
        return result.error, False
    attempt = result.attempt
    assert attempt is not None
    if attempt.chat_id and attempt.last_message_id:
        # Only the freshly sent question message is answerable.
        try:
            await bot.edit_message_reply_markup(chat_id=attempt.chat_id, message_id=attempt.last_message_id)
        except TelegramBadRequest:
            pass
    if not result.resumed or (attempt.answered_count == 0 and attempt.last_message_id is None):
        test = await session.get(Test, attempt.test_id)
        await bot.send_message(
            chat_id,
            t("emp.test_started", title=esc(test.title if test else ""), n=attempt.total_questions,
              start=fmt_dt(attempt.started_at), end=fmt_dt(attempt.deadline_at)),
        )  # fmt: skip
    await send_current_question(bot, chat_id, session, attempt)
    return None, result.resumed


@router.callback_query(EmpCB.filter(F.a == "start"))
async def cb_start(
    callback: CallbackQuery, callback_data: EmpCB, session: AsyncSession, bot: Bot, user: User | None, is_admin: bool
) -> None:
    if user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    if callback.message.chat.type != "private":
        await callback.answer(t("emp.private_only"), show_alert=True)
        return
    if not _can_use(user, is_admin):
        await callback.answer(t("emp.start_error.user_not_active"), show_alert=True)
        return
    test = await session.get(Test, callback_data.id)
    if test is not None and test.group_id and not await attempt_service.user_in_test_audience(session, test, user):
        from app.handlers.common import check_group_membership

        await check_group_membership(bot, session, user)
    error, resumed = await open_test_in_private(bot, callback.message.chat.id, session, user, callback_data.id)
    if error is not None:
        await callback.answer(t(START_ERRORS[error]), show_alert=True)
        return
    await callback.answer(t("emp.resumed") if resumed else t("emp.started"))


@router.callback_query(AnsCB.filter())
async def cb_answer(callback: CallbackQuery, callback_data: AnsCB, session: AsyncSession, user: User | None) -> None:
    """A variant was pressed: identify the employee (callback.from_user via the user middleware), find
    THEIR attempt, store the answer, check it and show the verdict right away."""
    if user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    result = await attempt_service.submit_answer(session, user, callback_data.at, callback_data.pos, callback_data.o)
    message = callback.message

    if result.outcome == AnswerOutcome.DUPLICATE:
        await callback.answer(t("gt.alert.duplicate"), show_alert=True)
        return
    if result.outcome == AnswerOutcome.INVALID:
        await callback.answer(t("errors.stale_button"), show_alert=True)
        return
    if result.outcome == AnswerOutcome.PAUSED:
        await callback.answer(t("quiz.paused"), show_alert=True)
        return
    if result.outcome == AnswerOutcome.NOT_IN_PROGRESS:
        finished = result.attempt is not None and result.attempt.status == AttemptStatus.COMPLETED
        # Every question of a completed attempt is answered: the answer is final.
        await callback.answer(t("gt.alert.duplicate") if finished else t("emp.answer.not_in_progress"), show_alert=True)
        try:
            await message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        return
    attempt = result.attempt
    assert attempt is not None
    test = await session.get(Test, attempt.test_id)
    assert test is not None
    if result.outcome == AnswerOutcome.EXPIRED:
        await callback.answer(t("emp.answer.expired"), show_alert=True)
        text, markup = await _final_result(session, test, attempt)
        try:
            await message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await message.answer(text, reply_markup=markup)
        return

    await callback.answer()
    item = attempt.layout[callback_data.pos]
    assert result.question is not None
    text = answer_result_text(
        result.question, list(item["opts"]), callback_data.pos, attempt.total_questions,
        result.selected_display or callback_data.o, test.answer_reveal,
    )  # fmt: skip
    markup = kb([(t("quiz.btn.close"), EmpCB(a="next", id=attempt.id))])
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        logger.debug("Could not edit answered question", extra={"error": str(exc)[:100]})
        await message.answer(text, reply_markup=markup)


@router.poll_answer()
async def on_poll_answer(poll_answer: PollAnswer, session: AsyncSession, bot: Bot, user: User | None) -> None:
    """A variant was chosen in a quiz poll: same rules as a button press (final, per-user, checked
    on the server). Telegram shows right/wrong at once; a lasting card with the correct answer, source and
    explanation follows, then the next question."""
    if user is None or not poll_answer.option_ids:
        return  # an answer can't be taken back: a retracted vote changes nothing
    if poll_answer.option_ids[0] >= len(LETTERS):
        return
    poll = await quiz_polls.find(session, poll_answer.poll_id)
    if poll is None:
        from app.handlers.group import on_group_poll_answer
        from app.services import group_tests

        gqm = await group_tests.by_poll(session, poll_answer.poll_id)
        if gqm is not None:  # a question posted in a group
            await on_group_poll_answer(bot, session, poll_answer, gqm.id, LETTERS[poll_answer.option_ids[0]])
        return
    chat_id, message_id, position = poll.chat_id, poll.message_id, poll.position
    result = await attempt_service.submit_answer(
        session, user, poll.attempt_id, position, LETTERS[poll_answer.option_ids[0]]
    )
    if result.outcome == AnswerOutcome.PAUSED:
        await bot.send_message(chat_id, t("quiz.paused"))
        return
    if result.outcome not in (AnswerOutcome.ACCEPTED, AnswerOutcome.EXPIRED):
        return  # answered already (e.g. an older copy of the poll) or the attempt is over
    attempt = result.attempt
    assert attempt is not None
    test = await session.get(Test, attempt.test_id)
    assert test is not None
    try:  # the time button is no longer needed under an answered question
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id)
    except TelegramBadRequest:
        pass
    if result.outcome == AnswerOutcome.EXPIRED:
        await bot.send_message(chat_id, t("emp.answer.expired"))
        text, markup = await _final_result(session, test, attempt)
        await bot.send_message(chat_id, text, reply_markup=markup)
        return
    if test.answer_reveal == AnswerReveal.IMMEDIATE and result.question is not None:
        # Telegram's explanation pop-up disappears after a few seconds: this card stays in the chat.
        await bot.send_message(
            chat_id,
            poll_verdict_text(result.question, list(attempt.layout[position]["opts"]), position,
                              attempt.total_questions, result.selected_display or LETTERS[poll_answer.option_ids[0]]),
        )  # fmt: skip
    if attempt.status == AttemptStatus.IN_PROGRESS:
        await send_current_question(bot, chat_id, session, attempt)
        return
    text, markup = await _final_result(session, test, attempt)
    await bot.send_message(chat_id, text, reply_markup=markup)


@router.callback_query(EmpCB.filter(F.a == "time"))
async def cb_time_left(callback: CallbackQuery, callback_data: EmpCB, session: AsyncSession, user: User | None) -> None:
    attempt = await session.get(TestAttempt, callback_data.id)
    if user is None or attempt is None or attempt.user_id != user.id:
        await callback.answer()
        return
    left = (attempt.deadline_at - utcnow()).total_seconds()
    if attempt.status != AttemptStatus.IN_PROGRESS or left <= 0:
        await callback.answer(t("emp.answer.not_in_progress"), show_alert=True)
        return
    await callback.answer(t("quiz.time_alert", d=fmt_span(left), end=fmt_dt(attempt.deadline_at)), show_alert=True)


@router.callback_query(EmpCB.filter(F.a == "next"))
async def cb_close_result(
    callback: CallbackQuery, callback_data: EmpCB, session: AsyncSession, bot: Bot, user: User | None
) -> None:
    """➡️ Keyingi savol: the answered card stays in the chat as it is (only its button goes away);
    the next question (or the final result after the last one) comes as a new message."""
    if user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    attempt = await session.get(TestAttempt, callback_data.id)
    if attempt is None or attempt.user_id != user.id:
        await callback.answer(t("errors.stale_button"), show_alert=True)
        return
    test = await session.get(Test, attempt.test_id)
    assert test is not None
    now = utcnow()
    if attempt.status == AttemptStatus.IN_PROGRESS and now >= attempt.deadline_at:
        await attempt_service.finalize_attempt(session, attempt, AttemptStatus.EXPIRED, now)
        await session.commit()
    await callback.answer()
    message = callback.message
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    if attempt.status == AttemptStatus.IN_PROGRESS and await attempt_service.current_question(session, attempt):
        await send_current_question(bot, message.chat.id, session, attempt)
        return
    text, markup = await _final_result(session, test, attempt)
    await message.answer(text, reply_markup=markup)


@router.callback_query(EmpCB.filter(F.a == "res"))
async def cb_result(callback: CallbackQuery, callback_data: EmpCB, session: AsyncSession, user: User | None) -> None:
    if user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    attempt = await session.get(TestAttempt, callback_data.id)
    if attempt is None or attempt.user_id != user.id:  # privacy: only own results
        await callback.answer(t("errors.stale_button"), show_alert=True)
        return
    if attempt.status == AttemptStatus.IN_PROGRESS:
        await callback.answer(t("emp.answer.still_in_progress"), show_alert=True)
        return
    await callback.answer()
    test = await session.get(Test, attempt.test_id)
    assert test is not None
    text, markup = await _final_result(session, test, attempt)
    await callback.message.answer(text, reply_markup=markup)


@router.callback_query(EmpCB.filter(F.a == "corr"))
async def cb_corrections(
    callback: CallbackQuery, callback_data: EmpCB, session: AsyncSession, user: User | None
) -> None:
    if user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    attempt = await session.get(TestAttempt, callback_data.id)
    if attempt is None or attempt.user_id != user.id:
        await callback.answer(t("errors.stale_button"), show_alert=True)
        return
    test = await session.get(Test, attempt.test_id)
    if test is None or test.answer_reveal == AnswerReveal.NEVER or attempt.status == AttemptStatus.IN_PROGRESS:
        await callback.answer(t("emp.corrections.hidden"), show_alert=True)
        return
    await callback.answer()
    items = await attempt_service.wrong_answers_for_attempt(session, attempt)
    if not items:
        await callback.message.answer(t("emp.corrections.none"))
        return
    text = (
        t("emp.corrections.title", title=esc(test.title))
        + "\n\n"
        + "\n\n".join(corrections_text(items, attempt.layout))
    )
    for part in split_message(text):
        await callback.message.answer(part)
