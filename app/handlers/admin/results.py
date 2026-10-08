"""📊 Natijalar: per test — every employee's score; per employee — every answer with its source.

Also the per-employee admin actions: extra time, allow a retake, take the test away."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.handlers.admin.common import show
from app.keyboards.callbacks import AdminCB, EmpCB
from app.keyboards.common import back_menu_row, confirm_kb, kb, pager
from app.locales import t
from app.models import (
    DURATION_CHOICES,
    AttemptStatus,
    ParticipationStatus,
    Test,
    TestAttempt,
    TestStatus,
    User,
)
from app.services import assignments
from app.services import attempts as attempt_service
from app.services.notifications import safe_send
from app.statistics.participation import participation
from app.statistics.sources import attempt_source_breakdown, test_source_breakdown
from app.utils.text import esc, pct, truncate
from app.utils.time import fmt_dt, fmt_hours, fmt_span, utcnow

logger = logging.getLogger(__name__)
router = Router(name="admin_results")

TESTS_PAGE = 8
PEOPLE_PAGE = 8
ANSWERS_PAGE = 8
STATE_ICON = {
    ParticipationStatus.COMPLETED: "🔵",
    ParticipationStatus.IN_PROGRESS: "🟡",
    ParticipationStatus.EXPIRED: "🔴",
    ParticipationStatus.CANCELLED: "🚫",
    ParticipationStatus.NOT_STARTED: "🟢",
}


@router.callback_query(AdminCB.filter(F.s == "res"))
async def cb_tests(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    page = max(0, callback_data.p)
    base = select(Test).where(Test.status.in_([TestStatus.ACTIVE, TestStatus.EXPIRED, TestStatus.CLOSED]))
    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    tests = list(
        (await session.execute(base.order_by(Test.created_at.desc()).offset(page * TESTS_PAGE).limit(TESTS_PAGE)))
        .scalars()
        .all()
    )
    counts = dict(
        (
            await session.execute(
                select(TestAttempt.test_id, func.count(func.distinct(TestAttempt.user_id)))
                .where(TestAttempt.test_id.in_([x.id for x in tests]))
                .group_by(TestAttempt.test_id)
            )
        ).all()
    )
    rows = [[(f"📋 {truncate(x.title, 36)} ({counts.get(x.id, 0)})", AdminCB(s="res_t", id=x.id))] for x in tests]
    text = t("res.title", total=total) + ("" if tests else "\n\n" + t("common.empty"))
    await show(callback, text, kb(*rows, pager("res", page, total, TESTS_PAGE), back_menu_row("menu")))


@router.callback_query(AdminCB.filter(F.s == "res_t"))
async def cb_test_results(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    test = await session.get(Test, callback_data.id)
    if test is None:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    summary = await participation(session, test, utcnow())
    people = [r for r in summary.rows if r.attempt is not None]
    page = max(0, callback_data.p)
    chunk = people[page * PEOPLE_PAGE : (page + 1) * PEOPLE_PAGE]
    lines = [f"📋 <b>{esc(test.title)}</b>", t("res.summary", started=len(people), total=summary.total,
             done=summary.count(ParticipationStatus.COMPLETED), none=summary.count(ParticipationStatus.NOT_STARTED))]  # fmt: skip
    for r in chunk:
        a = r.attempt
        assert a is not None
        lines += [
            "",
            f"{STATE_ICON[r.status]} <b>{esc(r.user.display_name)}</b>",
            t("res.row", total=a.total_questions, correct=a.correct_count, wrong=a.incorrect_count,
              score=pct(a.score_percent)),
        ]  # fmt: skip
    if not people:
        lines += ["", t("res.nobody")]
    sources = await test_source_breakdown(session, test.id)
    if len(sources) > 1:
        lines += ["", t("emp.result.by_source")]
        for row in sources:
            lines += [f"<b>{esc(row.name)}</b>", t("res.source_row", answered=row.answered, correct=row.correct,
                                                    p=pct(row.correct / row.answered * 100 if row.answered else None))]  # fmt: skip
    rows = [
        [
            (
                f"👤 {truncate(r.user.display_name, 34)} — {pct(r.attempt.score_percent)}",
                AdminCB(s="res_a", id=r.attempt.id),
            )
        ]
        for r in chunk
        if r.attempt is not None
    ]
    await show(
        callback,
        "\n".join(lines),
        kb(
            *rows,
            pager("res_t", page, len(people), PEOPLE_PAGE, id_=test.id),
            [(t("tests.btn.not_participated"), AdminCB(s="tst_part", id=test.id, v="none"))],
            [(t("tests.btn.report_xlsx"), AdminCB(s="tst_rep", id=test.id))],
            back_menu_row("tst_v", id_=test.id),
        ),
    )


async def render_attempt(target, session: AsyncSession, attempt_id: int, page: int = 0) -> None:
    attempt = await session.get(TestAttempt, attempt_id)
    if attempt is None:
        await show(target, t("common.not_found"), kb(back_menu_row("res")))
        return
    user = await session.get(User, attempt.user_id)
    test = await session.get(Test, attempt.test_id)
    assert user is not None and test is not None
    answers = {a.test_question_id: (a, tq) for a, tq in await attempt_service.answers_for_attempt(session, attempt)}
    from app.models import TestQuestion

    total = len(attempt.layout)
    page = min(max(0, page), max(0, (total - 1) // ANSWERS_PAGE))
    lines = [
        f"👤 <b>{esc(user.display_name)}</b>",
        f"📋 {esc(test.title)}",
        t("res.attempt", status=t(f"attempt_status.{attempt.status.value}"), correct=attempt.correct_count,
          wrong=attempt.incorrect_count, total=attempt.total_questions, score=pct(attempt.score_percent)),
        t("res.times", start=fmt_dt(attempt.started_at), end=fmt_dt(attempt.deadline_at),
          spent=fmt_span(attempt.duration_seconds) if attempt.duration_seconds is not None else "—"),
    ]  # fmt: skip
    for index in range(page * ANSWERS_PAGE, min(total, (page + 1) * ANSWERS_PAGE)):
        item = attempt.layout[index]
        pair = answers.get(item["tq"])
        tq = pair[1] if pair else await session.get(TestQuestion, item["tq"])
        if tq is None:
            continue
        display = {orig: "ABCDE"[i] for i, orig in enumerate(item["opts"])}
        lines += ["", t("res.q_header", n=index + 1, q=esc(truncate(tq.question_text, 140)))]
        if pair is None:
            lines.append(t("res.q_unanswered"))
        else:
            answer = pair[0]
            lines.append(
                t("res.q_chosen", a=f"{answer.selected_display}) {esc(tq.options.get(answer.selected_option, ''))}")
            )
            lines.append(t("res.q_ok") if answer.is_correct else t("res.q_bad"))
            if not answer.is_correct:
                corr = answer.correct_display or display.get(tq.correct_option, tq.correct_option)
                lines.append(t("res.q_correct", a=f"{corr}) {esc(tq.options[tq.correct_option])}"))
        if tq.source_name:
            lines.append(t("res.q_source", s=esc(tq.source_name)))
    sources = await attempt_source_breakdown(session, attempt.id)
    if len(sources) > 1:
        lines += ["", t("emp.result.by_source")]
        for row in sources:
            lines += [f"<b>{esc(row.name)}</b>", t("emp.result.source_row", total=row.total, correct=row.correct)]
    rows = []
    unfinished = attempt.answered_count < attempt.total_questions
    if attempt.status == AttemptStatus.IN_PROGRESS or (attempt.status == AttemptStatus.EXPIRED and unfinished):
        rows.append([(t("res.btn.extra_time"), AdminCB(s="res_xt", id=attempt.id))])
    if attempt.status != AttemptStatus.IN_PROGRESS:
        rows.append([(t("res.btn.retake"), AdminCB(s="res_rt", id=attempt.id))])
    rows.append([(t("res.btn.remove"), AdminCB(s="res_rm", id=attempt.id))])
    rows.append([(t("emp_admin.btn.profile"), AdminCB(s="emp_v", id=user.id))])
    await show(
        target,
        "\n".join(lines),
        kb(*rows, pager("res_a", page, total, ANSWERS_PAGE, id_=attempt.id), back_menu_row("res_t", id_=test.id)),
    )


@router.callback_query(AdminCB.filter(F.s == "res_a"))
async def cb_attempt(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    await render_attempt(callback, session, callback_data.id, callback_data.p)


# ------------------------------------------------------------------------------ per-employee actions


@router.callback_query(AdminCB.filter(F.s == "res_xt"))
async def cb_extra_time(callback: CallbackQuery, callback_data: AdminCB) -> None:
    await show(
        callback,
        t("res.extra_time_prompt"),
        kb(
            *[
                [(f"+{fmt_hours(s / 3600)}", AdminCB(s="res_xt_go", id=callback_data.id, v=str(s)))]
                for s in DURATION_CHOICES
            ],
            back_menu_row("res_a", id_=callback_data.id),
        ),
    )


@router.callback_query(AdminCB.filter(F.s == "res_xt_go"))
async def cb_extra_time_go(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, bot: Bot) -> None:
    seconds = int(callback_data.v or 0)
    if seconds not in DURATION_CHOICES:
        await callback.answer(t("errors.stale_button"), show_alert=True)
        return
    try:
        attempt = await attempt_service.add_time(session, callback_data.id, seconds)
    except ValueError as exc:
        await callback.answer(t(f"res.err.{exc}"), show_alert=True)
        return
    user = await session.get(User, attempt.user_id)
    test = await session.get(Test, attempt.test_id)
    await callback.answer(t("res.extra_time_done", d=fmt_dt(attempt.deadline_at)), show_alert=True)
    if user is not None and test is not None and user.has_private_chat:
        await safe_send(
            bot,
            user.telegram_id,
            t(
                "res.notify_extra_time",
                title=esc(test.title),
                plus=fmt_hours(seconds / 3600),
                d=fmt_dt(attempt.deadline_at),
            ),
            kb([(t("emp.btn.continue_test"), EmpCB(a="start", id=test.id))]),
        )
    await render_attempt(callback, session, attempt.id)


@router.callback_query(AdminCB.filter(F.s == "res_rt"))
async def cb_retake(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, bot: Bot, user: User
) -> None:
    attempt = await session.get(TestAttempt, callback_data.id)
    if attempt is None:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    test_id, user_id, attempt_id = attempt.test_id, attempt.user_id, attempt.id
    await assignments.allow_retake(session, test_id, user_id, user.id)
    await callback.answer(t("res.retake_done"), show_alert=True)
    employee = await session.get(User, user_id)
    test = await session.get(Test, test_id)
    if employee is not None and test is not None and employee.has_private_chat:
        await safe_send(bot, employee.telegram_id, t("res.notify_retake", title=esc(test.title)),
                        kb([(t("emp.btn.retake"), EmpCB(a="card", id=test_id))]))  # fmt: skip
    await render_attempt(callback, session, attempt_id)


@router.callback_query(AdminCB.filter(F.s == "res_rm"))
async def cb_remove(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    attempt = await session.get(TestAttempt, callback_data.id)
    if attempt is None:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    employee = await session.get(User, attempt.user_id)
    await show(
        callback,
        t("res.remove_confirm", name=esc(employee.display_name if employee else "—"), title=esc(attempt.test.title)),
        confirm_kb(AdminCB(s="res_rm_ok", id=attempt.id), AdminCB(s="res_a", id=attempt.id)),
    )


@router.callback_query(AdminCB.filter(F.s == "res_rm_ok"))
async def cb_remove_ok(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, bot: Bot, user: User
) -> None:
    attempt = await session.get(TestAttempt, callback_data.id)
    if attempt is None:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    test_id, user_id = attempt.test_id, attempt.user_id
    await assignments.remove_from_test(session, test_id, user_id, user.id)
    await callback.answer(t("res.removed"), show_alert=True)
    employee = await session.get(User, user_id)
    test = await session.get(Test, test_id)
    if employee is not None and test is not None and employee.has_private_chat:
        await safe_send(bot, employee.telegram_id, t("res.notify_removed", title=esc(test.title)))
    await cb_test_results(callback, AdminCB(s="res_t", id=test_id), session)
