"""⚙️ Settings: runtime switches stored in the database + read-only environment info."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.handlers.admin.common import show
from app.keyboards.callbacks import AdminCB
from app.keyboards.common import back_menu_row, cancel_kb, kb
from app.locales import t
from app.rag.embeddings import get_embedding_provider
from app.rag.vector_store import detect_backend
from app.services import embedding_guard, settings_service
from app.services.settings_service import SPECS

router = Router(name="admin_settings")


class SettingStates(StatesGroup):
    value = State()


def _ai_services(settings) -> str:
    """ "Google Gemini ✅ → Groq ⏳ 45 daq" — the order the bot uses the AI services in."""
    from app.ai.chain import ChainProvider
    from app.ai.factory import TITLES, get_llm_provider

    if not settings.ai_providers:
        return "—"
    try:
        provider = get_llm_provider(settings)
    except Exception:
        provider = None
    if isinstance(provider, ChainProvider):
        parts = [f"{title} " + (t("settings.ai_wait", m=m) if m else "✅") for title, m in provider.status()]
    else:
        parts = [TITLES.get(name, name) for name in settings.ai_providers]
    return " → ".join(parts)


async def render_settings(target, session: AsyncSession) -> None:
    values = await settings_service.get_all(session)
    settings = get_settings()
    embeddings = get_embedding_provider()
    backend = await detect_backend(session)
    lines = [t("settings.title"), ""]
    rows = []
    for key, spec in SPECS.items():
        if not spec.visible:
            continue
        value = values[key]
        shown = (t("common.on") if value else t("common.off")) if spec.kind == "bool" else str(value)
        lines.append(t(f"settings.{key}", v=shown))
        action = "set_tg" if spec.kind == "bool" else "set_int"
        rows.append([(t(f"settings.btn.{key}"), AdminCB(s=action, v=key))])
    lines += [
        "",
        t(
            "settings.env",
            tz=settings.timezone,
            model=_ai_services(settings),
            ai=t("common.on") if settings.ai_enabled else t("common.off"),
            emb=f"{embeddings.name} ({'OK' if embeddings.available else '—'})",
            backend=backend,
            admins=len(settings.admin_ids),
            mode=settings.bot_mode,
        ),
    ]
    if await embedding_guard.is_disabled(session):
        lines += ["", t("settings.emb_off")]
        rows.append([(t("settings.btn.emb_on"), AdminCB(s="set_emb"))])
    await show(target, "\n".join(lines), kb(*rows, back_menu_row("menu")))


@router.callback_query(AdminCB.filter(F.s == "set"))
async def cb_settings(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    await render_settings(callback, session)


@router.callback_query(AdminCB.filter(F.s == "set_emb"))
async def cb_embeddings_on(callback: CallbackQuery, session: AsyncSession, session_maker) -> None:
    await callback.answer()
    ok = await embedding_guard.switch_on(session_maker)
    if not ok:
        embedding_guard.switch_off()
    assert isinstance(callback.message, Message)
    await callback.message.answer(t("settings.emb_on_ok" if ok else "settings.emb_on_fail"))
    await render_settings(callback, session)


@router.callback_query(AdminCB.filter(F.s == "set_tg"))
async def cb_toggle(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    if callback_data.v not in SPECS or SPECS[callback_data.v].kind != "bool":
        await callback.answer(t("errors.stale_button"))
        return
    current = await settings_service.get_value(session, callback_data.v)
    await settings_service.set_value(session, callback_data.v, not current)
    await callback.answer(t("common.saved"))
    await render_settings(callback, session)


@router.callback_query(AdminCB.filter(F.s == "set_int"))
async def cb_int(callback: CallbackQuery, callback_data: AdminCB, state: FSMContext) -> None:
    spec = SPECS.get(callback_data.v)
    if spec is None or spec.kind != "int":
        await callback.answer(t("errors.stale_button"))
        return
    await state.set_state(SettingStates.value)
    await state.update_data(setting_key=spec.key)
    await show(
        callback,
        t("settings.int_prompt", name=t(f"settings.btn.{spec.key}"), min=spec.min_value, max=spec.max_value),
        cancel_kb(),
    )


@router.message(SettingStates.value, F.text)
async def on_int(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    spec = SPECS[data["setting_key"]]
    value = (message.text or "").strip()
    if not value.isdigit() or not (spec.min_value or 0) <= int(value) <= (spec.max_value or 10**6):
        await message.answer(t("settings.int_bad", min=spec.min_value, max=spec.max_value), reply_markup=cancel_kb())
        return
    await settings_service.set_value(session, spec.key, int(value))
    await state.clear()
    await message.answer(t("common.saved"))
    await render_settings(message, session)
