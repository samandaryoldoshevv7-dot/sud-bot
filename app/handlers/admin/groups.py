"""👥 Groups: registered Telegram groups, members, activation, announcements."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.handlers.admin.common import show
from app.handlers.formatting import user_line
from app.keyboards.callbacks import AdminCB
from app.keyboards.common import back_menu_row, confirm_kb, kb, pager
from app.locales import t
from app.models import Group, Test, TestStatus, User
from app.services import groups as group_service
from app.services.notifications import deep_link, safe_send, test_card_text
from app.utils.text import esc, truncate
from app.utils.time import fmt_dt

router = Router(name="admin_groups")


@router.callback_query(AdminCB.filter(F.s == "grp"))
async def cb_list(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    groups = await group_service.list_groups(session)
    me = await bot.me()
    lines = [t("groups.title", n=len(groups)), "", t("groups.howto", bot=esc(me.username or ""))]
    rows = [
        [(f"{'🟢' if g.is_active else '⚪️'} {truncate(g.title, 36)} ({n})", AdminCB(s="grp_v", id=g.id))]
        for g, n in groups
    ]
    await show(callback, "\n".join(lines), kb(*rows, back_menu_row("menu")))


async def render_group(target, session: AsyncSession, group_id: int) -> None:
    group = await session.get(Group, group_id)
    if group is None:
        await show(target, t("common.not_found"), kb(back_menu_row("grp")))
        return
    _, members = await group_service.group_members(session, group.id, 0, 1)
    text = t(
        "groups.detail",
        title=esc(group.title),
        chat_id=group.chat_id,
        members=members,
        active=t("common.yes") if group.is_active else t("common.no"),
        created=fmt_dt(group.created_at),
    )
    await show(
        target,
        text,
        kb(
            [
                (t("groups.btn.members"), AdminCB(s="grp_mem", id=group.id)),
                (t("groups.btn.announce"), AdminCB(s="grp_ann", id=group.id)),
            ],
            [
                (
                    t("news.btn.deactivate") if group.is_active else t("news.btn.activate"),
                    AdminCB(s="grp_tg", id=group.id),
                ),
                (t("mat.btn.delete"), AdminCB(s="grp_del", id=group.id)),
            ],
            back_menu_row("grp"),
        ),
    )


@router.callback_query(AdminCB.filter(F.s == "grp_v"))
async def cb_view(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    await render_group(callback, session, callback_data.id)


@router.callback_query(AdminCB.filter(F.s == "grp_reg"))
async def cb_register(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, bot: Bot, user: User
) -> None:
    chat_id = int(callback_data.v)
    try:
        chat = await bot.get_chat(chat_id)
    except Exception:
        await callback.answer(t("groups.unreachable"), show_alert=True)
        return
    group, _ = await group_service.register_group(session, chat_id, chat.title or str(chat_id), user)
    await callback.answer(t("common.saved"))
    await safe_send(bot, chat_id, t("group.registered", title=esc(group.title)))
    await render_group(callback, session, group.id)


@router.callback_query(AdminCB.filter(F.s == "grp_tg"))
async def cb_toggle(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    group = await session.get(Group, callback_data.id)
    if group is None:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    await group_service.set_active(session, group.id, not group.is_active)
    await callback.answer(t("common.saved"))
    await render_group(callback, session, group.id)


@router.callback_query(AdminCB.filter(F.s == "grp_del"))
async def cb_delete(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    group = await session.get(Group, callback_data.id)
    if group is None:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    await show(
        callback,
        t("groups.delete_confirm", title=esc(group.title)),
        confirm_kb(AdminCB(s="grp_del_ok", id=group.id), AdminCB(s="grp_v", id=group.id)),
    )


@router.callback_query(AdminCB.filter(F.s == "grp_del_ok"))
async def cb_delete_ok(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, bot: Bot) -> None:
    await group_service.delete_group(session, callback_data.id)
    await callback.answer(t("common.deleted"))
    await cb_list(callback, session, bot)


@router.callback_query(AdminCB.filter(F.s == "grp_mem"))
async def cb_members(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    page = max(0, callback_data.p)
    users, total = await group_service.group_members(session, callback_data.id, page, 15)
    lines = [t("groups.members", n=total), ""] + [f"• {user_line(u)}" for u in users]
    if not users:
        lines.append(t("groups.members_hint"))
    rows = [[(truncate(u.display_name, 40), AdminCB(s="emp_v", id=u.id))] for u in users]
    await show(
        callback,
        "\n".join(lines),
        kb(
            *rows, pager("grp_mem", page, total, 15, id_=callback_data.id), back_menu_row("grp_v", id_=callback_data.id)
        ),
    )


@router.callback_query(AdminCB.filter(F.s == "grp_ann"))
async def cb_announce_choose(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    tests = (
        (await session.execute(select(Test).where(Test.status == TestStatus.ACTIVE).order_by(Test.deadline_at)))
        .scalars()
        .all()
    )
    rows = [[(truncate(x.title, 40), AdminCB(s="grp_ann_t", id=callback_data.id, v=str(x.id)))] for x in tests]
    await show(
        callback,
        t("groups.choose_test") if rows else t("groups.no_active_tests"),
        kb(*rows, back_menu_row("grp_v", id_=callback_data.id)),
    )


@router.callback_query(AdminCB.filter(F.s == "grp_ann_t"))
async def cb_announce(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, bot: Bot) -> None:
    group = await session.get(Group, callback_data.id)
    test = await session.get(Test, int(callback_data.v or 0))
    if group is None or test is None or test.status != TestStatus.ACTIVE:
        await callback.answer(t("common.not_found"), show_alert=True)
        return
    link = await deep_link(bot, f"test_{test.id}")
    ok = await safe_send(
        bot,
        group.chat_id,
        t("announce.group_header") + "\n\n" + await test_card_text(test),
        kb([(t("btn.start_test_caps"), link)]),
    )
    await callback.answer(t("groups.announced") if ok else t("groups.unreachable"), show_alert=not ok)
