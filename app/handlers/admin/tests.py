"""📝 Test management: detail, question assembly & review, lifecycle, participants, reports."""

from __future__ import annotations

import logging
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.handlers.admin.common import show
from app.handlers.admin.materials import _friendly_error
from app.handlers.formatting import STATUS_EMOJI, user_line
from app.keyboards.callbacks import AdminCB
from app.keyboards.common import back_menu_row, cancel_kb, confirm_kb, kb, pager
from app.locales import t
from app.models import (
    DURATION_CHOICES,
    AttemptStatus,
    DeliveryMode,
    GenerationStatus,
    Group,
    ParticipationStatus,
    Test,
    TestStatus,
    User,
)
from app.reports.builder import build_test_report_csv, build_test_report_xlsx
from app.services import background, group_tests
from app.services import groups as group_service
from app.services import test_builder as tb
from app.services.ai_runtime import ai_available, make_generator
from app.services.notifications import announce_test
from app.services.scheduler import claim_marker, launch_test
from app.services.test_builder import TestStateError
from app.statistics.participation import participation
from app.utils.text import esc, pct, truncate
from app.utils.time import fmt_dt, fmt_duration, fmt_hours, parse_local_datetime, utcnow

logger = logging.getLogger(__name__)
router = Router(name="admin_tests")

PARTICIPANT_PAGE = 12
PART_FILTERS = {
    "all": None,
    "done": ParticipationStatus.COMPLETED,
    "prog": ParticipationStatus.IN_PROGRESS,
    "exp": ParticipationStatus.EXPIRED,
    "none": ParticipationStatus.NOT_STARTED,
    "canc": ParticipationStatus.CANCELLED,
}
PART_ICON = {
    ParticipationStatus.COMPLETED: "✅",
    ParticipationStatus.IN_PROGRESS: "⏳",
    ParticipationStatus.EXPIRED: "⌛",
    ParticipationStatus.CANCELLED: "🚫",
    ParticipationStatus.NOT_STARTED: "⚪️",
}


class TestStates(StatesGroup):
    __test__ = False
    extend = State()


# ------------------------------------------------------------------------------ list & detail


