"""📰 News: add, list, toggle active, delete. Active news can be used as a test source."""

from __future__ import annotations

from datetime import date

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.handlers.admin.common import show
from app.keyboards.callbacks import AdminCB
from app.keyboards.common import back_menu_row, cancel_kb, confirm_kb, kb, pager
from app.locales import t
from app.models import MaterialStatus, News, User
from app.services import materials as material_service
from app.utils.text import esc, truncate
from app.utils.time import parse_local_datetime, to_local, utcnow

router = Router(name="admin_news")


class NewsStates(StatesGroup):
    title = State()
    body = State()
    source = State()
    category = State()
    news_date = State()


@router.callback_query(AdminCB.filter(F.s == "news"))
async def cb_list(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    page = max(0, callback_data.p)
    items, total = await material_service.list_news(session, page)
    lines = [t("news.section", total=total), ""]
    if not items:
        lines.append(t("news.empty"))
    rows = []
    for item in items:
        icon = "🟢" if item.is_active else "⚪️"
        if item.status == MaterialStatus.FAILED:
            icon = "❌"
        rows.append(
            [(f"{icon} {item.news_date:%d.%m} {truncate(item.title, 36)}", AdminCB(s="news_v", id=item.id, p=page))]
        )
    await show(
        callback,
        "\n".join(lines),
        kb(
            [(t("news.btn.add"), AdminCB(s="news_add"))],
            *rows,
            pager("news", page, total, material_service.PAGE_SIZE),
            back_menu_row("menu"),
        ),
    )


@router.callback_query(AdminCB.filter(F.s == "news_add"))
async def cb_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(NewsStates.title)
    await show(callback, t("news.ask_title"), cancel_kb())


@router.message(NewsStates.title, F.text)
async def on_title(message: Message, state: FSMContext) -> None:
    title = " ".join((message.text or "").split())
    if not 3 <= len(title) <= 255:
        await message.answer(t("mat.bad_title"), reply_markup=cancel_kb())
        return
    await state.update_data(title=title)
    await state.set_state(NewsStates.body)
    await message.answer(t("news.ask_body"), reply_markup=cancel_kb())


@router.message(NewsStates.body, F.text)
async def on_body(message: Message, state: FSMContext) -> None:
    body = (message.text or "").strip()
    if len(body) < 50:
        await message.answer(t("news.body_short"), reply_markup=cancel_kb())
        return
    await state.update_data(body=body)
    await state.set_state(NewsStates.source)
    await message.answer(
        t("news.ask_source"),
        reply_markup=kb([(t("btn.skip"), AdminCB(s="news_skip")), (t("btn.cancel"), AdminCB(s="cancel"))]),
    )


async def _next_after_source(target, state: FSMContext) -> None:
    await state.set_state(NewsStates.category)
    await show(
        target,
        t("news.ask_category"),
        kb([(t("btn.skip"), AdminCB(s="news_skip")), (t("btn.cancel"), AdminCB(s="cancel"))]),
    )


async def _next_after_category(target, state: FSMContext) -> None:
    await state.set_state(NewsStates.news_date)
    await show(
        target,
        t("news.ask_date"),
        kb([(t("news.btn.today"), AdminCB(s="news_skip")), (t("btn.cancel"), AdminCB(s="cancel"))]),
    )


@router.message(NewsStates.source, F.text)
async def on_source(message: Message, state: FSMContext) -> None:
    await state.update_data(source=(message.text or "").strip()[:255])
    await _next_after_source(message, state)


@router.message(NewsStates.category, F.text)
async def on_category(message: Message, state: FSMContext) -> None:
    await state.update_data(category=(message.text or "").strip()[:128])
    await _next_after_category(message, state)


@router.callback_query(AdminCB.filter(F.s == "news_skip"))
async def cb_skip(callback: CallbackQuery, state: FSMContext, session: AsyncSession, user: User) -> None:
    current = await state.get_state()
    if current == NewsStates.source.state:
        await _next_after_source(callback, state)
    elif current == NewsStates.category.state:
        await _next_after_category(callback, state)
    elif current == NewsStates.news_date.state:
        local_today = to_local(utcnow())
        assert local_today is not None
        await callback.answer()
        await _save(callback.message, state, session, user, local_today.date())  # type: ignore[arg-type]
    else:
        await callback.answer(t("errors.stale_button"))


@router.message(NewsStates.news_date, F.text)
async def on_date(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    parsed = parse_local_datetime((message.text or "").strip())
    if parsed is None:
        await message.answer(t("news.bad_date"), reply_markup=cancel_kb())
        return
    local = to_local(parsed)
    assert local is not None
    await _save(message, state, session, user, local.date())


async def _save(message: Message, state: FSMContext, session: AsyncSession, user: User, news_date: date) -> None:
    data = await state.get_data()
    await state.clear()
    wait = await message.answer(t("news.saving"))
    news = await material_service.create_news(
        session,
        title=data["title"],
        body=data["body"],
        news_date=news_date,
        source=data.get("source"),
        category=data.get("category"),
        created_by_id=user.id,
    )
    await wait.delete()
    await render_news(message, session, news.id)


async def render_news(target, session: AsyncSession, news_id: int, page: int = 0) -> None:
    news = await session.get(News, news_id)
    if news is None:
        await show(target, t("common.not_found"), kb(back_menu_row("news")))
        return
    text = t(
        "news.detail",
        title=esc(news.title),
        date=f"{news.news_date:%d.%m.%Y}",
        source=esc(news.source or "—"),
        category=esc(news.category or "—"),
        active=t("common.yes") if news.is_active else t("common.no"),
        status=news.status.value,
        body=esc(truncate(news.body, 2500)),
    )
    await show(
        target,
        text,
        kb(
            [
                (
                    t("news.btn.deactivate") if news.is_active else t("news.btn.activate"),
                    AdminCB(s="news_tg", id=news.id),
                ),
                (t("mat.btn.delete"), AdminCB(s="news_del", id=news.id)),
            ],
            [(t("mat.btn.questions"), AdminCB(s="qb_news"))],
            back_menu_row("news", p=page),
        ),
    )


@router.callback_query(AdminCB.filter(F.s == "news_v"))
async def cb_view(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    await render_news(callback, session, callback_data.id, callback_data.p)


@router.callback_query(AdminCB.filter(F.s == "news_tg"))
async def cb_toggle(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    news = await session.get(News, callback_data.id)
    if news is None:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    news.is_active = not news.is_active
    await session.commit()
    await callback.answer(t("common.saved"))
    await render_news(callback, session, news.id)


@router.callback_query(AdminCB.filter(F.s == "news_del"))
async def cb_delete(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    news = await session.get(News, callback_data.id)
    if news is None:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    await show(
        callback,
        t("news.delete_confirm", title=esc(news.title)),
        confirm_kb(AdminCB(s="news_del_ok", id=news.id), AdminCB(s="news_v", id=news.id)),
    )


@router.callback_query(AdminCB.filter(F.s == "news_del_ok"))
async def cb_delete_ok(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, state: FSMContext
) -> None:
    from sqlalchemy import update

    from app.models import Question, QuestionStatus

    news = await session.get(News, callback_data.id)
    if news is not None:
        await session.execute(
            update(Question)
            .where(Question.source_news_id == news.id, Question.status != QuestionStatus.REJECTED)
            .values(status=QuestionStatus.REJECTED)
        )
        await session.delete(news)
        await session.commit()
    await callback.answer(t("common.deleted"))
    await cb_list(callback, AdminCB(s="news"), session, state)
