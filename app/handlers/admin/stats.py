"""📊 Statistics dashboard, 🏆 rankings and 📈 report exports."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.handlers.admin.common import show
from app.handlers.formatting import STATUS_EMOJI
from app.keyboards.callbacks import AdminCB
from app.keyboards.common import back_menu_row, kb, pager
from app.locales import t
from app.models import TestStatus
from app.reports.builder import build_summary_report_xlsx
from app.services import settings_service
from app.services import test_builder as tb
from app.statistics.dashboard import common_mistakes, dashboard, hardest_questions
from app.statistics.employee import split_topics, topic_stats
from app.statistics.rankings import PERIODS, ranking
from app.utils.text import esc, pct, progress_bar, truncate
from app.utils.time import local_period_start

router = Router(name="admin_stats")
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


@router.callback_query(AdminCB.filter(F.s == "st"))
async def cb_dashboard(callback: CallbackQuery, session: AsyncSession) -> None:
    min_answers = int(await settings_service.get_value(session, "weak_topic_min_answers"))
    d = await dashboard(session, min_answers)
    accuracy = d.answers_correct / d.answers_total * 100 if d.answers_total else None
    lines = [
        t("stats.title"),
        "",
        t("stats.employees", total=d.total_employees, active=d.active_employees, pending=d.pending_employees),
        t(
            "stats.tests",
            total=d.tests_total,
            active=d.tests_by_status.get("ACTIVE", 0),
            draft=d.tests_by_status.get("DRAFT", 0),
            ended=d.tests_by_status.get("EXPIRED", 0) + d.tests_by_status.get("CLOSED", 0),
        ),
        t(
            "stats.attempts",
            completed=d.attempts_completed,
            progress=d.attempts_in_progress,
            expired=d.attempts_expired,
        ),
        t("stats.average", avg=pct(d.average_score)),
        t(
            "stats.participation",
            rate=pct(d.participation_rate),
            bar=progress_bar(d.participation_rate or 0) if d.participation_rate is not None else "",
        ),
        t("stats.answers", total=d.answers_total, correct=d.answers_correct, acc=pct(accuracy)),
    ]
    if d.weak_topics:
        lines += ["", t("stats.weak_topics")]
        lines += [f"• {esc(s.topic)} — {pct(s.percent)} ({s.correct}/{s.total})" for s in d.weak_topics]
    await show(
        callback,
        "\n".join(lines),
        kb(
            [(t("stats.btn.hardest"), AdminCB(s="st_hard")), (t("stats.btn.mistakes"), AdminCB(s="st_mist"))],
            [(t("stats.btn.topics"), AdminCB(s="st_top")), (t("menu.rankings"), AdminCB(s="rk", v="week"))],
            [(t("btn.refresh"), AdminCB(s="st"))],
            back_menu_row("menu"),
        ),
    )


@router.callback_query(AdminCB.filter(F.s == "st_hard"))
async def cb_hardest(callback: CallbackQuery, session: AsyncSession) -> None:
    rows = await hardest_questions(session, limit=10)
    lines = [t("stats.hardest.title"), ""]
    if not rows:
        lines.append(t("stats.not_enough_data"))
    for i, r in enumerate(rows, 1):
        lines.append(
            t(
                "stats.hardest.item",
                i=i,
                pct=pct(r.percent),
                c=r.correct,
                n=r.answers,
                q=esc(truncate(r.question.question_text, 140)),
                test=esc(truncate(r.test_title, 40)),
                topic=esc(r.question.topic_name or "—"),
            )
        )
    await show(callback, "\n".join(lines), kb(back_menu_row("st")))


@router.callback_query(AdminCB.filter(F.s == "st_mist"))
async def cb_mistakes(callback: CallbackQuery, session: AsyncSession) -> None:
    rows = await common_mistakes(session, limit=10)
    lines = [t("stats.mistakes.title"), ""]
    if not rows:
        lines.append(t("stats.not_enough_data"))
    for r in rows:
        q = r.question
        lines.append(
            t(
                "stats.mistakes.item",
                q=esc(truncate(q.question_text, 120)),
                opt=f"{r.wrong_option}) {esc(truncate(q.options.get(r.wrong_option, ''), 80))}",
                n=r.times,
            )
            + "\n"
            + t(
                "stats.mistakes.correct",
                opt=f"{q.correct_option}) {esc(truncate(q.options.get(q.correct_option, ''), 80))}",
            )
        )
    await show(callback, "\n\n".join(lines), kb(back_menu_row("st")))


@router.callback_query(AdminCB.filter(F.s == "st_top"))
async def cb_topics(callback: CallbackQuery, session: AsyncSession) -> None:
    min_answers = int(await settings_service.get_value(session, "weak_topic_min_answers"))
    stats = await topic_stats(session)
    weak, strong = split_topics(stats, min_answers, limit=10)
    lines = [t("stats.topics.title"), ""]
    if not weak and not strong:
        lines.append(t("stats.not_enough_data"))
    if weak:
        lines.append(t("emp_admin.profile.weak"))
        lines += [f"• {esc(s.topic)} — {pct(s.percent)} ({s.correct}/{s.total})" for s in weak]
    if strong:
        lines += ["", t("emp_admin.profile.strong")]
        lines += [f"• {esc(s.topic)} — {pct(s.percent)} ({s.correct}/{s.total})" for s in strong]
    await show(callback, "\n".join(lines), kb(back_menu_row("st")))


@router.callback_query(AdminCB.filter(F.s == "rk"))
async def cb_ranking(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    period = callback_data.v if callback_data.v in PERIODS else "week"
    min_attempts = int(await settings_service.get_value(session, "ranking_min_attempts"))
    rows = await ranking(session, period, min_attempts=min_attempts, limit=20)
    lines = [t("rank.title", period=t(f"rank.period.{period}")), ""]
    if not rows:
        lines.append(t("rank.empty", n=min_attempts))
    for i, r in enumerate(rows, 1):
        lines.append(
            f"{MEDALS.get(i, f'{i}.')} {esc(r.user.display_name)} — <b>{pct(r.percent)}</b> "
            f"({r.correct}/{r.total}, {t('rank.attempts', n=r.attempts)})"
        )
    lines += ["", t("rank.note", n=min_attempts)]
    buttons = [(("• " if p == period else "") + t(f"rank.period.{p}"), AdminCB(s="rk", v=p)) for p in PERIODS]
    await show(callback, "\n".join(lines), kb(buttons[:2], buttons[2:], back_menu_row("menu")))


@router.callback_query(AdminCB.filter(F.s == "rep"))
async def cb_reports(callback: CallbackQuery) -> None:
    await show(
        callback,
        t("reports.title"),
        kb(
            [(t("reports.btn.by_test"), AdminCB(s="rep_t"))],
            [
                (t("reports.btn.all_week"), AdminCB(s="rep_all", v="week")),
                (t("reports.btn.all_month"), AdminCB(s="rep_all", v="month")),
            ],
            [(t("reports.btn.all_time"), AdminCB(s="rep_all", v="all"))],
            back_menu_row("menu"),
        ),
    )


@router.callback_query(AdminCB.filter(F.s == "rep_t"))
async def cb_report_tests(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    page = max(0, callback_data.p)
    tests, total = await tb.list_tests(session, page)
    tests = [x for x in tests if x.status != TestStatus.DRAFT]
    rows = [
        [(f"{STATUS_EMOJI[x.status]} #{x.id} {truncate(x.title, 34)}", AdminCB(s="tst_rep", id=x.id))] for x in tests
    ]
    await show(
        callback,
        t("reports.choose_test") if rows else t("tests.empty"),
        kb(*rows, pager("rep_t", page, total, tb.PAGE_SIZE), back_menu_row("rep")),
    )


@router.callback_query(AdminCB.filter(F.s == "rep_all"))
async def cb_report_all(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    await callback.answer(t("reports.generating"))
    since = local_period_start(callback_data.v) if callback_data.v in ("week", "month") else None
    data, filename = await build_summary_report_xlsx(session, since)
    assert isinstance(callback.message, Message)
    await callback.message.answer_document(BufferedInputFile(data, filename=filename), caption=t("reports.caption"))
