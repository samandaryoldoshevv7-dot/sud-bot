"""📚 Materials: upload (file or pasted text), processing status, generation into the question bank."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.base import AIError
from app.config import get_settings
from app.documents import DocumentError, detect_file_type
from app.handlers.admin.common import show
from app.keyboards.callbacks import AdminCB
from app.keyboards.common import back_menu_row, cancel_kb, confirm_kb, kb, pager
from app.locales import t
from app.models import FileType, Material, MaterialStatus, User
from app.rag.vector_store import SourceScope
from app.services import background, settings_service
from app.services import materials as material_service
from app.services.ai_runtime import ai_available, make_generator
from app.services.question_generation import GenerationRequest
from app.utils.text import esc, sha256_bytes, truncate
from app.utils.time import fmt_dt

logger = logging.getLogger(__name__)
router = Router(name="admin_materials")

STATUS_ICON = {
    MaterialStatus.UPLOADED: "📥",
    MaterialStatus.PROCESSING: "⏳",
    MaterialStatus.READY: "✅",
    MaterialStatus.FAILED: "❌",
}


class MaterialStates(StatesGroup):
    source = State()
    title = State()
    category = State()
    description = State()


def doc_error_text(code: str) -> str:
    return t(f"doc_error.{code}") if code else t("doc_error.internal")


@router.callback_query(AdminCB.filter(F.s == "mat"))
async def cb_list(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    page = max(0, callback_data.p)
    items, total = await material_service.list_materials(session, page, include_archived=True)
    lines = [t("mat.section", total=total), ""]
    rows = []
    if not items:
        lines.append(t("mat.empty"))
    for material, qcount in items:
        icon = STATUS_ICON[material.status] if material.is_active else "🗄"
        rows.append([(f"{icon} {truncate(material.title, 38)} ({qcount})", AdminCB(s="mat_v", id=material.id, p=page))])
    await show(
        callback,
        "\n".join(lines),
        kb(
            [(t("mat.btn.upload"), AdminCB(s="mat_up"))],
            *rows,
            pager("mat", page, total, material_service.PAGE_SIZE),
            back_menu_row("menu"),
        ),
    )


@router.callback_query(AdminCB.filter(F.s == "mat_up"))
async def cb_upload(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(MaterialStates.source)
    await show(callback, t("mat.upload_prompt", mb=get_settings().max_file_size_mb), cancel_kb())


@router.message(MaterialStates.source, F.document)
async def on_document(message: Message, state: FSMContext) -> None:
    document = message.document
    assert document is not None
    settings = get_settings()
    try:
        file_type = detect_file_type(document.file_name, document.mime_type)
    except DocumentError as exc:
        await message.answer(doc_error_text(exc.code), reply_markup=cancel_kb())
        return
    if document.file_size and document.file_size > settings.max_file_size_bytes:
        await message.answer(t("doc_error.too_large_file", mb=settings.max_file_size_mb), reply_markup=cancel_kb())
        return
    default_title = Path(document.file_name or "Hujjat").stem.replace("_", " ").strip()[:200] or "Hujjat"
    await state.update_data(
        kind="file",
        file_id=document.file_id,
        file_unique_id=document.file_unique_id,
        file_name=document.file_name,
        file_size=document.file_size,
        file_type=file_type.value,
        default_title=default_title,
    )
    await state.set_state(MaterialStates.title)
    await message.answer(
        t("mat.ask_title", default=esc(default_title)),
        reply_markup=kb(
            [(t("mat.btn.use_filename"), AdminCB(s="mat_t_def"))], [(t("btn.cancel"), AdminCB(s="cancel"))]
        ),
    )


@router.message(MaterialStates.source, F.text)
async def on_text(message: Message, state: FSMContext) -> None:
    text = message.text or ""
    if text.startswith("/"):
        return
    data = await state.get_data()
    collected = (data.get("text") or "") + ("\n\n" if data.get("text") else "") + text
    if len(collected) > 200_000:
        await message.answer(t("doc_error.too_large_text"), reply_markup=cancel_kb())
        return
    await state.update_data(kind="text", text=collected, file_type=FileType.TEXT.value)
    # Long texts arrive split into several Telegram messages: let the admin send more parts.
    await message.answer(
        t("mat.text_received", n=len(collected)),
        reply_markup=kb([(t("mat.btn.text_done"), AdminCB(s="mat_txt_ok"))], [(t("btn.cancel"), AdminCB(s="cancel"))]),
    )


@router.callback_query(AdminCB.filter(F.s == "mat_txt_ok"), MaterialStates.source)
async def cb_text_done(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    text = (data.get("text") or "").strip()
    if len(text) < 200:
        await callback.answer(t("mat.text_too_short"), show_alert=True)
        return
    first_line = text.split("\n", 1)[0].strip()[:120] or "Matn"
    await state.update_data(default_title=first_line)
    await state.set_state(MaterialStates.title)
    await show(
        callback,
        t("mat.ask_title", default=esc(first_line)),
        kb([(t("mat.btn.use_filename"), AdminCB(s="mat_t_def"))], [(t("btn.cancel"), AdminCB(s="cancel"))]),
    )


@router.message(MaterialStates.source)
async def on_bad_source(message: Message) -> None:
    await message.answer(t("mat.bad_source"), reply_markup=cancel_kb())


async def _ask_category(target, session: AsyncSession, state: FSMContext) -> None:
    await state.set_state(MaterialStates.category)
    result = await session.execute(select(Material.category).where(Material.category.is_not(None)).distinct().limit(8))
    categories = [c for c in result.scalars() if c]
    # Callback data is limited to 64 bytes, so buttons carry an index into the list kept in FSM data.
    await state.update_data(category_options=categories)
    rows = [[(truncate(c, 30), AdminCB(s="mat_cat", v=str(i)))] for i, c in enumerate(categories)]
    rows.append([(t("btn.skip"), AdminCB(s="mat_cat", v="")), (t("btn.cancel"), AdminCB(s="cancel"))])
    await show(target, t("mat.ask_category"), kb(*rows))


@router.callback_query(AdminCB.filter(F.s == "mat_t_def"), MaterialStates.title)
async def cb_title_default(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    await state.update_data(title=data.get("default_title") or "Hujjat")
    await _ask_category(callback, session, state)


@router.message(MaterialStates.title, F.text)
async def on_title(message: Message, state: FSMContext, session: AsyncSession) -> None:
    title = " ".join((message.text or "").split())
    if not 3 <= len(title) <= 255:
        await message.answer(t("mat.bad_title"), reply_markup=cancel_kb())
        return
    await state.update_data(title=title)
    await _ask_category(message, session, state)


async def _ask_description(target, state: FSMContext) -> None:
    await state.set_state(MaterialStates.description)
    await show(
        target,
        t("mat.ask_description"),
        kb([(t("btn.skip"), AdminCB(s="mat_desc_skip")), (t("btn.cancel"), AdminCB(s="cancel"))]),
    )


@router.callback_query(AdminCB.filter(F.s == "mat_cat"), MaterialStates.category)
async def cb_category(callback: CallbackQuery, callback_data: AdminCB, state: FSMContext) -> None:
    options = (await state.get_data()).get("category_options") or []
    index = int(callback_data.v) if callback_data.v.isdigit() else -1
    await state.update_data(category=options[index] if 0 <= index < len(options) else None)
    await _ask_description(callback, state)


@router.message(MaterialStates.category, F.text)
async def on_category(message: Message, state: FSMContext) -> None:
    await state.update_data(category=(message.text or "").strip()[:128])
    await _ask_description(message, state)


@router.callback_query(AdminCB.filter(F.s == "mat_desc_skip"), MaterialStates.description)
async def cb_description_skip(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, session_maker, bot: Bot, user: User
) -> None:
    await callback.answer()
    await _finish_upload(callback.message, state, session, session_maker, bot, user, None)  # type: ignore[arg-type]


@router.message(MaterialStates.description, F.text)
async def on_description(
    message: Message, state: FSMContext, session: AsyncSession, session_maker, bot: Bot, user: User
) -> None:
    await _finish_upload(message, state, session, session_maker, bot, user, (message.text or "").strip()[:2000])


async def _finish_upload(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    bot: Bot,
    user: User,
    description: str | None,
) -> None:
    data = await state.get_data()
    await state.clear()
    file_type = FileType(data["file_type"])
    status_msg = await message.answer(t("mat.processing_started"))
    tmp_path: Path | None = None
    if data.get("kind") == "file":
        fd, name = tempfile.mkstemp(prefix="material_", suffix=f".{file_type.value}")
        os.close(fd)
        tmp_path = Path(name)
        try:
            await bot.download(data["file_id"], destination=tmp_path)
        except Exception as exc:
            tmp_path.unlink(missing_ok=True)
            logger.warning("Telegram file download failed", extra={"error": str(exc)[:200]})
            await status_msg.edit_text(t("doc_error.download_failed"))
            return
        if tmp_path.stat().st_size > get_settings().max_file_size_bytes:
            tmp_path.unlink(missing_ok=True)
            await status_msg.edit_text(t("doc_error.too_large_file", mb=get_settings().max_file_size_mb))
            return
        content_hash = sha256_bytes(tmp_path.read_bytes())
        raw_text = None
    else:
        raw_text = data.get("text") or ""
        content_hash = sha256_bytes(raw_text.encode("utf-8"))
    duplicate = await material_service.find_duplicate(session, content_hash)
    if duplicate is not None:
        if tmp_path:
            tmp_path.unlink(missing_ok=True)
        await status_msg.edit_text(
            t("mat.duplicate", title=esc(duplicate.title)),
            reply_markup=kb([(t("mat.btn.open"), AdminCB(s="mat_v", id=duplicate.id))]),
        )
        return
    material = await material_service.create_material(
        session,
        title=data.get("title") or data.get("default_title") or "Hujjat",
        file_type=file_type,
        uploaded_by_id=user.id,
        file_name=data.get("file_name"),
        telegram_file_id=data.get("file_id"),
        telegram_file_unique_id=data.get("file_unique_id"),
        file_size=data.get("file_size"),
        content_hash=content_hash,
        category=data.get("category"),
        description=description,
        raw_text=raw_text,
    )
    background.spawn(
        _process_and_report(session_maker, bot, material.id, tmp_path, status_msg.chat.id, status_msg.message_id),
        name=f"process-material-{material.id}",
    )


async def _process_and_report(
    session_maker: async_sessionmaker[AsyncSession],
    bot: Bot,
    material_id: int,
    path: Path | None,
    chat_id: int,
    message_id: int,
) -> None:
    try:
        result = await material_service.process_material(session_maker, material_id, path)
    finally:
        if path is not None:
            path.unlink(missing_ok=True)
    if result.ok:
        text = t(
            "mat.processed_ok",
            chunks=result.chunk_count,
            pages=result.page_count or "—",
            chars=result.char_count,
            emb=t("common.yes") if result.embedded else t("mat.no_embeddings"),
        )
    else:
        text = t("mat.processed_fail", error=doc_error_text(result.error_code or "internal"))
        if result.error_code == "internal" and result.error_detail:
            text += "\n" + t("mat.error_detail", d=esc(result.error_detail[:200]))
    markup = kb([(t("mat.btn.open"), AdminCB(s="mat_v", id=material_id))])
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
    except TelegramBadRequest:
        await bot.send_message(chat_id, text, reply_markup=markup)
    if not result.ok or not ai_available():
        return
    # Automatic step: build source-grounded questions from the new material right away.
    async with session_maker() as session:
        count = int(await settings_service.get_value(session, "auto_generate_questions"))
    if count > 0:
        status_msg = await bot.send_message(chat_id, t("gen.auto_started", n=count))
        await _generate_to_bank(session_maker, bot, material_id, count, status_msg.chat.id, status_msg.message_id)


async def download_material(bot: Bot, material: Material) -> Path | None:
    """Fetch the original file again from Telegram (None: pasted text, or the download failed)."""
    if not material.telegram_file_id or material.file_type == FileType.TEXT:
        return None
    fd, name = tempfile.mkstemp(prefix="material_", suffix=f".{material.file_type.value}")
    os.close(fd)
    path = Path(name)
    try:
        await bot.download(material.telegram_file_id, destination=path)
    except Exception as exc:
        logger.warning("Re-download failed; using stored text", extra={"error": str(exc)[:200]})
        path.unlink(missing_ok=True)
        return None
    return path


RESUME_DELAY_SECONDS = 30


async def resume_interrupted_materials(
    bot: Bot, session_maker: async_sessionmaker[AsyncSession], delay: float = RESUME_DELAY_SECONDS
) -> int:
    """At startup: finish materials whose processing was cut off by a restart and tell the admins.

    Each material is retried only ONCE: a marker row is stored before processing and removed after
    it. If the bot dies again while processing the same file (e.g. the file is too big for the
    server's memory), the next start finds the marker and marks the material FAILED instead of
    crashing the bot in a loop.
    """
    from sqlalchemy import delete as sql_delete
    from sqlalchemy.dialects.postgresql import insert

    from app.models import BotSetting
    from app.services.notifications import notify_admins

    await asyncio.sleep(delay)  # let the bot answer users first
    async with session_maker() as session:
        ids = await material_service.interrupted_material_ids(session)
    for material_id in ids:
        marker = f"material_resume:{material_id}"
        open_kb = kb([(t("mat.btn.open"), AdminCB(s="mat_v", id=material_id))])
        async with session_maker() as session:
            material = await session.get(Material, material_id)
            if material is None:
                continue
            title = material.title
            if await session.get(BotSetting, marker) is not None:
                await session.execute(sql_delete(BotSetting).where(BotSetting.key == marker))
                await material_service._mark_failed(session, material_id, "crashed", "")
                await notify_admins(bot, t("mat.resume_crashed", title=esc(title)), open_kb)
                logger.error("Material crashed the bot twice; marked failed", extra={"material_id": material_id})
                continue
            await session.execute(insert(BotSetting).values(key=marker, value=1).on_conflict_do_nothing())
            await session.commit()
            path = await download_material(bot, material)
            if path is None and material.file_type != FileType.TEXT:
                await material_service._mark_failed(session, material_id, "download_failed", "")
                await session.execute(sql_delete(BotSetting).where(BotSetting.key == marker))
                await session.commit()
                await notify_admins(bot, t("mat.resume_failed", title=esc(title)), open_kb)
                continue
        try:
            result = await material_service.process_material(session_maker, material_id, path)
        finally:
            if path is not None:
                path.unlink(missing_ok=True)
            async with session_maker() as session:
                await session.execute(sql_delete(BotSetting).where(BotSetting.key == marker))
                await session.commit()
        await notify_admins(bot, t("mat.resumed_ok" if result.ok else "mat.resume_failed", title=esc(title)), open_kb)
        logger.info("Interrupted material processed again", extra={"material_id": material_id, "ok": result.ok})
    return len(ids)


async def render_material(target, session: AsyncSession, material_id: int, page: int = 0) -> None:
    material = await session.get(Material, material_id)
    if material is None:
        await show(target, t("common.not_found"), kb(back_menu_row("mat")))
        return
    qstats = await material_service.material_question_stats(session, material_id)
    lines = [
        t("mat.detail.title", title=esc(material.title)),
        "",
        t(
            "mat.detail.file",
            name=esc(material.file_name or t("mat.pasted_text")),
            type=material.file_type.value.upper(),
        ),
        t("mat.detail.status", status=f"{STATUS_ICON[material.status]} {material.status.value}"),
        t("mat.detail.category", c=esc(material.category or "—")),
        t("mat.detail.uploaded", d=fmt_dt(material.created_at)),
        t("mat.detail.stats", pages=material.page_count or "—", chunks=material.chunk_count, chars=material.char_count),
        t("mat.detail.questions", approved=qstats["approved"], pending=qstats["pending"], rejected=qstats["rejected"]),
        t("mat.detail.active", a=t("common.yes") if material.is_active else t("mat.archived")),
    ]
    if material.description:
        lines += ["", esc(truncate(material.description, 500))]
    if material.status in (MaterialStatus.UPLOADED, MaterialStatus.PROCESSING):
        lines += ["", t("mat.detail.processing_hint")]
    if material.status == MaterialStatus.FAILED and material.error_message:
        code = material.error_message.split(":", 1)[0]
        lines += ["", t("mat.detail.error", error=doc_error_text(code))]
        detail = material.error_message.split(":", 1)[1].strip() if ":" in material.error_message else ""
        if code == "internal" and detail:
            lines.append(t("mat.error_detail", d=esc(detail[:200])))
    rows = []
    if material.status == MaterialStatus.READY and material.is_active:
        rows.append([(t("mat.btn.quick_test"), AdminCB(s="mat_qt", id=material.id))])
        rows.append([(t("mat.btn.generate"), AdminCB(s="mat_gen", id=material.id))])
        rows.append([(t("mat.btn.questions"), AdminCB(s="qb_mat", id=material.id))])
    if material.status in (MaterialStatus.FAILED, MaterialStatus.READY, MaterialStatus.UPLOADED) or (
        material_service.is_stale(material)
    ):
        rows.append([(t("mat.btn.reprocess"), AdminCB(s="mat_re", id=material.id))])
    rows.append(
        [
            (
                t("mat.btn.unarchive") if not material.is_active else t("mat.btn.archive"),
                AdminCB(s="mat_arch", id=material.id),
            ),
            (t("mat.btn.delete"), AdminCB(s="mat_del", id=material.id)),
        ]
    )
    rows.append(back_menu_row("mat", p=page))
    await show(target, "\n".join(lines), kb(*rows))


@router.callback_query(AdminCB.filter(F.s == "mat_v"))
async def cb_view(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    await render_material(callback, session, callback_data.id, callback_data.p)


@router.callback_query(AdminCB.filter(F.s == "mat_re"))
async def cb_reprocess(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, session_maker, bot: Bot
) -> None:
    material = await session.get(Material, callback_data.id)
    if material is None:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    if material.status == MaterialStatus.PROCESSING and not material_service.is_stale(material):
        await callback.answer(t("mat.already_processing"), show_alert=True)
        return
    await callback.answer()
    assert isinstance(callback.message, Message)
    status_msg = await callback.message.answer(t("mat.processing_started"))
    tmp_path = await download_material(bot, material)
    background.spawn(
        _process_and_report(session_maker, bot, material.id, tmp_path, status_msg.chat.id, status_msg.message_id),
        name=f"reprocess-material-{material.id}",
    )


@router.callback_query(AdminCB.filter(F.s == "mat_arch"))
async def cb_archive(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    material = await session.get(Material, callback_data.id)
    if material is None:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    await material_service.set_archived(session, material.id, material.is_active)
    await callback.answer(t("common.saved"))
    await render_material(callback, session, material.id)


@router.callback_query(AdminCB.filter(F.s == "mat_del"))
async def cb_delete(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    material = await session.get(Material, callback_data.id)
    if material is None:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    await show(
        callback,
        t("mat.delete_confirm", title=esc(material.title)),
        confirm_kb(AdminCB(s="mat_del_ok", id=material.id), AdminCB(s="mat_v", id=material.id)),
    )


@router.callback_query(AdminCB.filter(F.s == "mat_del_ok"))
async def cb_delete_ok(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, state: FSMContext
) -> None:
    await material_service.delete_material(session, callback_data.id)
    await callback.answer(t("common.deleted"))
    await cb_list(callback, AdminCB(s="mat"), session, state)


# ------------------------------------------------------------------ generation into the bank


@router.callback_query(AdminCB.filter(F.s == "mat_gen"))
async def cb_generate(callback: CallbackQuery, callback_data: AdminCB) -> None:
    mid = callback_data.id
    await show(
        callback,
        t("mat.generate_prompt"),
        kb(
            [(str(n), AdminCB(s="mat_gen_n", id=mid, v=str(n))) for n in (5, 10, 15, 20)],
            back_menu_row("mat_v", id_=mid),
        ),
    )


@router.callback_query(AdminCB.filter(F.s == "mat_gen_n"))
async def cb_generate_n(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, session_maker, bot: Bot
) -> None:
    material = await session.get(Material, callback_data.id)
    if material is None or material.status != MaterialStatus.READY:
        await callback.answer(t("mat.not_ready"), show_alert=True)
        return
    try:
        generator_check = make_generator()
    except AIError:
        await callback.answer(t("ai.not_configured"), show_alert=True)
        return
    del generator_check
    count = max(1, min(30, int(callback_data.v or 5)))
    await callback.answer()
    assert isinstance(callback.message, Message)
    status_msg = await callback.message.answer(t("gen.started", n=count, title=esc(material.title)))
    background.spawn(
        _generate_to_bank(session_maker, bot, material.id, count, status_msg.chat.id, status_msg.message_id),
        name=f"generate-material-{material.id}",
    )


async def _generate_to_bank(
    session_maker: async_sessionmaker[AsyncSession],
    bot: Bot,
    material_id: int,
    count: int,
    chat_id: int,
    message_id: int,
) -> None:
    async def progress(done: int, total: int) -> None:
        try:
            await bot.edit_message_text(
                t("gen.progress", done=done, total=total), chat_id=chat_id, message_id=message_id
            )
        except TelegramBadRequest:
            pass

    async with session_maker() as session:
        try:
            generator = make_generator()
            result = await generator.generate(
                session, GenerationRequest(scope=SourceScope(material_ids=[material_id]), count=count), progress
            )
            text = gen_summary_text(result.created, count, dict(result.rejected), result.error)
        except AIError as exc:
            logger.error("Generation failed", extra={"material_id": material_id, "error": str(exc)[:200]})
            text = t("gen.failed", error=esc(_friendly_error(str(exc))))
        except Exception as exc:
            logger.exception("Generation crashed", extra={"material_id": material_id})
            text = t("gen.failed", error=esc(f"{type(exc).__name__}: {str(exc)[:200]}"))
    markup = kb(
        [(t("mat.btn.quick_test"), AdminCB(s="mat_qt", id=material_id))],
        [(t("gen.btn.review"), AdminCB(s="qb_mat", id=material_id, v="pending"))],
    )
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
    except TelegramBadRequest:
        await bot.send_message(chat_id, text, reply_markup=markup)


def gen_summary_text(created: int, requested: int, rejected: dict, error: str | None) -> str:
    lines = [t("gen.done", created=created, requested=requested)]
    if rejected:
        lines.append(t("gen.rejected_title"))
        for key, value in sorted(rejected.items(), key=lambda kv: -kv[1])[:8]:
            label = t(f"gen.reason.{key}") if not key.startswith("option_count_") else t("gen.reason.option_count")
            lines.append(f"• {label}: {value}")
    if error and created < requested:
        lines.append(t("gen.error_note", error=esc(_friendly_error(error))))
    return "\n".join(lines)


def _friendly_error(error: str) -> str:
    """Translate internal/AI error codes into a clear Uzbek explanation for the admin."""
    known = {"no_source_chunks": t("gen.reason.no_source_chunks"), "ai_not_configured": t("ai.not_configured"),
             "no_sources": t("gen.reason.no_sources"), "not_enough_questions": t("gen.reason.not_enough"),
             "interrupted_by_restart": t("gen.reason.interrupted")}  # fmt: skip
    if error in known:
        return known[error]
    lowered = error.lower()
    if "authentication" in lowered:
        return t("ai.err.auth")
    if "models unavailable" in lowered or "no usable groq model" in lowered:
        return t("ai.err.model")
    if "rate limit" in lowered:
        return t("ai.err.rate_limit")
    if "unreachable" in lowered:
        return t("ai.err.unreachable")
    if "invalid ai response" in lowered:
        return t("ai.err.invalid_response")
    return error[:200]


# ------------------------------------------------------------------ ⚡ quick test from one material


@router.callback_query(AdminCB.filter(F.s == "mat_qt"))
async def cb_quick_test(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    material = await session.get(Material, callback_data.id)
    if material is None or material.status != MaterialStatus.READY:
        await callback.answer(t("mat.not_ready"), show_alert=True)
        return
    mid = material.id
    await show(
        callback,
        t("mat.quick_test_count", title=esc(material.title)),
        kb(
            [(str(n), AdminCB(s="mat_qt_n", id=mid, v=str(n))) for n in (10, 15, 20, 30)],
            back_menu_row("mat_v", id_=mid),
        ),
    )


@router.callback_query(AdminCB.filter(F.s == "mat_qt_n"))
async def cb_quick_test_target(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    from app.services import groups as group_service

    groups = await group_service.list_groups(session, active_only=True)
    n = callback_data.v
    rows = [
        [
            (
                t("wiz.in_group", g=truncate(g.title, 36), n=members),
                AdminCB(s="mat_qt_go", id=callback_data.id, v=f"{n}-{g.id}"),
            )
        ]
        for g, members in groups
    ]
    rows.append([(t("wiz.private_mode"), AdminCB(s="mat_qt_go", id=callback_data.id, v=f"{n}-0"))])
    rows.append(back_menu_row("mat_qt", id_=callback_data.id))
    await show(callback, t("mat.quick_test_where"), kb(*rows))


@router.callback_query(AdminCB.filter(F.s == "mat_qt_go"))
async def cb_quick_test_create(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, session_maker, bot: Bot, user: User
) -> None:
    from app.handlers.admin.tests import start_assembly
    from app.models import AnswerReveal, DeliveryMode, TestDifficulty
    from app.services.test_builder import OPEN_WINDOW, TestDraftData, create_test
    from app.utils.time import utcnow

    material = await session.get(Material, callback_data.id)
    count_s, _, group_s = callback_data.v.partition("-")
    if material is None or not count_s.isdigit():
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    group_id = int(group_s) if group_s.isdigit() and int(group_s) else None
    now = utcnow()
    test = await create_test(
        session,
        TestDraftData(
            title=material.title[:200],
            question_count=int(count_s),
            starts_at=now,
            deadline_at=now + OPEN_WINDOW,
            difficulty=TestDifficulty.MIXED,
            material_ids=[material.id],
            randomize_questions=True,
            randomize_options=True,
            answer_reveal=AnswerReveal.IMMEDIATE.value,
            passing_percent=60,
            group_id=group_id,
            delivery_mode=DeliveryMode.GROUP if group_id else DeliveryMode.PRIVATE,
        ),
        user.id,
    )
    await callback.answer(t("wiz.created"))
    await start_assembly(callback, session_maker, bot, test.id)
