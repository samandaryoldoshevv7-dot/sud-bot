"""Employee flows: available tests, taking a test, results and corrections."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.handlers.formatting import (
    answered_question_text,
    corrections_text,
    employee_test_card,
    question_text,
    result_text,
)
from app.keyboards.callbacks import AnsCB, EmpCB
from app.keyboards.common import kb
from app.keyboards.employee import answer_kb
from app.locales import t
from app.models import AnswerReveal, AttemptStatus, DeliveryMode, Test, TestAttempt, TestStatus, User, UserStatus
from app.services import attempts as attempt_service
from app.services.attempts import AnswerOutcome, StartError
from app.utils.text import esc, pct, split_message, truncate
from app.utils.time import fmt_dt

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
    StartError.GROUP_ONLY: "emp.start_error.group_only",
}


def _can_use(user: User | None, is_admin: bool) -> bool:
    return user is not None and (is_admin or user.status == UserStatus.ACTIVE)


def _card_keyboard(test: Test, attempt: TestAttempt | None):
    if test.delivery_mode == DeliveryMode.GROUP and (attempt is None or attempt.status == AttemptStatus.IN_PROGRESS):
        return None  # answered inside the group, see the card text
    if attempt is not None and attempt.status == AttemptStatus.IN_PROGRESS:
        return kb([(t("emp.btn.continue_test"), EmpCB(a="start", id=test.id))])
    if attempt is not None and attempt.status in (AttemptStatus.COMPLETED, AttemptStatus.EXPIRED):
        rows = [[(t("emp.btn.view_result"), EmpCB(a="res", id=attempt.id))]]
        return kb(*rows)
    if test.status == TestStatus.ACTIVE:
        return kb([(t("emp.btn.start_test"), EmpCB(a="start", id=test.id))])
    return None


async def send_test_card(message: Message, session: AsyncSession, user: User, test_id: int) -> None:
    test = await session.get(Test, test_id)
    if test is None or test.status == TestStatus.DRAFT:
        await message.answer(t("emp.start_error.not_found"))
        return
    attempts = [a for tst, a in await attempt_service.available_tests_for_user(session, user) if tst.id == test_id]
    attempt = attempts[0] if attempts else None
    if test.status != TestStatus.ACTIVE and attempt is None:
        key = (
            "emp.start_error.not_started_yet" if test.status == TestStatus.READY else "emp.start_error.deadline_passed"
        )
        await message.answer(t(key))
        return
    await message.answer(employee_test_card(test, attempt), reply_markup=_card_keyboard(test, attempt))


@router.message(F.text == t("emp.btn.my_tests"))
async def my_tests(message: Message, session: AsyncSession, user: User | None, is_admin: bool) -> None:
    if not _can_use(user, is_admin):
        await message.answer(
            t("register.pending") if user and user.status == UserStatus.PENDING else t("start.inactive")
        )
        return
    assert user is not None
    items = await attempt_service.available_tests_for_user(session, user)
    if not items:
        await message.answer(t("emp.no_tests"))
        return
    rows = []
    lines = [t("emp.my_tests.title"), ""]
    for test, attempt in items:
        if attempt is None:
            mark = "🆕"
        elif attempt.status == AttemptStatus.IN_PROGRESS:
            mark = "⏳"
        elif attempt.status == AttemptStatus.COMPLETED:
            mark = "✅"
        else:
            mark = "⌛"
        lines.append(f"{mark} <b>{esc(test.title)}</b> — {t('emp.my_tests.deadline', d=fmt_dt(test.deadline_at))}")
        rows.append([(f"{mark} {truncate(test.title, 40)}", EmpCB(a="card", id=test.id))])
    await message.answer("\n".join(lines), reply_markup=kb(*rows))


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


async def send_current_question(bot: Bot, chat_id: int, session: AsyncSession, attempt: TestAttempt) -> None:
    current = await attempt_service.current_question(session, attempt)
    if current is None:
        return
    tq, item = current
    test = await session.get(Test, attempt.test_id)
    assert test is not None
    letters = [chr(ord("A") + i) for i in range(len(item["opts"]))]
    sent = await bot.send_message(
        chat_id,
        question_text(test, attempt, tq, item),
        reply_markup=answer_kb(attempt.id, attempt.current_index, letters),
    )
    await attempt_service.set_message_ref(session, attempt.id, chat_id, sent.message_id)


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
    result = await attempt_service.start_attempt(session, user, callback_data.id, chat_id=callback.message.chat.id)
    if result.error is not None:
        await callback.answer(t(START_ERRORS[result.error]), show_alert=True)
        return
    attempt = result.attempt
    assert attempt is not None
    await callback.answer(t("emp.resumed") if result.resumed else t("emp.started"))
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    if result.resumed and attempt.chat_id and attempt.last_message_id:
        # Disable the old question message so only the freshly sent one is answerable.
        try:
            await bot.edit_message_reply_markup(
                chat_id=attempt.chat_id, message_id=attempt.last_message_id, reply_markup=None
            )
        except TelegramBadRequest:
            pass
    else:
        test = await session.get(Test, attempt.test_id)
        await callback.message.answer(
            t("emp.test_started", title=esc(test.title if test else ""), n=attempt.total_questions)
        )
    await send_current_question(bot, callback.message.chat.id, session, attempt)


@router.callback_query(AnsCB.filter())
async def cb_answer(
    callback: CallbackQuery, callback_data: AnsCB, session: AsyncSession, bot: Bot, user: User | None, is_admin: bool
) -> None:
    if user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    result = await attempt_service.submit_answer(session, user, callback_data.at, callback_data.pos, callback_data.o)
    message = callback.message

    if result.outcome == AnswerOutcome.DUPLICATE:
        await callback.answer(t("emp.answer.duplicate"))
        return
    if result.outcome == AnswerOutcome.INVALID:
        await callback.answer(t("errors.stale_button"), show_alert=True)
        return
    if result.outcome == AnswerOutcome.NOT_IN_PROGRESS:
        await callback.answer(t("emp.answer.not_in_progress"), show_alert=True)
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
        try:
            await message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await message.answer(result_text(test, attempt))
        return

    await callback.answer()
    try:
        await message.edit_text(
            answered_question_text(
                message.html_text or "",
                result.selected_display or callback_data.o,
                test.answer_reveal,
                result.is_correct,
                result.correct_display,
                result.question,
            ),
            reply_markup=None,
        )
    except TelegramBadRequest as exc:
        logger.debug("Could not edit answered question", extra={"error": str(exc)[:100]})

    if result.finished:
        await send_result(message, session, test, attempt)
    else:
        await send_current_question(bot, message.chat.id, session, attempt)


async def send_result(message: Message, session: AsyncSession, test: Test, attempt: TestAttempt) -> None:
    rows = []
    if test.answer_reveal == AnswerReveal.AFTER_COMPLETION and attempt.incorrect_count:
        rows.append([(t("emp.btn.corrections"), EmpCB(a="corr", id=attempt.id))])
    await message.answer(result_text(test, attempt), reply_markup=kb(*rows) if rows else None)


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
    rows = []
    if test.answer_reveal != AnswerReveal.NEVER and attempt.incorrect_count:
        rows.append([(t("emp.btn.corrections"), EmpCB(a="corr", id=attempt.id))])
    await callback.message.answer(result_text(test, attempt), reply_markup=kb(*rows) if rows else None)


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
