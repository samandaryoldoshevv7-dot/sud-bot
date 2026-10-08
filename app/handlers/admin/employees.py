"""👥 Employees: list, search, profile, statistics, wrong answers, activation."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.handlers.admin.common import show
from app.handlers.formatting import user_line
from app.keyboards.callbacks import AdminCB
from app.keyboards.common import back_menu_row, cancel_kb, kb, pager
from app.locales import t
from app.models import User, UserRole, UserStatus
from app.services import settings_service
from app.services import users as user_service
from app.services.notifications import safe_send
from app.services.users import PAGE_SIZE, UserFilter
from app.statistics.employee import employee_stats, wrong_answers
from app.utils.text import esc, pct, truncate
from app.utils.time import fmt_dt, fmt_duration

router = Router(name="admin_employees")

STATUS_ICON = {UserStatus.ACTIVE: "🟢", UserStatus.PENDING: "🟡", UserStatus.INACTIVE: "🔴"}


class EmployeeStates(StatesGroup):
    search = State()
    rename = State()


@router.callback_query(AdminCB.filter(F.s == "emp"))
async def cb_section(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    counts = await user_service.employee_counts(session)
    text = t(
        "emp_admin.section",
        total=counts["total"],
        active=counts["active"],
        pending=counts["pending"],
        inactive=counts["inactive"],
    )
    await show(
        callback,
        text,
        kb(
            [
                (t("emp_admin.btn.all"), AdminCB(s="emp_l", v="all")),
                (t("emp_admin.btn.active"), AdminCB(s="emp_l", v="active")),
            ],
            [
                (t("emp_admin.btn.pending", n=counts["pending"]), AdminCB(s="emp_l", v="pending")),
                (t("emp_admin.btn.inactive"), AdminCB(s="emp_l", v="inactive")),
            ],
            [(t("emp_admin.btn.search"), AdminCB(s="emp_srch"))],
            back_menu_row("menu"),
        ),
    )


async def _render_list(target, session: AsyncSession, status: str, page: int, query: str = "") -> None:
    users, total = await user_service.list_employees(session, UserFilter(status=status, query=query), page)
    title_key = "emp_admin.search_results" if query else f"emp_admin.list.{status}"
    lines = [t(title_key, q=esc(query), total=total), ""]
    if not users:
        lines.append(t("common.empty"))
    rows = []
    for user in users:
        rows.append(
            [
                (
                    f"{STATUS_ICON[user.status]} {truncate(user.display_name, 40)}",
                    AdminCB(s="emp_v", id=user.id, v=status),
                )
            ]
        )
    nav = pager("emp_l", page, total, PAGE_SIZE, v=status) if not query else []
    await show(target, "\n".join(lines), kb(*rows, nav, back_menu_row("emp")))


@router.callback_query(AdminCB.filter(F.s == "emp_l"))
async def cb_list(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    await _render_list(callback, session, callback_data.v or "all", max(0, callback_data.p))


@router.callback_query(AdminCB.filter(F.s == "emp_srch"))
async def cb_search(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EmployeeStates.search)
    await show(callback, t("emp_admin.search_prompt"), cancel_kb())


@router.message(EmployeeStates.search, F.text)
async def on_search(message: Message, state: FSMContext, session: AsyncSession) -> None:
    query = (message.text or "").strip()[:64]
    if len(query) < 2:
        await message.answer(t("emp_admin.search_short"), reply_markup=cancel_kb())
        return
    await state.clear()
    await _render_list(message, session, "all", 0, query)


async def render_profile(target, session: AsyncSession, user_id: int, back: str = "all") -> None:
    user = await session.get(User, user_id)
    if user is None:
        await show(target, t("common.not_found"), kb(back_menu_row("emp")))
        return
    min_answers = int(await settings_service.get_value(session, "weak_topic_min_answers"))
    stats = await employee_stats(session, user, min_answers)
    groups = await user_service.user_groups(session, user.id)
    lines = [
        t("emp_admin.profile.title"),
        "",
        t("emp_admin.profile.name", name=esc(user.display_name)),
        t("emp_admin.profile.tg_name", name=esc(" ".join(p for p in (user.first_name, user.last_name) if p) or "—")),
        t("emp_admin.profile.tg_id", id=user.telegram_id),
        t("emp_admin.profile.username", u=f"@{esc(user.username)}" if user.username else "—"),
        t("emp_admin.profile.group", g=esc(", ".join(g.title for g in groups)) or "—"),
        t("emp_admin.profile.status", s=t(f"user_status.{user.status.value}")),
        t("emp_admin.profile.registered", d=fmt_dt(user.created_at)),
        "",
        t(
            "emp_admin.profile.tests",
            total=stats.total_tests,
            completed=stats.completed_tests,
            expired=stats.expired_tests,
            missed=stats.missed_tests,
        ),
        t(
            "emp_admin.profile.scores",
            avg=pct(stats.average_score),
            best=pct(stats.best_score),
            worst=pct(stats.worst_score),
        ),
        t("emp_admin.profile.answers", total=stats.total_questions, correct=stats.correct, incorrect=stats.incorrect),
        t("emp_admin.profile.time", time=fmt_duration(stats.total_time_seconds)),
    ]
    if stats.weak_topics:
        lines += ["", t("emp_admin.profile.weak")]
        lines += [f"• {esc(s.topic)} — {pct(s.percent)} ({s.correct}/{s.total})" for s in stats.weak_topics]
    if stats.strong_topics:
        lines += ["", t("emp_admin.profile.strong")]
        lines += [f"• {esc(s.topic)} — {pct(s.percent)} ({s.correct}/{s.total})" for s in stats.strong_topics]
    if stats.recent:
        lines += ["", t("emp_admin.profile.recent")]
        for a in stats.recent:
            lines.append(
                f"• {esc(truncate(a.test.title, 40))} — {pct(a.score_percent)} "
                f"({a.correct_count}/{a.total_questions}), {t(f'attempt_status.{a.status.value}')}, {fmt_dt(a.started_at)}"
            )
    rows = [
        [
            (t("emp_admin.btn.wrong"), AdminCB(s="emp_wr", id=user.id)),
            (t("emp_admin.btn.rename"), AdminCB(s="emp_name", id=user.id)),
        ]
    ]
    if user.role == UserRole.EMPLOYEE:
        if user.status == UserStatus.ACTIVE:
            rows.append([(t("emp_admin.btn.deactivate"), AdminCB(s="emp_deact", id=user.id))])
        elif user.status == UserStatus.PENDING:
            rows.append(
                [
                    (t("emp_admin.btn.approve"), AdminCB(s="emp_appr", id=user.id)),
                    (t("emp_admin.btn.reject"), AdminCB(s="emp_deact", id=user.id)),
                ]
            )
        else:
            rows.append([(t("emp_admin.btn.activate"), AdminCB(s="emp_appr", id=user.id))])
    rows.append(back_menu_row("emp_l", v=back))
    await show(target, "\n".join(lines), kb(*rows))


@router.callback_query(AdminCB.filter(F.s == "emp_v"))
async def cb_profile(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    await render_profile(callback, session, callback_data.id, callback_data.v or "all")


@router.callback_query(AdminCB.filter(F.s.in_({"emp_appr", "emp_deact"})))
async def cb_set_status(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, bot: Bot) -> None:
    status = UserStatus.ACTIVE if callback_data.s == "emp_appr" else UserStatus.INACTIVE
    user = await session.get(User, callback_data.id)
    if user is None:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    if user.role == UserRole.ADMIN:
        await callback.answer(t("emp_admin.admin_immutable"), show_alert=True)
        return
    previous = user.status
    await user_service.set_status(session, user.id, status)
    await callback.answer(t("emp_admin.status_changed"))
    if previous != status and user.has_private_chat:
        key = "emp_admin.notify_activated" if status == UserStatus.ACTIVE else "emp_admin.notify_deactivated"
        await safe_send(bot, user.telegram_id, t(key))
        if status == UserStatus.ACTIVE:
            from app.handlers.test_listing import available_tests_view

            await session.refresh(user)
            view = await available_tests_view(bot, session, user)
            if view is not None:
                await safe_send(bot, user.telegram_id, view[0], view[1])
    await render_profile(callback, session, user.id)


@router.callback_query(AdminCB.filter(F.s == "emp_name"))
async def cb_rename(callback: CallbackQuery, callback_data: AdminCB, state: FSMContext) -> None:
    await state.set_state(EmployeeStates.rename)
    await state.update_data(user_id=callback_data.id)
    await show(callback, t("emp_admin.rename_prompt"), cancel_kb())


@router.message(EmployeeStates.rename, F.text)
async def on_rename(message: Message, state: FSMContext, session: AsyncSession) -> None:
    name = " ".join((message.text or "").split())
    if not 3 <= len(name) <= 120:
        await message.answer(t("register.bad_name"), reply_markup=cancel_kb())
        return
    data = await state.get_data()
    await state.clear()
    user = await session.get(User, int(data.get("user_id", 0)))
    if user is None:
        await message.answer(t("common.not_found"))
        return
    await user_service.set_full_name(session, user, name)
    await message.answer(t("common.saved"))
    await render_profile(message, session, user.id)


@router.callback_query(AdminCB.filter(F.s == "emp_wr"))
async def cb_wrong(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    user = await session.get(User, callback_data.id)
    if user is None:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    page_size = 3
    rows, total = await wrong_answers(session, user.id, max(0, callback_data.p), page_size)
    lines = [t("emp_admin.wrong.title", name=user_line(user), total=total), ""]
    if not rows:
        lines.append(t("emp_admin.wrong.none"))
    for r in rows:
        q = r.question
        lines.append(
            t(
                "emp_admin.wrong.item",
                test=esc(truncate(r.test_title, 60)),
                n=r.answer.position + 1,
                question=esc(truncate(q.question_text, 400)),
                selected=f"{r.answer.selected_display}) {esc(truncate(q.options.get(r.answer.selected_option, ''), 150))}",
                correct=f"{r.answer.correct_display or q.correct_option}) "
                f"{esc(truncate(q.options.get(q.correct_option, ''), 150))}",
                topic=esc(q.topic_name or "—"),
                source=esc(truncate(q.source_reference, 150)),
                explanation=esc(truncate(q.explanation, 300)),
                time=fmt_dt(r.answer.answered_at),
            )
        )
    await show(
        callback,
        "\n\n".join(lines),
        kb(pager("emp_wr", callback_data.p, total, page_size, id_=user.id), back_menu_row("emp_v", id_=user.id)),
    )
