"""🗂 Question bank: browse, filter, search, approve/reject, edit, delete, regenerate."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.base import AIError
from app.handlers.admin.common import show
from app.handlers.admin.materials import _friendly_error
from app.keyboards.callbacks import AdminCB
from app.keyboards.common import back_menu_row, cancel_kb, confirm_kb, kb, pager
from app.locales import t
from app.models import Material, Question, QuestionStatus, SourceChunk, User
from app.rag.vector_store import SourceScope
from app.services import background
from app.services import question_bank as qb
from app.services.ai_runtime import ai_available, make_generator
from app.services.question_bank import QuestionFilter
from app.services.question_generation import GenerationRequest
from app.utils.text import esc, truncate
from app.utils.time import fmt_dt

logger = logging.getLogger(__name__)
router = Router(name="admin_questions")

STATUS_ICON = {QuestionStatus.APPROVED: "✅", QuestionStatus.PENDING: "🟡", QuestionStatus.REJECTED: "❌"}


class BankStates(StatesGroup):
    search = State()
    edit = State()


async def _filter(state: FSMContext) -> QuestionFilter:
    return QuestionFilter.from_dict((await state.get_data()).get("qb_filter"))


async def _save_filter(state: FSMContext, flt: QuestionFilter) -> None:
    await state.update_data(qb_filter=flt.to_dict())


async def _describe(session: AsyncSession, flt: QuestionFilter) -> str:
    parts = []
    if flt.status:
        parts.append(t(f"question_status.{flt.status}"))
    if flt.difficulty:
        parts.append(t(f"difficulty.{flt.difficulty}"))
    if flt.material_id:
        material = await session.get(Material, flt.material_id)
        parts.append("📚 " + esc(truncate(material.title, 30) if material else "?"))
    if flt.news_only:
        parts.append(t("source_kind.news"))
    if flt.topic_id:
        from app.models import Topic

        topic = await session.get(Topic, flt.topic_id)
        parts.append("🏷 " + esc(topic.name if topic else "?"))
    if flt.query:
        parts.append(f"🔍 «{esc(flt.query)}»")
    return ", ".join(parts) or t("common.all")


async def render_list(target, session: AsyncSession, state: FSMContext, page: int) -> None:
    flt = await _filter(state)
    items, total = await qb.list_questions(session, flt, page)
    counts = await qb.bank_counts(session)
    lines = [
        t(
            "qb.section",
            total=counts["total"],
            approved=counts["approved"],
            pending=counts["pending"],
            rejected=counts["rejected"],
        ),
        t("qb.filter", f=await _describe(session, flt)),
        t("qb.found", n=total),
    ]
    rows = [[(t("qb.btn.filters"), AdminCB(s="qb_f")), (t("qb.btn.search"), AdminCB(s="qb_srch"))]]
    for q in items:
        rows.append(
            [(f"{STATUS_ICON[q.status]} #{q.id} {truncate(q.question_text, 40)}", AdminCB(s="qb_v", id=q.id, p=page))]
        )
    rows.append(pager("qb_l", page, total, qb.PAGE_SIZE))
    rows.append(back_menu_row("menu"))
    await show(target, "\n".join(lines), kb(*rows))


@router.callback_query(AdminCB.filter(F.s == "qb"))
async def cb_section(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await state.set_state(None)
    await render_list(callback, session, state, 0)


@router.callback_query(AdminCB.filter(F.s == "qb_l"))
async def cb_page(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, state: FSMContext) -> None:
    await render_list(callback, session, state, max(0, callback_data.p))


@router.callback_query(AdminCB.filter(F.s == "qb_mat"))
async def cb_by_material(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, state: FSMContext
) -> None:
    await state.set_state(None)
    await _save_filter(state, QuestionFilter(material_id=callback_data.id, status=callback_data.v or None))
    await render_list(callback, session, state, 0)


@router.callback_query(AdminCB.filter(F.s == "qb_news"))
async def cb_news_questions(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await state.set_state(None)
    await _save_filter(state, QuestionFilter(news_only=True))
    await render_list(callback, session, state, 0)


@router.callback_query(AdminCB.filter(F.s == "qb_f"))
async def cb_filters(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    flt = await _filter(state)
    rows = [
        [
            (("• " if flt.status == s else "") + t(f"question_status.{s}"), AdminCB(s="qb_fs", v=s))
            for s in ("pending", "approved", "rejected")
        ],
        [
            (("• " if flt.difficulty == d else "") + t(f"difficulty.{d}"), AdminCB(s="qb_fd", v=d))
            for d in ("easy", "medium", "hard")
        ],
        [(t("qb.btn.by_topic"), AdminCB(s="qb_ft")), (t("qb.btn.by_material"), AdminCB(s="qb_fm"))],
        [
            (("• " if flt.news_only else "") + t("source_kind.news"), AdminCB(s="qb_fn")),
            (t("qb.btn.clear"), AdminCB(s="qb_fc")),
        ],
        back_menu_row("qb_l"),
    ]
    await show(callback, t("qb.filters_title", f=await _describe(session, flt)), kb(*rows))


@router.callback_query(AdminCB.filter(F.s.in_({"qb_fs", "qb_fd", "qb_fn", "qb_fc", "qb_ft_set", "qb_fm_set"})))
async def cb_filter_set(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, state: FSMContext
) -> None:
    flt = await _filter(state)
    if callback_data.s == "qb_fs":
        flt.status = None if flt.status == callback_data.v else callback_data.v
    elif callback_data.s == "qb_fd":
        flt.difficulty = None if flt.difficulty == callback_data.v else callback_data.v
    elif callback_data.s == "qb_fn":
        flt.news_only = not flt.news_only
        flt.material_id = None
    elif callback_data.s == "qb_ft_set":
        flt.topic_id = callback_data.id or None
    elif callback_data.s == "qb_fm_set":
        flt.material_id = callback_data.id or None
        flt.news_only = False
    else:
        flt = QuestionFilter()
    await _save_filter(state, flt)
    await render_list(callback, session, state, 0)


@router.callback_query(AdminCB.filter(F.s == "qb_ft"))
async def cb_topic_choose(callback: CallbackQuery, session: AsyncSession) -> None:
    topics = await qb.topics_with_counts(session)
    rows = [[(f"{truncate(tp.name, 35)} ({n})", AdminCB(s="qb_ft_set", id=tp.id))] for tp, n in topics[:20]]
    rows.append([(t("qb.btn.any"), AdminCB(s="qb_ft_set", id=0))])
    rows.append(back_menu_row("qb_f"))
    await show(callback, t("qb.choose_topic") if topics else t("qb.no_topics"), kb(*rows))


@router.callback_query(AdminCB.filter(F.s == "qb_fm"))
async def cb_material_choose(callback: CallbackQuery, session: AsyncSession) -> None:
    from app.services.materials import ready_materials

    materials = (await ready_materials(session))[:20]
    rows = [[(truncate(m.title, 40), AdminCB(s="qb_fm_set", id=m.id))] for m in materials]
    rows.append([(t("qb.btn.any"), AdminCB(s="qb_fm_set", id=0))])
    rows.append(back_menu_row("qb_f"))
    await show(callback, t("qb.choose_material"), kb(*rows))


@router.callback_query(AdminCB.filter(F.s == "qb_srch"))
async def cb_search(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BankStates.search)
    await show(callback, t("qb.search_prompt"), cancel_kb())


@router.message(BankStates.search, F.text)
async def on_search(message: Message, session: AsyncSession, state: FSMContext) -> None:
    flt = await _filter(state)
    flt.query = (message.text or "").strip()[:100]
    await _save_filter(state, flt)
    await state.set_state(None)
    await render_list(message, session, state, 0)


async def render_question(target, session: AsyncSession, question_id: int, page: int = 0) -> None:
    q = await session.get(Question, question_id)
    if q is None:
        await show(target, t("common.not_found"), kb(back_menu_row("qb_l")))
        return
    options = "\n".join(
        f"{'✅' if letter == q.correct_option else '▫️'} <b>{letter})</b> {esc(text)}"
        for letter, text in q.options.items()
    )
    text = t(
        "qb.detail",
        id=q.id,
        status=f"{STATUS_ICON[q.status]} {t(f'question_status.{q.status.value}')}",
        version=q.version,
        question=esc(q.question_text),
        options=options,
        explanation=esc(truncate(q.explanation, 700)),
        topic=esc(q.topic_name),
        difficulty=t(f"difficulty.{q.difficulty.value}"),
        source=esc(truncate(q.source_reference, 200)),
        excerpt=esc(truncate(q.source_excerpt, 700)),
        used=q.times_used,
        created=fmt_dt(q.created_at),
        confidence=f"{q.confidence:.2f}" if q.confidence is not None else "—",
    )
    rows = []
    status_row = []
    if q.status != QuestionStatus.APPROVED:
        status_row.append((t("tests.btn.approve"), AdminCB(s="qb_ap", id=q.id, p=page)))
    if q.status != QuestionStatus.REJECTED:
        status_row.append((t("tests.btn.reject"), AdminCB(s="qb_rj", id=q.id, p=page)))
    rows.append(status_row)
    rows.append(
        [
            (t("qb.btn.edit"), AdminCB(s="qb_ed", id=q.id, p=page)),
            (t("tests.btn.regenerate"), AdminCB(s="qb_rg", id=q.id, p=page)),
        ]
    )
    rows.append([(t("mat.btn.delete"), AdminCB(s="qb_del", id=q.id, p=page))])
    rows.append(back_menu_row("qb_l", p=page))
    await show(target, text, kb(*rows))


@router.callback_query(AdminCB.filter(F.s == "qb_v"))
async def cb_view(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    await render_question(callback, session, callback_data.id, callback_data.p)


@router.callback_query(AdminCB.filter(F.s.in_({"qb_ap", "qb_rj"})))
async def cb_review(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User) -> None:
    status = QuestionStatus.APPROVED if callback_data.s == "qb_ap" else QuestionStatus.REJECTED
    if await qb.set_status(session, callback_data.id, status, user.id) is None:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    await callback.answer(t("common.saved"))
    await render_question(callback, session, callback_data.id, callback_data.p)


@router.callback_query(AdminCB.filter(F.s == "qb_del"))
async def cb_delete(callback: CallbackQuery, callback_data: AdminCB) -> None:
    await show(
        callback,
        t("qb.delete_confirm", id=callback_data.id),
        confirm_kb(
            AdminCB(s="qb_del_ok", id=callback_data.id, p=callback_data.p),
            AdminCB(s="qb_v", id=callback_data.id, p=callback_data.p),
        ),
    )


@router.callback_query(AdminCB.filter(F.s == "qb_del_ok"))
async def cb_delete_ok(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, state: FSMContext
) -> None:
    await qb.delete_question(session, callback_data.id)
    await callback.answer(t("common.deleted"))
    await render_list(callback, session, state, callback_data.p)


FIELD_KEYS = ("question", "A", "B", "C", "D", "E", "correct", "explanation", "topic", "difficulty")


@router.callback_query(AdminCB.filter(F.s == "qb_ed"))
async def cb_edit(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    q = await session.get(Question, callback_data.id)
    if q is None:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    option_row = [(t("qb.field.option", l=letter), AdminCB(s="qb_edf", id=q.id, v=letter)) for letter in q.options]
    rows = [
        [(t("qb.field.question"), AdminCB(s="qb_edf", id=q.id, v="question"))],
        option_row,
        [
            (t("qb.field.correct"), AdminCB(s="qb_edf", id=q.id, v="correct")),
            (t("qb.field.explanation"), AdminCB(s="qb_edf", id=q.id, v="explanation")),
        ],
        [
            (t("qb.field.topic"), AdminCB(s="qb_edf", id=q.id, v="topic")),
            (t("qb.field.difficulty"), AdminCB(s="qb_edf", id=q.id, v="difficulty")),
        ],
    ]
    if ai_available():
        rows.append([(t("qb.btn.ai_explanation"), AdminCB(s="qb_aiexp", id=q.id))])
    rows.append(back_menu_row("qb_v", id_=q.id, p=callback_data.p))
    await show(callback, t("qb.edit_title", id=q.id), kb(*rows))


@router.callback_query(AdminCB.filter(F.s == "qb_edf"))
async def cb_edit_field(
    callback: CallbackQuery, callback_data: AdminCB, state: FSMContext, session: AsyncSession
) -> None:
    q = await session.get(Question, callback_data.id)
    if q is None or callback_data.v not in FIELD_KEYS:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    if callback_data.v == "correct":
        rows = [[(letter, AdminCB(s="qb_set", id=q.id, v=f"correct-{letter}")) for letter in q.options]]
        await show(callback, t("qb.choose_correct"), kb(*rows, back_menu_row("qb_ed", id_=q.id)))
        return
    if callback_data.v == "difficulty":
        rows = [
            [
                (t(f"difficulty.{d}"), AdminCB(s="qb_set", id=q.id, v=f"difficulty-{d}"))
                for d in ("easy", "medium", "hard")
            ]
        ]
        await show(callback, t("qb.choose_difficulty"), kb(*rows, back_menu_row("qb_ed", id_=q.id)))
        return
    current = {
        "question": q.question_text,
        "explanation": q.explanation,
        "topic": q.topic_name,
    }.get(callback_data.v, q.options.get(callback_data.v, ""))
    await state.set_state(BankStates.edit)
    await state.update_data(edit_qid=q.id, edit_field=callback_data.v)
    await show(callback, t("qb.edit_prompt", current=esc(truncate(current, 1500))), cancel_kb())


async def _apply_edit(target, session: AsyncSession, qid: int, field: str, value: str, user: User) -> None:
    result = await qb.apply_edit(session, qid, field, value, user.id)
    if not result.ok:
        errors = ", ".join(t(f"gen.reason.{e}") for e in result.errors)
        await show(target, t("qb.edit_failed", errors=errors), kb(back_menu_row("qb_ed", id_=qid)))
        return
    await render_question(target, session, qid)


@router.message(BankStates.edit, F.text)
async def on_edit(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    data = await state.get_data()
    await state.set_state(None)
    value = (message.text or "").strip()
    if not value:
        await message.answer(t("common.unexpected_input"))
        return
    await _apply_edit(message, session, int(data["edit_qid"]), str(data["edit_field"]), value, user)


@router.callback_query(AdminCB.filter(F.s == "qb_set"))
async def cb_set_value(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User) -> None:
    field, _, value = callback_data.v.partition("-")
    await callback.answer()
    await _apply_edit(callback, session, callback_data.id, field, value, user)


@router.callback_query(AdminCB.filter(F.s == "qb_aiexp"))
async def cb_ai_explanation(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User) -> None:
    q = await session.get(Question, callback_data.id)
    if q is None:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    chunk = await session.get(SourceChunk, q.source_chunk_id) if q.source_chunk_id else None
    source = chunk.text if chunk else q.source_excerpt
    await callback.answer(t("qb.ai_working"))
    try:
        generator = make_generator()
        explanation = await generator.regenerate_explanation(source, q.question_text, q.options, q.correct_option)
        supported, verification = await generator.verify_existing(source, q.question_text, q.options, q.correct_option)
    except AIError as exc:
        await show(
            callback,
            t("gen.failed", error=esc(_friendly_error(str(exc)))),
            kb(back_menu_row("qb_v", id_=q.id)),
            answer=False,
        )
        return
    if explanation is None or not supported:
        q.validation = {**(q.validation or {}), "admin_check": verification}
        await session.commit()
        await show(callback, t("qb.ai_unsupported"), kb(back_menu_row("qb_v", id_=q.id)), answer=False)
        return
    await _apply_edit(callback, session, q.id, "explanation", explanation, user)


@router.callback_query(AdminCB.filter(F.s == "qb_rg"))
async def cb_regenerate(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, session_maker, bot: Bot, user: User
) -> None:
    q = await session.get(Question, callback_data.id)
    if q is None:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    if not ai_available():
        await callback.answer(t("ai.not_configured"), show_alert=True)
        return
    scope = SourceScope(
        material_ids=[q.source_material_id] if q.source_material_id else [],
        news_ids=[q.source_news_id] if q.source_news_id else [],
    )
    if scope.empty:
        await callback.answer(t("qb.no_source"), show_alert=True)
        return
    await qb.set_status(session, q.id, QuestionStatus.REJECTED, user.id)
    await callback.answer(t("tests.review.regenerating"))
    assert isinstance(callback.message, Message)
    status_msg = await callback.message.answer(t("gen.started", n=1, title=f"#{q.id}"))
    background.spawn(
        _regenerate(
            session_maker, bot, scope, q.difficulty.value, q.option_count, status_msg.chat.id, status_msg.message_id
        ),
        name=f"regenerate-question-{q.id}",
    )


async def _regenerate(
    session_maker: async_sessionmaker[AsyncSession], bot: Bot, scope: SourceScope, difficulty: str, option_count: int,
    chat_id: int, message_id: int,
) -> None:  # fmt: skip
    from app.handlers.admin.materials import gen_summary_text
    from app.models import TestDifficulty

    markup = None
    async with session_maker() as session:
        try:
            result = await make_generator().generate(
                session,
                GenerationRequest(
                    scope=scope, count=1, option_count=option_count, difficulty=TestDifficulty(difficulty)
                ),
            )
            text = gen_summary_text(result.created, 1, dict(result.rejected), result.error)
            if result.questions:
                markup = kb([(t("qb.btn.open_new"), AdminCB(s="qb_v", id=result.questions[0].id))])
        except AIError as exc:
            text = t("gen.failed", error=esc(_friendly_error(str(exc))))
        except Exception as exc:
            logger.exception("Question regeneration crashed")
            text = t("gen.failed", error=esc(f"{type(exc).__name__}: {str(exc)[:200]}"))
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
    except TelegramBadRequest:
        await bot.send_message(chat_id, text, reply_markup=markup)