@router.callback_query(AdminCB.filter(F.s == "tst"))
async def cb_list(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    page = max(0, callback_data.p)
    status = TestStatus(callback_data.v) if callback_data.v in TestStatus._value2member_map_ else None
    tests, total = await tb.list_tests(session, page, status)
    lines = [t("tests.section", total=total), ""]
    if not tests:
        lines.append(t("tests.empty"))
    rows = [[(t("menu.create_test"), AdminCB(s="ct"))], [(t("tests.btn.new_advanced"), AdminCB(s="tst_new"))]]
    filters = []
    for st in (TestStatus.DRAFT, TestStatus.ACTIVE, TestStatus.EXPIRED):
        mark = "• " if status == st else ""
        filters.append((mark + t(f"test_status.{st.value}"), AdminCB(s="tst", v=st.value)))
    filters.append((("• " if status is None else "") + t("common.all"), AdminCB(s="tst")))
    rows.append(filters)
    for test in tests:
        rows.append(
            [(f"{STATUS_EMOJI[test.status]} #{test.id} {truncate(test.title, 34)}", AdminCB(s="tst_v", id=test.id))]
        )
    await show(
        callback,
        "\n".join(lines),
        kb(*rows, pager("tst", page, total, tb.PAGE_SIZE, v=callback_data.v), back_menu_row("menu")),
    )


async def render_test(target, session: AsyncSession, test_id: int) -> None:
    test = await session.get(Test, test_id)
    if test is None:
        await show(target, t("common.not_found"), kb(back_menu_row("tst")))
        return
    from app.handlers.admin.create_test import audience_label

    total, approved = await tb.question_counts(session, test_id)
    sources = await tb.source_names(session, test_id) or await tb.test_material_titles(session, test)
    open_ended = test.deadline_at - test.starts_at >= tb.OPEN_WINDOW - timedelta(days=1)
    lines = [
        t("tests.detail.title", id=test.id, title=esc(test.title)),
        t("tests.detail.status", status=f"{STATUS_EMOJI[test.status]} {t(f'test_status.{test.status.value}')}"),
        "",
        t("tests.detail.questions", total=total, need=test.question_count, approved=approved),
        t(
            "tests.detail.config",
            options=test.option_count,
            difficulty=t(f"difficulty.{test.difficulty.value}"),
            news=test.news_percent,
            passing=test.passing_percent,
        ),
        t(
            "tests.detail.sources",
            s=t("wiz.all_materials")
            if test.use_all_materials and not sources
            else esc(" + ".join(truncate(x, 30) for x in sources) or "—"),
        ),
        t("tests.detail.audience", a=await audience_label(session, test)),
        t("tests.detail.duration", d=fmt_hours(test.duration_seconds / 3600)),
        t("tests.detail.window_open", start=fmt_dt(test.starts_at))
        if open_ended
        else t("tests.detail.window", start=fmt_dt(test.starts_at), deadline=fmt_dt(test.deadline_at)),
        t(
            "tests.detail.flags",
            rq=_yn(test.randomize_questions),
            ro=_yn(test.randomize_options),
            reveal=t(f"reveal.{test.answer_reveal.value}"),
            mode=t(f"tests.mode.{test.delivery_mode.value}"),
        ),
    ]
    if test.focus_query:
        lines.append(t("tests.detail.focus", f=esc(test.focus_query)))
    if test.generation_status == GenerationStatus.RUNNING:
        lines += ["", t("tests.detail.generating")]
    elif test.generation_status == GenerationStatus.FAILED and test.generation_error:
        lines += ["", t("tests.detail.generation_failed", error=esc(_friendly_error(test.generation_error)))]
    if test.status == TestStatus.READY and test.published_at:
        lines += ["", t("tests.detail.scheduled", start=fmt_dt(test.starts_at))]
    if test.paused:
        lines += ["", t("tests.detail.paused")]

    rows = []
    if test.status == TestStatus.DRAFT:
        if total:
            rows.append([(t("tests.btn.preview", n=total), AdminCB(s="tst_rv", id=test.id, p=1))])
        if total < test.question_count and test.generation_status != GenerationStatus.RUNNING:
            rows.append([(t("tests.btn.fill", n=test.question_count - total), AdminCB(s="tst_gen", id=test.id))])
            if total:
                rows.append([(t("tests.btn.shrink", n=total), AdminCB(s="tst_shrink", id=test.id))])
        if total and approved < total:
            rows.append([(t("tests.btn.approve_all"), AdminCB(s="tst_apall", id=test.id))])
        if total == test.question_count and approved == total:
            rows.append([(t("tests.btn.ready"), AdminCB(s="tst_ready", id=test.id))])
        rows.append([(t("tests.btn.duration"), AdminCB(s="tst_dur", id=test.id))])
        rows.append([(t("tests.btn.delete"), AdminCB(s="tst_del", id=test.id))])
    elif test.status == TestStatus.READY:
        if test.published_at is None:
            rows.append([(t("tests.btn.start_in_group"), AdminCB(s="tst_grp", id=test.id))])
            rows.append([(t("tests.btn.publish"), AdminCB(s="tst_pub", id=test.id))])
            rows.append(
                [
                    (t("tests.btn.preview", n=total), AdminCB(s="tst_rv", id=test.id, p=1)),
                    (t("tests.btn.to_draft"), AdminCB(s="tst_draft", id=test.id)),
                ]
            )
            rows.append([(t("tests.btn.duration"), AdminCB(s="tst_dur", id=test.id))])
            rows.append([(t("tests.btn.delete"), AdminCB(s="tst_del", id=test.id))])
        else:
            rows.append(
                [
                    (t("tests.btn.duration"), AdminCB(s="tst_dur", id=test.id)),
                    (t("tests.btn.close"), AdminCB(s="tst_close", id=test.id)),
                ]
            )
    else:
        rows.append([(t("menu.results"), AdminCB(s="res_t", id=test.id))])
        rows.append([(t("tests.btn.participants"), AdminCB(s="tst_part", id=test.id, v="all"))])
        rows.append(
            [
                (t("tests.btn.not_participated"), AdminCB(s="tst_part", id=test.id, v="none")),
                (t("tests.btn.qstats"), AdminCB(s="tst_qs", id=test.id)),
            ]
        )
        rows.append(
            [
                (t("tests.btn.report_xlsx"), AdminCB(s="tst_rep", id=test.id)),
                (t("tests.btn.report_csv"), AdminCB(s="tst_csv", id=test.id)),
            ]
        )
        if test.status == TestStatus.ACTIVE:
            rows.append(
                [
                    (
                        t("tests.btn.resume") if test.paused else t("tests.btn.pause"),
                        AdminCB(s="tst_pause", id=test.id),
                    ),
                    (t("tests.btn.duration"), AdminCB(s="tst_dur", id=test.id)),
                ]
            )
            rows.append([(t("tests.btn.close"), AdminCB(s="tst_close", id=test.id))])
            rows.append([(t("tests.btn.start_in_group"), AdminCB(s="tst_grp", id=test.id))])
            rows.append([(t("tests.btn.announce"), AdminCB(s="tst_ann", id=test.id))])
        else:
            rows.append([(t("tests.btn.reopen"), AdminCB(s="tst_reopen", id=test.id))])
        rows.append([(t("tests.btn.preview", n=total), AdminCB(s="tst_rv", id=test.id, p=1))])
        rows.append([(t("tests.btn.delete"), AdminCB(s="tst_del", id=test.id))])
    rows.append([(t("btn.refresh"), AdminCB(s="tst_v", id=test.id))])
    rows.append(back_menu_row("tst"))
    await show(target, "\n".join(lines), kb(*rows))


def _yn(value: bool) -> str:
    return t("common.yes") if value else t("common.no")


@router.callback_query(AdminCB.filter(F.s == "tst_v"))
async def cb_view(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    await render_test(callback, session, callback_data.id)


# ------------------------------------------------------------------------------ assembly


async def start_assembly(
    target: CallbackQuery,
    session_maker: async_sessionmaker[AsyncSession],
    bot: Bot,
    test_id: int,
    force_generate: bool = False,
) -> None:
    assert isinstance(target.message, Message)
    status_msg = await target.message.answer(t("tests.assembly_started"))
    background.spawn(
        _assemble_and_report(session_maker, bot, test_id, status_msg.chat.id, status_msg.message_id, force_generate),
        name=f"assemble-test-{test_id}",
    )


async def _assemble_and_report(
    session_maker: async_sessionmaker[AsyncSession],
    bot: Bot,
    test_id: int,
    chat_id: int,
    message_id: int,
    force_generate: bool,
) -> None:
    async def progress(stage: str, done: int, total: int) -> None:
        try:
            await bot.edit_message_text(
                t("tests.assembly_progress", done=done, total=total), chat_id=chat_id, message_id=message_id
            )
        except TelegramBadRequest:
            pass

    from app.ai import usage as ai_usage
    from app.handlers.admin.materials import usage_text

    with ai_usage.track() as spent:
        report = await tb.assemble_questions(
            session_maker, test_id, make_generator if ai_available() else None, progress, force_generate=force_generate
        )
    if report.error == "busy_or_not_draft":
        text = t("tests.assembly_busy")
    else:
        text = t(
            "tests.assembly_done",
            bank=report.added_from_bank,
            generated=report.generated,
            material=report.material_questions,
            news=report.news_questions,
            missing=report.missing,
        )
        if report.missing and report.error:
            text += "\n" + t("gen.error_note", error=esc(_friendly_error(report.error)))
        if report.rejected:
            text += "\n" + t("tests.assembly_rejected", n=sum(report.rejected.values()))
        if spent.requests or spent.cache_hits:
            text += "\n" + usage_text(spent)
    rows = [
        [(t("tests.btn.open"), AdminCB(s="tst_v", id=test_id))],
        [(t("tests.btn.preview_short"), AdminCB(s="tst_rv", id=test_id, p=1))],
    ]
    if report.missing and report.error != "busy_or_not_draft":
        rows.insert(0, [(t("tests.btn.shrink_short"), AdminCB(s="tst_shrink", id=test_id))])
    markup = kb(*rows)
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
    except TelegramBadRequest:
        await bot.send_message(chat_id, text, reply_markup=markup)


@router.callback_query(AdminCB.filter(F.s == "tst_gen"))
async def cb_fill(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, session_maker, bot: Bot
) -> None:
    test = await session.get(Test, callback_data.id)
    if test is None or test.status != TestStatus.DRAFT:
        await callback.answer(t("test_error.not_draft"), show_alert=True)
        return
    if test.generation_status == GenerationStatus.RUNNING:
        await callback.answer(t("tests.assembly_busy"), show_alert=True)
        return
    from app.handlers.admin.create_test import cb_resume, is_mixed_draft

    if await is_mixed_draft(session, test.id):  # made with ➕ Test yaratish: continue the mixed build
        await cb_resume(callback, callback_data, session, session_maker, bot)
        return
    await callback.answer()
    await start_assembly(callback, session_maker, bot, test.id)


# ------------------------------------------------------------------------------ review


async def render_review(target, session: AsyncSession, test_id: int, position: int) -> None:
    test = await session.get(Test, test_id)
    if test is None:
        await show(target, t("common.not_found"), kb(back_menu_row("tst")))
        return
    total, approved = await tb.question_counts(session, test_id)
    if total == 0:
        await show(target, t("tests.review.empty"), kb(back_menu_row("tst_v", id_=test_id)))
        return
    position = min(max(1, position), total)
    tq = await tb.get_test_question(session, test_id, position)
    if tq is None:
        await show(target, t("common.not_found"), kb(back_menu_row("tst_v", id_=test_id)))
        return
    options = "\n".join(
        f"{'✅' if letter == tq.correct_option else '▫️'} <b>{letter})</b> {esc(text)}"
        for letter, text in tq.options.items()
    )
    text = t(
        "tests.review.item",
        n=position,
        total=total,
        approved=approved,
        state=t("tests.review.approved") if tq.is_approved else t("tests.review.pending"),
        question=esc(tq.question_text),
        options=options,
        explanation=esc(truncate(tq.explanation, 600)),
        topic=esc(tq.topic_name or "—"),
        difficulty=t(f"difficulty.{tq.difficulty.value}"),
        kind=t(f"source_kind.{tq.source_kind.value}"),
        source=esc(truncate(tq.source_reference, 200)),
        excerpt=esc(truncate(tq.source_excerpt, 700)),
    )
    rows = []
    if test.status == TestStatus.DRAFT:
        action_row = []
        if not tq.is_approved:
            action_row.append((t("tests.btn.approve"), AdminCB(s="tst_ap", id=tq.id, p=position)))
        action_row.append((t("tests.btn.reject"), AdminCB(s="tst_rj", id=tq.id, p=position)))
        action_row.append((t("tests.btn.regenerate"), AdminCB(s="tst_rg", id=tq.id, p=position)))
        rows.append(action_row)
    nav = []
    nav.append(("◀️", AdminCB(s="tst_rv", id=test_id, p=position - 1)) if position > 1 else (" ", AdminCB(s="noop")))
    nav.append((f"{position}/{total}", AdminCB(s="noop")))
    nav.append(("▶️", AdminCB(s="tst_rv", id=test_id, p=position + 1)) if position < total else (" ", AdminCB(s="noop")))
    rows.append(nav)
    if test.status == TestStatus.DRAFT and approved < total:
        rows.append([(t("tests.btn.approve_all"), AdminCB(s="tst_apall", id=test_id))])
    rows.append(back_menu_row("tst_v", id_=test_id))
    await show(target, text, kb(*rows))


@router.callback_query(AdminCB.filter(F.s == "tst_rv"))
async def cb_review(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    await render_review(callback, session, callback_data.id, callback_data.p)


@router.callback_query(AdminCB.filter(F.s == "tst_ap"))
async def cb_approve(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User) -> None:
    try:
        tq = await tb.approve_test_question(session, callback_data.id, user.id)
    except TestStateError as exc:
        await callback.answer(t(f"test_error.{exc.code}"), show_alert=True)
        return
    await callback.answer(t("tests.review.approved_toast"))
    total, _ = await tb.question_counts(session, tq.test_id)
    await render_review(callback, session, tq.test_id, min(callback_data.p + 1, total))


@router.callback_query(AdminCB.filter(F.s == "tst_rj"))
async def cb_reject(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User) -> None:
    try:
        test_id = await tb.reject_test_question(session, callback_data.id, user.id)
    except TestStateError as exc:
        await callback.answer(t(f"test_error.{exc.code}"), show_alert=True)
        return
    await callback.answer(t("tests.review.rejected_toast"))
    await render_review(callback, session, test_id, callback_data.p)


@router.callback_query(AdminCB.filter(F.s == "tst_rg"))
async def cb_regenerate(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, session_maker, bot: Bot, user: User
) -> None:
    if not ai_available():
        await callback.answer(t("ai.not_configured"), show_alert=True)
        return
    try:
        test_id = await tb.reject_test_question(session, callback_data.id, user.id)
    except TestStateError as exc:
        await callback.answer(t(f"test_error.{exc.code}"), show_alert=True)
        return
    await callback.answer(t("tests.review.regenerating"))
    await start_assembly(callback, session_maker, bot, test_id, force_generate=True)


@router.callback_query(AdminCB.filter(F.s == "tst_apall"))
async def cb_approve_all(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User) -> None:
    try:
        n = await tb.approve_all(session, callback_data.id, user.id)
    except TestStateError as exc:
        await callback.answer(t(f"test_error.{exc.code}"), show_alert=True)
        return
    await callback.answer(t("tests.review.approved_all", n=n))
    await render_test(callback, session, callback_data.id)


# ------------------------------------------------------------------------------ lifecycle


@router.callback_query(AdminCB.filter(F.s == "tst_shrink"))
async def cb_shrink(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    try:
        test = await tb.shrink_to_available(session, callback_data.id)
    except TestStateError as exc:
        await callback.answer(t(f"test_error.{exc.code}"), show_alert=True)
        return
    await callback.answer(t("tests.shrunk", n=test.question_count))
    await render_test(callback, session, test.id)


@router.callback_query(AdminCB.filter(F.s == "tst_ready"))
async def cb_ready(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    try:
        await tb.mark_ready(session, callback_data.id)
    except TestStateError as exc:
        await callback.answer(t(f"test_error.{exc.code}"), show_alert=True)
        return
    await callback.answer(t("tests.ready_toast"))
    await render_test(callback, session, callback_data.id)


@router.callback_query(AdminCB.filter(F.s == "tst_draft"))
async def cb_to_draft(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    try:
        await tb.back_to_draft(session, callback_data.id)
    except TestStateError as exc:
        await callback.answer(t(f"test_error.{exc.code}"), show_alert=True)
        return
    await callback.answer()
    await render_test(callback, session, callback_data.id)


@router.callback_query(AdminCB.filter(F.s == "tst_pub"))
async def cb_publish(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    test = await session.get(Test, callback_data.id)
    if test is None:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    await show(
        callback,
        t(
            "tests.publish_confirm",
            title=esc(test.title),
            start=fmt_dt(test.starts_at),
            deadline=fmt_dt(test.deadline_at),
        ),
        confirm_kb(AdminCB(s="tst_pub_ok", id=test.id), AdminCB(s="tst_v", id=test.id)),
    )


@router.callback_query(AdminCB.filter(F.s == "tst_pub_ok"))
async def cb_publish_ok(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, session_maker, bot: Bot
) -> None:
    draft_test = await session.get(Test, callback_data.id)
    if draft_test is not None and draft_test.status == TestStatus.READY and draft_test.published_at is None:
        draft_test.delivery_mode = DeliveryMode.PRIVATE
        await session.commit()
    try:
        test = await tb.publish(session, callback_data.id)
    except TestStateError as exc:
        await callback.answer(t(f"test_error.{exc.code}"), show_alert=True)
        return
    await callback.answer(t("tests.published_toast"))
    if test.status == TestStatus.ACTIVE:
        background.spawn(_announce_now(session_maker, bot, test.id), name=f"announce-{test.id}")
    await render_test(callback, session, test.id)


async def _announce_now(session_maker: async_sessionmaker[AsyncSession], bot: Bot, test_id: int) -> None:
    await launch_test(bot, session_maker, test_id)


@router.callback_query(AdminCB.filter(F.s == "tst_grp"))
async def cb_start_in_group(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    test = await session.get(Test, callback_data.id)
    if test is None or test.status not in (TestStatus.READY, TestStatus.ACTIVE):
        await callback.answer(t("test_error.not_ready"), show_alert=True)
        return
    groups = await group_service.list_groups(session, active_only=True)
    if not groups:
        await callback.answer(t("tests.no_groups"), show_alert=True)
        return
    rows = [
        [(f"👥 {truncate(g.title, 40)} ({n})", AdminCB(s="tst_grp_ok", id=test.id, v=str(g.id)))] for g, n in groups
    ]
    await show(callback, t("tests.choose_group", title=esc(test.title)), kb(*rows, back_menu_row("tst_v", id_=test.id)))


@router.callback_query(AdminCB.filter(F.s == "tst_grp_ok"))
async def cb_start_in_group_confirm(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    test = await session.get(Test, callback_data.id)
    group = await session.get(Group, int(callback_data.v or 0))
    if test is None or group is None:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    await show(
        callback,
        t("tests.group_start_confirm", title=esc(test.title), group=esc(group.title), n=test.question_count,
          start=fmt_dt(test.starts_at), deadline=fmt_dt(test.deadline_at)),
        confirm_kb(AdminCB(s="tst_grp_go", id=test.id, v=str(group.id)), AdminCB(s="tst_v", id=test.id)),
    )  # fmt: skip


@router.callback_query(AdminCB.filter(F.s == "tst_grp_go"))
async def cb_start_in_group_go(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, session_maker, bot: Bot
) -> None:
    test = await session.get(Test, callback_data.id)
    group = await session.get(Group, int(callback_data.v or 0))
    if test is None or group is None or not group.is_active:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    assert isinstance(callback.message, Message)
    admin_chat = callback.message.chat.id
    if test.status == TestStatus.ACTIVE:
        # Already running (e.g. announced in private chats): additionally post it into this group.
        if test.deadline_at <= utcnow():
            await callback.answer(t("test_error.deadline_passed"), show_alert=True)
            return
        await callback.answer(t("tests.group_starting"))
        background.spawn(
            group_tests.post_to_group_and_report(bot, session_maker, test.id, group.id, admin_chat),
            name=f"group-post-{test.id}-{group.id}",
        )
        await render_test(callback, session, test.id)
        return
    if test.status != TestStatus.READY or test.published_at is not None:
        await callback.answer(t("test_error.not_ready"), show_alert=True)
        return
    test.group_id = group.id
    test.delivery_mode = DeliveryMode.GROUP
    await session.commit()
    try:
        test = await tb.publish(session, test.id)
    except TestStateError as exc:
        await callback.answer(t(f"test_error.{exc.code}"), show_alert=True)
        return
    if test.status == TestStatus.ACTIVE and await claim_marker(session, test.id, "announced_at"):
        await callback.answer(t("tests.group_starting"))
        background.spawn(
            group_tests.post_to_group_and_report(bot, session_maker, test.id, group.id, admin_chat),
            name=f"group-test-{test.id}",
        )
    elif test.status != TestStatus.ACTIVE:
        await callback.answer(t("tests.group_scheduled", start=fmt_dt(test.starts_at)), show_alert=True)
    else:
        await callback.answer()
    await render_test(callback, session, test.id)


@router.callback_query(AdminCB.filter(F.s == "tst_ann"))
async def cb_reannounce(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, bot: Bot) -> None:
    test = await session.get(Test, callback_data.id)
    if test is None or test.status != TestStatus.ACTIVE:
        await callback.answer(t("test_error.not_active"), show_alert=True)
        return
    await callback.answer(t("tests.announcing"))
    stats = await announce_test(bot, session, test, groups_only=True)
    assert isinstance(callback.message, Message)
    await callback.message.answer(t("tests.announced", groups=stats["groups"]))


@router.callback_query(AdminCB.filter(F.s == "tst_close"))
async def cb_close(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    test = await session.get(Test, callback_data.id)
    if test is None:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    await show(
        callback,
        t("tests.close_confirm", title=esc(test.title)),
        confirm_kb(AdminCB(s="tst_close_ok", id=test.id), AdminCB(s="tst_v", id=test.id)),
    )


@router.callback_query(AdminCB.filter(F.s == "tst_close_ok"))
async def cb_close_ok(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    try:
        await tb.close_test(session, callback_data.id)
    except TestStateError as exc:
        await callback.answer(t(f"test_error.{exc.code}"), show_alert=True)
        return
    await callback.answer(t("tests.closed_toast"))
    await render_test(callback, session, callback_data.id)


@router.callback_query(AdminCB.filter(F.s == "tst_ext"))
async def cb_extend(callback: CallbackQuery, callback_data: AdminCB, state: FSMContext, session: AsyncSession) -> None:
    test = await session.get(Test, callback_data.id)
    if test is None:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    await state.set_state(TestStates.extend)
    await state.update_data(test_id=test.id)
    await show(callback, t("tests.extend_prompt", deadline=fmt_dt(test.deadline_at)), cancel_kb())


@router.message(TestStates.extend, F.text)
async def on_extend(message: Message, state: FSMContext, session: AsyncSession) -> None:
    from datetime import timedelta

    data = await state.get_data()
    test = await session.get(Test, int(data.get("test_id", 0)))
    if test is None:
        await state.clear()
        await message.answer(t("common.not_found"))
        return
    value = (message.text or "").strip()
    new_deadline = parse_local_datetime(value)
    if new_deadline is None:
        try:
            hours = float(value.replace(",", "."))
        except ValueError:
            await message.answer(t("wiz.err.duration"), reply_markup=cancel_kb())
            return
        if not 0 < hours <= 24 * 30:
            await message.answer(t("wiz.err.duration"), reply_markup=cancel_kb())
            return
        new_deadline = test.deadline_at + timedelta(hours=hours)
    try:
        await tb.extend_deadline(session, test.id, new_deadline)
    except TestStateError as exc:
        await message.answer(t(f"test_error.{exc.code}"), reply_markup=cancel_kb())
        return
    await state.clear()
    await message.answer(t("tests.extended", deadline=fmt_dt(new_deadline)))
    await render_test(message, session, test.id)


@router.callback_query(AdminCB.filter(F.s == "tst_del"))
async def cb_delete(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    test = await session.get(Test, callback_data.id)
    if test is None:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    key = "tests.delete_confirm" if test.published_at is None else "tests.delete_confirm_results"
    await show(
        callback,
        t(key, title=esc(test.title)),
        confirm_kb(AdminCB(s="tst_del_ok", id=test.id), AdminCB(s="tst_v", id=test.id)),
    )


@router.callback_query(AdminCB.filter(F.s == "tst_del_ok"))
async def cb_delete_ok(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, state: FSMContext
) -> None:
    try:
        await tb.delete_test(session, callback_data.id)
    except TestStateError as exc:
        await callback.answer(t(f"test_error.{exc.code}"), show_alert=True)
        return
    await callback.answer(t("common.deleted"))
    await cb_list(callback, AdminCB(s="tst"), session, state)


# ------------------------------------------------------------------------------ results


@router.callback_query(AdminCB.filter(F.s == "tst_part"))
async def cb_participants(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    test = await session.get(Test, callback_data.id)
    if test is None:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    summary = await participation(session, test, utcnow())
    flt = callback_data.v if callback_data.v in PART_FILTERS else "all"
    status = PART_FILTERS[flt]
    rows_data = summary.rows if status is None else summary.by_status(status)
    page = max(0, callback_data.p)
    chunk = rows_data[page * PARTICIPANT_PAGE : (page + 1) * PARTICIPANT_PAGE]

    lines = [
        t("tests.part.header", id=test.id, title=esc(test.title)),
        t(
            "tests.part.summary",
            total=summary.total,
            done=summary.count(ParticipationStatus.COMPLETED),
            prog=summary.count(ParticipationStatus.IN_PROGRESS),
            exp=summary.count(ParticipationStatus.EXPIRED),
            canc=summary.count(ParticipationStatus.CANCELLED),
            none=summary.count(ParticipationStatus.NOT_STARTED),
            rate=pct(summary.participation_rate),
            avg=pct(summary.average_score),
            passed=summary.passed,
        ),
        "",
        t(f"tests.part.filter.{flt}") + f" ({len(rows_data)})",
    ]
    for r in chunk:
        a = r.attempt
        if a is None:
            lines.append(f"{PART_ICON[r.status]} {user_line(r.user)}")
            continue
        detail = t(
            "tests.part.row",
            score=pct(a.score_percent) if a.status != AttemptStatus.IN_PROGRESS else "—",
            correct=a.correct_count,
            wrong=a.incorrect_count,
            answered=a.answered_count,
            total=a.total_questions,
            time=fmt_duration(a.duration_seconds) if a.duration_seconds is not None else "—",
            started=fmt_dt(a.started_at, with_year=False),
            completed=fmt_dt(a.completed_at, with_year=False) if a.completed_at else "—",
        )
        lines.append(f"{PART_ICON[r.status]} <b>{user_line(r.user)}</b>\n    {detail}")
    if not chunk:
        lines.append(t("common.empty"))

    filter_row1 = [(t(f"tests.part.btn.{k}"), AdminCB(s="tst_part", id=test.id, v=k)) for k in ("all", "done", "prog")]
    filter_row2 = [(t(f"tests.part.btn.{k}"), AdminCB(s="tst_part", id=test.id, v=k)) for k in ("exp", "none", "canc")]
    user_rows = [
        [
            (
                f"{PART_ICON[r.status]} {truncate(r.user.display_name, 30)}",
                AdminCB(s="tst_pu", id=r.attempt.id, v=str(test.id)),
            )
        ]
        for r in chunk
        if r.attempt is not None
    ]
    await show(
        callback,
        "\n".join(lines),
        kb(
            filter_row1,
            filter_row2,
            *user_rows,
            pager("tst_part", page, len(rows_data), PARTICIPANT_PAGE, id_=test.id, v=flt),
            back_menu_row("tst_v", id_=test.id),
        ),
    )


@router.callback_query(AdminCB.filter(F.s == "tst_pu"))
async def cb_participant_attempt(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    from app.handlers.admin.results import render_attempt

    await render_attempt(callback, session, callback_data.id)


# ------------------------------------------------------------------------------ management


@router.callback_query(AdminCB.filter(F.s == "tst_dur"))
async def cb_duration(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    test = await session.get(Test, callback_data.id)
    if test is None:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    rows = [
        [
            (
                (("• " if s == test.duration_seconds else "") + fmt_hours(s / 3600)),
                AdminCB(s="tst_dur_go", id=test.id, v=str(s)),
            )
        ]
        for s in DURATION_CHOICES
    ]
    await show(callback, t("tests.duration_prompt", d=fmt_hours(test.duration_seconds / 3600)),
               kb(*rows, back_menu_row("tst_v", id_=test.id)))  # fmt: skip


@router.callback_query(AdminCB.filter(F.s == "tst_dur_go"))
async def cb_duration_go(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    try:
        adjusted = await tb.set_duration(session, callback_data.id, int(callback_data.v or 0))
    except TestStateError as exc:
        await callback.answer(t(f"test_error.{exc.code}"), show_alert=True)
        return
    await callback.answer(t("tests.duration_done", n=adjusted), show_alert=True)
    await render_test(callback, session, callback_data.id)


@router.callback_query(AdminCB.filter(F.s == "tst_pause"))
async def cb_pause(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    test = await session.get(Test, callback_data.id)
    if test is None:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    try:
        await tb.set_paused(session, test.id, not test.paused)
    except TestStateError as exc:
        await callback.answer(t(f"test_error.{exc.code}"), show_alert=True)
        return
    await callback.answer(t("tests.paused_toast") if test.paused else t("tests.resumed_toast"), show_alert=True)
    await render_test(callback, session, test.id)


@router.callback_query(AdminCB.filter(F.s == "tst_reopen"))
async def cb_reopen(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    test = await session.get(Test, callback_data.id)
    if test is None:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    await show(
        callback,
        t("tests.reopen_confirm", title=esc(test.title)),
        confirm_kb(AdminCB(s="tst_reopen_ok", id=test.id), AdminCB(s="tst_v", id=test.id)),
    )


@router.callback_query(AdminCB.filter(F.s == "tst_reopen_ok"))
async def cb_reopen_ok(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    try:
        await tb.reopen_test(session, callback_data.id)
    except TestStateError as exc:
        await callback.answer(t(f"test_error.{exc.code}"), show_alert=True)
        return
    await callback.answer(t("tests.reopened_toast"), show_alert=True)
    await render_test(callback, session, callback_data.id)


@router.callback_query(AdminCB.filter(F.s == "tst_qs"))
async def cb_question_stats(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    test = await session.get(Test, callback_data.id)
    if test is None:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    items = await group_tests.answers_by_question(session, test.id)
    lines = [t("tests.qstats.title", title=esc(test.title)), ""]
    for n, item in enumerate(items, start=1):
        q = item.question
        total = len(item.answers)
        correct = sum(1 for _, a in item.answers if a.is_correct)
        counts: dict[str, int] = {}
        for _, a in item.answers:
            counts[a.selected_option] = counts.get(a.selected_option, 0) + 1
        share = pct(correct / total * 100) if total else "—"
        lines.append(f"<b>{n}-savol</b> — {share} ({correct}/{total})\n<i>{esc(truncate(q.question_text, 90))}</i>")
        lines.append(
            "    " + " | ".join(
                f"{'ABCDE'[i]}{'✅' if o == q.correct_option else ''} — {counts.get(o, 0)}" for i, o in enumerate(item.opts)
            )
        )  # fmt: skip
    if not items:
        lines.append(t("common.empty"))
    await show(
        callback,
        "\n".join(lines),
        kb([(t("tests.btn.all_answers"), AdminCB(s="tst_ans", id=test.id, p=1))], back_menu_row("tst_v", id_=test.id)),
    )


@router.callback_query(AdminCB.filter(F.s == "tst_ans"))
async def cb_all_answers(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    """Who chose what: one question per page, every employee's choice."""
    test = await session.get(Test, callback_data.id)
    if test is None:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    data = await group_tests.answers_by_question(session, test.id)
    if not data:
        await show(callback, t("common.empty"), kb(back_menu_row("tst_v", id_=test.id)))
        return
    position = min(max(1, callback_data.p), len(data))
    item = data[position - 1]
    tq = item.question
    lines = [
        t("tests.answers.header", title=esc(test.title), n=position, total=len(data)),
        f"<b>{esc(tq.question_text)}</b>",
        "",
    ]
    for i, original in enumerate(item.opts):
        mark = "✅" if original == tq.correct_option else "▫️"
        lines.append(f"{mark} <b>{'ABCDE'[i]})</b> {esc(truncate(tq.options[original], 120))}")
    lines.append("")
    if not item.answers:
        lines.append(t("tests.answers.none"))
    for user, answer in item.answers:
        # Group tests: everyone saw the same letters. Private tests: options may be shuffled per
        # employee, so the letter of the shared snapshot order is shown.
        sel = answer.selected_display if item.group_mode else item.display_of(answer.selected_option)
        corr = item.display_of(tq.correct_option)
        lines.append(
            t("tests.answers.item", mark="✅" if answer.is_correct else "❌", name=esc(user.display_name),
              sel=sel, corr=corr)
        )  # fmt: skip
    nav = [
        ("◀️", AdminCB(s="tst_ans", id=test.id, p=position - 1)) if position > 1 else (" ", AdminCB(s="noop")),
        (f"{position}/{len(data)}", AdminCB(s="noop")),
        ("▶️", AdminCB(s="tst_ans", id=test.id, p=position + 1)) if position < len(data) else (" ", AdminCB(s="noop")),
    ]
    await show(callback, "\n".join(lines), kb(nav, back_menu_row("tst_qs", id_=test.id)))


@router.callback_query(AdminCB.filter(F.s.in_({"tst_rep", "tst_csv"})))
async def cb_report(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    if await session.get(Test, callback_data.id) is None:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    await callback.answer(t("reports.generating"))
    if callback_data.s == "tst_rep":
        data, filename = await build_test_report_xlsx(session, callback_data.id)
    else:
        data, filename = await build_test_report_csv(session, callback_data.id)
    assert isinstance(callback.message, Message)
    await callback.message.answer_document(BufferedInputFile(data, filename=filename), caption=t("reports.caption"))
