"""➕ Test yaratish: name → files (several) → 🔀 mixed test → count → time → who gets it.

The draft test is stored in the database from the first step and every uploaded file becomes a
``test_sources`` row right away, so several files sent at once (an album) never race each other and a
bot restart loses nothing.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.documents import DocumentError, detect_file_type
from app.handlers.admin.common import show
from app.handlers.admin.materials import _friendly_error, doc_error_text
from app.keyboards.callbacks import AdminCB
from app.keyboards.common import kb
from app.locales import t
from app.models import (
    DURATION_CHOICES,
    AnswerReveal,
    DeliveryMode,
    Group,
    Material,
    MaterialStatus,
    Test,
    TestAudience,
    TestSource,
    TestStatus,
    User,
)
from app.services import assignments, background
from app.services import groups as group_service
from app.services import materials as material_service
from app.services import test_builder as tb
from app.services.ai_runtime import ai_available, make_generator
from app.services.mixed_tests import build_mixed_test
from app.services.scheduler import launch_test
from app.utils.text import esc, sha256_bytes, truncate
from app.utils.time import fmt_hours, utcnow

logger = logging.getLogger(__name__)
router = Router(name="admin_create_test")

COUNTS = (10, 20, 30, 40, 50)
USERS_PAGE = 8
MATERIAL_ICON = {
    MaterialStatus.UPLOADED: "⏳",
    MaterialStatus.PROCESSING: "⏳",
    MaterialStatus.READY: "✅",
    MaterialStatus.FAILED: "❌",
}


class CreateStates(StatesGroup):
    title = State()
    files = State()
    count = State()


def _cancel_row(test_id: int) -> list:
    return [(t("btn.cancel"), AdminCB(s="ct_x", id=test_id))]


# ------------------------------------------------------------------------------ 1. name


@router.callback_query(AdminCB.filter(F.s == "ct"))
async def cb_create(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(CreateStates.title)
    await show(callback, t("ct.ask_title"), kb([(t("btn.cancel"), AdminCB(s="cancel"))]))


@router.message(CreateStates.title, F.text)
async def on_title(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    title = " ".join((message.text or "").split())
    if not 3 <= len(title) <= 200:
        await message.answer(t("wiz.err.title"))
        return
    now = utcnow()
    test = await tb.create_test(
        session,
        tb.TestDraftData(
            title=title,
            question_count=20,
            starts_at=now,
            deadline_at=now + tb.OPEN_WINDOW,
            answer_reveal=AnswerReveal.IMMEDIATE.value,
            randomize_questions=True,
            randomize_options=True,
        ),
        user.id,
    )
    await state.set_state(CreateStates.files)
    await state.update_data(ct_test=test.id)
    await message.answer(
        t("ct.ask_files", mb=get_settings().max_file_size_mb, title=esc(title)),
        reply_markup=kb(_cancel_row(test.id)),
    )


# ------------------------------------------------------------------------------ 2. files


async def _files_text(session: AsyncSession, test_id: int) -> tuple[str, int]:
    rows = (
        await session.execute(
            select(TestSource, Material.status)
            .join(Material, Material.id == TestSource.material_id, isouter=True)
            .where(TestSource.test_id == test_id)
            .order_by(TestSource.position, TestSource.id)
        )
    ).all()
    lines = [t("ct.files_title", n=len(rows)), ""]
    for i, (src, status) in enumerate(rows, start=1):
        icon = MATERIAL_ICON.get(status, "❌")
        lines.append(f"{i}. {icon} <b>{esc(src.source_name)}</b> — {esc(src.original_file_name or '')}")
    lines += ["", t("ct.files_hint")]
    return "\n".join(lines), len(rows)


@router.message(CreateStates.files, F.document)
async def on_file(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    bot: Bot,
    user: User,
) -> None:
    test_id = int((await state.get_data()).get("ct_test", 0))
    test = await session.get(Test, test_id)
    if test is None or test.status != TestStatus.DRAFT:
        await state.clear()
        await message.answer(t("errors.stale_button"))
        return
    document = message.document
    assert document is not None
    settings = get_settings()
    try:
        file_type = detect_file_type(document.file_name, document.mime_type)
    except DocumentError as exc:
        await message.answer(f"«{esc(document.file_name or '')}»: {doc_error_text(exc.code)}")
        return
    if document.file_size and document.file_size > settings.max_file_size_bytes:
        await message.answer(t("doc_error.too_large_file", mb=settings.max_file_size_mb))
        return
    fd, name = tempfile.mkstemp(prefix="material_", suffix=f".{file_type.value}")
    os.close(fd)
    path = Path(name)
    try:
        await bot.download(document.file_id, destination=path)
    except Exception as exc:
        path.unlink(missing_ok=True)
        logger.warning("Telegram file download failed", extra={"error": str(exc)[:200]})
        await message.answer(t("doc_error.download_failed"))
        return
    content_hash = sha256_bytes(path.read_bytes())
    material = await material_service.find_duplicate(session, content_hash)
    if material is not None:  # the same file was uploaded before: reuse its processed text
        path.unlink(missing_ok=True)
        if material.status == MaterialStatus.FAILED:
            material = None
            await message.answer(t("ct.duplicate_failed"))
    if material is None:
        stem = Path(document.file_name or "Hujjat").stem.replace("_", " ").strip()[:200] or "Hujjat"
        material = await material_service.create_material(
            session,
            title=stem,
            file_type=file_type,
            uploaded_by_id=user.id,
            file_name=document.file_name,
            telegram_file_id=document.file_id,
            telegram_file_unique_id=document.file_unique_id,
            file_size=document.file_size,
            content_hash=content_hash,
        )
        if path.exists():
            background.spawn(_process(session_maker, material.id, path), name=f"process-material-{material.id}")
    await tb.ensure_source(session, test_id, material)
    await session.commit()
    text, _ = await _files_text(session, test_id)
    await message.answer(
        text,
        reply_markup=kb(
            [(t("ct.btn.mix"), AdminCB(s="ct_mix", id=test_id))],
            [(t("btn.refresh"), AdminCB(s="ct_files", id=test_id))],
            _cancel_row(test_id),
        ),
    )


async def _process(session_maker: async_sessionmaker[AsyncSession], material_id: int, path: Path) -> None:
    try:
        await material_service.process_material(session_maker, material_id, path)
    finally:
        path.unlink(missing_ok=True)


@router.callback_query(AdminCB.filter(F.s == "ct_files"))
async def cb_files(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    text, _ = await _files_text(session, callback_data.id)
    await show(
        callback,
        text,
        kb(
            [(t("ct.btn.mix"), AdminCB(s="ct_mix", id=callback_data.id))],
            [(t("btn.refresh"), AdminCB(s="ct_files", id=callback_data.id))],
            _cancel_row(callback_data.id),
        ),
    )


@router.message(CreateStates.files)
async def on_not_file(message: Message) -> None:
    await message.answer(t("ct.need_file"))


@router.callback_query(AdminCB.filter(F.s == "ct_x"))
async def cb_cancel(callback: CallbackQuery, callback_data: AdminCB, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    test = await session.get(Test, callback_data.id)
    if test is not None and test.status == TestStatus.DRAFT and test.published_at is None:
        await tb.delete_test(session, test.id)  # uploaded files stay in 📚 Materiallar
    await show(callback, t("common.cancelled"))


# ------------------------------------------------------------------------------ 3. count / time / audience


async def _draft(callback: CallbackQuery, session: AsyncSession, test_id: int) -> Test | None:
    test = await session.get(Test, test_id)
    if test is None or test.status != TestStatus.DRAFT:
        await callback.answer(t("errors.stale_button"), show_alert=True)
        return None
    return test


@router.callback_query(AdminCB.filter(F.s == "ct_mix"))
async def cb_mix(callback: CallbackQuery, callback_data: AdminCB, state: FSMContext, session: AsyncSession) -> None:
    test = await _draft(callback, session, callback_data.id)
    if test is None:
        return
    sources = await tb.source_names(session, test.id)
    if not sources:
        await callback.answer(t("ct.need_file"), show_alert=True)
        return
    await state.set_state(CreateStates.count)
    await state.update_data(ct_test=test.id)
    tid = test.id
    await show(
        callback,
        t("ct.ask_count", sources=esc(" + ".join(sources))),
        kb(
            [(str(n), AdminCB(s="ct_n", id=tid, v=str(n))) for n in COUNTS[:3]],
            [(str(n), AdminCB(s="ct_n", id=tid, v=str(n))) for n in COUNTS[3:]]
            + [(t("ct.btn.custom"), AdminCB(s="ct_nc", id=tid))],
            _cancel_row(tid),
        ),
    )


@router.callback_query(AdminCB.filter(F.s == "ct_nc"))
async def cb_custom_count(callback: CallbackQuery, callback_data: AdminCB, state: FSMContext) -> None:
    await state.set_state(CreateStates.count)
    await state.update_data(ct_test=callback_data.id)
    await show(callback, t("ct.ask_custom_count"), kb(_cancel_row(callback_data.id)))


@router.message(CreateStates.count, F.text)
async def on_custom_count(message: Message, state: FSMContext, session: AsyncSession) -> None:
    value = (message.text or "").strip()
    test_id = int((await state.get_data()).get("ct_test", 0))
    if not value.isdigit() or not 1 <= int(value) <= 100:
        await message.answer(t("wiz.err.count"))
        return
    await _set_count(message, session, test_id, int(value))
    await state.set_state(None)


@router.callback_query(AdminCB.filter(F.s == "ct_n"))
async def cb_count(callback: CallbackQuery, callback_data: AdminCB, state: FSMContext, session: AsyncSession) -> None:
    if await _draft(callback, session, callback_data.id) is None:
        return
    await state.set_state(None)
    await _set_count(callback, session, callback_data.id, int(callback_data.v))


async def _set_count(target, session: AsyncSession, test_id: int, count: int) -> None:
    test = await session.get(Test, test_id)
    if test is None:
        return
    test.question_count = count
    await session.commit()
    await show(
        target,
        t("ct.ask_duration"),
        kb(
            *[[(fmt_hours(s / 3600), AdminCB(s="ct_d", id=test_id, v=str(s)))] for s in DURATION_CHOICES],
            _cancel_row(test_id),
        ),
    )


@router.callback_query(AdminCB.filter(F.s == "ct_d"))
async def cb_duration(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    test = await _draft(callback, session, callback_data.id)
    if test is None or int(callback_data.v or 0) not in DURATION_CHOICES:
        return
    test.duration_seconds = int(callback_data.v)
    await session.commit()
    await show_audience(callback, test.id)


async def show_audience(target, test_id: int) -> None:
    await show(
        target,
        t("ct.ask_audience"),
        kb(
            [(t("ct.aud.all"), AdminCB(s="ct_a", id=test_id, v="all"))],
            [(t("ct.aud.group"), AdminCB(s="ct_a", id=test_id, v="group"))],
            [(t("ct.aud.users"), AdminCB(s="ct_a", id=test_id, v="users"))],
            [(t("ct.aud.one"), AdminCB(s="ct_a", id=test_id, v="one"))],
            _cancel_row(test_id),
        ),
    )


@router.callback_query(AdminCB.filter(F.s == "ct_a"))
async def cb_audience(
    callback: CallbackQuery, callback_data: AdminCB, state: FSMContext, session: AsyncSession
) -> None:
    test = await _draft(callback, session, callback_data.id)
    if test is None:
        return
    choice = callback_data.v
    if choice == "all":
        test.audience, test.group_id, test.delivery_mode = TestAudience.ALL, None, DeliveryMode.PRIVATE
        await session.commit()
        await _confirm(callback, session, test.id)
    elif choice == "group":
        groups = await group_service.list_groups(session, active_only=True)
        if not groups:
            await callback.answer(t("tests.no_groups"), show_alert=True)
            return
        rows = [[(f"👥 {truncate(g.title, 40)} ({n})", AdminCB(s="ct_g", id=test.id, v=str(g.id)))] for g, n in groups]
        await show(callback, t("ct.ask_group"), kb(*rows, _cancel_row(test.id)))
    else:
        await state.update_data(ct_users=[], ct_single=choice == "one")
        await _users_page(callback, state, session, test.id, 0)


@router.callback_query(AdminCB.filter(F.s == "ct_g"))
async def cb_group(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    test = await _draft(callback, session, callback_data.id)
    group = await session.get(Group, int(callback_data.v or 0))
    if test is None or group is None:
        return
    test.audience, test.group_id, test.delivery_mode = TestAudience.GROUP, group.id, DeliveryMode.GROUP
    await session.commit()
    await _confirm(callback, session, test.id)


async def _users_page(target, state: FSMContext, session: AsyncSession, test_id: int, page: int) -> None:
    data = await state.get_data()
    selected = set(data.get("ct_users") or [])
    single = bool(data.get("ct_single"))
    users = await assignments.active_employees(session)
    pages = max(1, (len(users) + USERS_PAGE - 1) // USERS_PAGE)
    page = min(max(0, page), pages - 1)
    chunk = users[page * USERS_PAGE : (page + 1) * USERS_PAGE]
    rows = []
    for u in chunk:
        mark = "👤" if single else ("☑️" if u.id in selected else "▫️")
        rows.append([(f"{mark} {truncate(u.display_name, 40)}", AdminCB(s="ct_u", id=test_id, p=page, v=str(u.id)))])
    nav = []
    if page > 0:
        nav.append(("◀️", AdminCB(s="ct_up", id=test_id, p=page - 1)))
    if pages > 1:
        nav.append((f"{page + 1}/{pages}", AdminCB(s="noop")))
    if page < pages - 1:
        nav.append(("▶️", AdminCB(s="ct_up", id=test_id, p=page + 1)))
    done = [] if single else [(t("ct.btn.users_done", n=len(selected)), AdminCB(s="ct_ud", id=test_id))]
    text = t("ct.ask_one") if single else t("ct.ask_users", n=len(selected))
    if not users:
        text += "\n\n" + t("common.empty")
    await show(target, text, kb(*rows, nav, done, _cancel_row(test_id)))


@router.callback_query(AdminCB.filter(F.s == "ct_up"))
async def cb_users_page(
    callback: CallbackQuery, callback_data: AdminCB, state: FSMContext, session: AsyncSession
) -> None:
    await _users_page(callback, state, session, callback_data.id, callback_data.p)


@router.callback_query(AdminCB.filter(F.s == "ct_u"))
async def cb_user_toggle(
    callback: CallbackQuery, callback_data: AdminCB, state: FSMContext, session: AsyncSession
) -> None:
    data = await state.get_data()
    user_id = int(callback_data.v or 0)
    if data.get("ct_single"):
        await _save_users(callback, session, callback_data.id, [user_id])
        return
    selected = list(data.get("ct_users") or [])
    if user_id in selected:
        selected.remove(user_id)
    else:
        selected.append(user_id)
    await state.update_data(ct_users=selected)
    await _users_page(callback, state, session, callback_data.id, callback_data.p)


@router.callback_query(AdminCB.filter(F.s == "ct_ud"))
async def cb_users_done(
    callback: CallbackQuery, callback_data: AdminCB, state: FSMContext, session: AsyncSession
) -> None:
    selected = list((await state.get_data()).get("ct_users") or [])
    if not selected:
        await callback.answer(t("ct.need_users"), show_alert=True)
        return
    await _save_users(callback, session, callback_data.id, selected)


async def _save_users(callback: CallbackQuery, session: AsyncSession, test_id: int, user_ids: list[int]) -> None:
    test = await _draft(callback, session, test_id)
    if test is None:
        return
    test.audience, test.group_id, test.delivery_mode = TestAudience.USERS, None, DeliveryMode.PRIVATE
    await session.commit()
    from app.models import TestAssignment

    await session.execute(delete(TestAssignment).where(TestAssignment.test_id == test_id))
    await assignments.assign_users(session, test_id, user_ids)
    await _confirm(callback, session, test_id)


async def audience_label(session: AsyncSession, test: Test) -> str:
    if test.audience == TestAudience.GROUP and test.group_id:
        group = await session.get(Group, test.group_id)
        return t("ct.aud.group_named", g=esc(group.title if group else "—"))
    if test.audience == TestAudience.USERS:
        ids = await assignments.assigned_user_ids(session, test.id)
        if len(ids) == 1:
            user = await session.get(User, ids[0])
            return f"👤 {esc(user.display_name if user else '—')}"
        return t("ct.aud.users_n", n=len(ids))
    return t("ct.aud.all")


async def _confirm(target, session: AsyncSession, test_id: int) -> None:
    test = await session.get(Test, test_id)
    assert test is not None
    sources = await tb.source_names(session, test_id)
    text = t(
        "ct.confirm",
        title=esc(test.title),
        sources=esc(" + ".join(sources)),
        n=test.question_count,
        duration=fmt_hours(test.duration_seconds / 3600),
        audience=await audience_label(session, test),
    )
    await show(target, text, kb([(t("ct.btn.create"), AdminCB(s="ct_go", id=test_id))], _cancel_row(test_id)))


# ------------------------------------------------------------------------------ 4. build & send


@router.callback_query(AdminCB.filter(F.s == "ct_go"))
async def cb_go(
    callback: CallbackQuery, callback_data: AdminCB, state: FSMContext, session: AsyncSession, session_maker, bot: Bot
) -> None:
    test = await _draft(callback, session, callback_data.id)
    if test is None:
        return
    await state.clear()
    if not ai_available():
        await callback.answer(t("ai.not_configured"), show_alert=True)
    await show(callback, t("ct.building", n=test.question_count))
    assert isinstance(callback.message, Message)
    background.spawn(
        build_and_send(session_maker, bot, test.id, callback.message.chat.id, callback.message.message_id),
        name=f"mixed-test-{test.id}",
    )


async def build_and_send(
    session_maker: async_sessionmaker[AsyncSession], bot: Bot, test_id: int, chat_id: int, message_id: int
) -> None:
    async def progress(stage: str, done: int, total: int) -> None:
        key = "ct.progress_processing" if stage == "processing" else "ct.progress"
        try:
            await bot.edit_message_text(t(key, done=done, total=total), chat_id=chat_id, message_id=message_id)
        except TelegramBadRequest:
            pass

    report = await build_mixed_test(session_maker, test_id, make_generator if ai_available() else None, progress)
    per_source = "\n".join(f"• {esc(name)} — {n}" for name, n in report.per_source.items())
    if report.failed_sources:
        per_source += "\n" + t("ct.failed_sources", s=esc(", ".join(report.failed_sources)))
    if report.created and report.missing == 0:
        await publish_and_send(session_maker, bot, test_id)
        async with session_maker() as session:
            test = await session.get(Test, test_id)
            label = await audience_label(session, test) if test else ""
        text = t("ct.done", created=report.created, per_source=per_source, audience=label)
        markup = kb([(t("tests.btn.open"), AdminCB(s="tst_v", id=test_id))])
    else:
        reason = esc(_friendly_error(report.error or "not_enough_questions"))
        text = t("ct.partial", created=report.created, requested=report.requested, per_source=per_source or "—",
                 reason=reason)  # fmt: skip
        rows = []
        if report.created:
            rows.append([(t("ct.btn.send_partial", n=report.created), AdminCB(s="ct_part", id=test_id))])
            rows.append([(t("tests.btn.preview_short"), AdminCB(s="tst_rv", id=test_id, p=1))])
        rows.append([(t("tests.btn.open"), AdminCB(s="tst_v", id=test_id))])
        markup = kb(*rows)
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
    except TelegramBadRequest:
        await bot.send_message(chat_id, text, reply_markup=markup)


async def publish_and_send(session_maker: async_sessionmaker[AsyncSession], bot: Bot, test_id: int) -> None:
    """READY → ACTIVE right now → the test appears in 📚 Testlarim of its audience, who are notified."""
    async with session_maker() as session:
        test = await session.get(Test, test_id)
        if test is None:
            return
        if test.status == TestStatus.DRAFT:
            await tb.mark_ready(session, test_id)
        await tb.publish(session, test_id)
    await launch_test(bot, session_maker, test_id)


@router.callback_query(AdminCB.filter(F.s == "ct_part"))
async def cb_send_partial(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, session_maker, bot: Bot
) -> None:
    try:
        await tb.shrink_to_available(session, callback_data.id)
    except tb.TestStateError as exc:
        await callback.answer(t(f"test_error.{exc.code}"), show_alert=True)
        return
    await publish_and_send(session_maker, bot, callback_data.id)
    await callback.answer(t("ct.sent"))
    from app.handlers.admin.tests import render_test

    await render_test(callback, session, callback_data.id)
