"""📝 Test creation wizard (finite-state machine, every step has Back/Cancel)."""

from __future__ import annotations

import logging
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.handlers.admin.common import show
from app.keyboards.callbacks import AdminCB, WizCB
from app.keyboards.common import Button, kb
from app.locales import t
from app.models import TestDifficulty, User
from app.services import groups as group_service
from app.services import materials as material_service
from app.services.ai_runtime import ai_available
from app.services.test_builder import TestDraftData, TestStateError, create_test
from app.utils.text import esc, truncate
from app.utils.time import fmt_dt, fmt_hours, parse_local_datetime, utcnow

logger = logging.getLogger(__name__)
router = Router(name="admin_test_wizard")

STEPS = [
    "title",
    "description",
    "count",
    "options",
    "difficulty",
    "sources",
    "news",
    "focus",
    "audience",
    "start",
    "duration",
    "rand_q",
    "rand_o",
    "reveal",
    "retakes",
    "passing",
    "confirm",
]
TEXT_STEPS = {"title", "description", "count", "news", "focus", "start", "duration", "passing"}
MAX_MATERIAL_BUTTONS = 20


class WizardStates(StatesGroup):
    text_input = State()
    choice = State()


def _nav(step: str, skip: bool = False) -> list[Button]:
    row: list[Button] = []
    if STEPS.index(step) > 0:
        row.append((t("btn.back"), WizCB(f="_back", v=step)))
    if skip:
        row.append((t("btn.skip"), WizCB(f=step, v="_skip")))
    row.append((t("btn.cancel"), AdminCB(s="cancel")))
    return row


def _yes_no(step: str) -> InlineKeyboardMarkup:
    return kb([(t("common.yes"), WizCB(f=step, v="1")), (t("common.no"), WizCB(f=step, v="0"))], _nav(step))


async def _prompt(target, state: FSMContext, session: AsyncSession, step: str) -> None:
    data = await state.get_data()
    wiz: dict = data.get("wiz", {})
    await state.update_data(step=step)
    await state.set_state(WizardStates.text_input if step in TEXT_STEPS else WizardStates.choice)
    header = t("wiz.header", n=STEPS.index(step) + 1, total=len(STEPS))

    if step == "title":
        await show(target, header + t("wiz.title"), kb(_nav(step)))
    elif step == "description":
        await show(target, header + t("wiz.description"), kb(_nav(step, skip=True)))
    elif step == "count":
        await show(
            target,
            header + t("wiz.count"),
            kb([(str(n), WizCB(f=step, v=str(n))) for n in (10, 15, 20, 25, 30)], _nav(step)),
        )
    elif step == "options":
        await show(
            target, header + t("wiz.options"), kb([(str(n), WizCB(f=step, v=str(n))) for n in (3, 4, 5)], _nav(step))
        )
    elif step == "difficulty":
        await show(
            target,
            header + t("wiz.difficulty"),
            kb(
                [(t("difficulty.easy"), WizCB(f=step, v="easy")), (t("difficulty.medium"), WizCB(f=step, v="medium"))],
                [(t("difficulty.hard"), WizCB(f=step, v="hard")), (t("difficulty.mixed"), WizCB(f=step, v="mixed"))],
                _nav(step),
            ),
        )
    elif step == "sources":
        materials = (await material_service.ready_materials(session))[:MAX_MATERIAL_BUTTONS]
        selected = set(wiz.get("material_ids", []))
        use_all = wiz.get("use_all", False)
        rows = [[(("☑️ " if use_all else "⬜️ ") + t("wiz.all_materials"), WizCB(f="src_all"))]]
        for m in materials:
            mark = "✅ " if (m.id in selected and not use_all) else "▫️ "
            rows.append([(mark + truncate(m.title, 40), WizCB(f="src", v=str(m.id)))])
        rows.append([(t("wiz.btn.next"), WizCB(f="src_done"))])
        text = header + t("wiz.sources", n=len(selected) if not use_all else len(materials))
        if not materials:
            text += "\n\n" + t("wiz.no_materials")
        await show(target, text, kb(*rows, _nav(step)))
    elif step == "news":
        await show(
            target,
            header + t("wiz.news", n=wiz.get("news_available", 0)),
            kb([(f"{n}%", WizCB(f=step, v=str(n))) for n in (0, 10, 20, 30, 50, 100)], _nav(step)),
        )
    elif step == "focus":
        await show(target, header + t("wiz.focus"), kb(_nav(step, skip=True)))
    elif step == "audience":
        groups = await group_service.list_groups(session, active_only=True)
        rows = [[(t("wiz.all_employees"), WizCB(f=step, v="0"))]]
        rows += [[(f"👥 {truncate(g.title, 40)} ({n})", WizCB(f=step, v=str(g.id)))] for g, n in groups]
        await show(target, header + t("wiz.audience"), kb(*rows, _nav(step)))
    elif step == "start":
        await show(target, header + t("wiz.start"), kb([(t("wiz.now"), WizCB(f=step, v="now"))], _nav(step)))
    elif step == "duration":
        await show(
            target,
            header + t("wiz.duration"),
            kb(
                [(fmt_hours(h), WizCB(f=step, v=str(h))) for h in (12, 24, 48)],
                [(fmt_hours(h), WizCB(f=step, v=str(h))) for h in (72, 168)],
                _nav(step),
            ),
        )
    elif step in ("rand_q", "rand_o", "retakes"):
        await show(target, header + t(f"wiz.{step}"), _yes_no(step))
    elif step == "reveal":
        await show(
            target,
            header + t("wiz.reveal"),
            kb(
                [(t("reveal.immediate"), WizCB(f=step, v="immediate"))],
                [(t("reveal.after"), WizCB(f=step, v="after"))],
                [(t("reveal.never"), WizCB(f=step, v="never"))],
                _nav(step),
            ),
        )
    elif step == "passing":
        await show(
            target,
            header + t("wiz.passing"),
            kb([(f"{n}%", WizCB(f=step, v=str(n))) for n in (50, 60, 70, 80, 90)], _nav(step)),
        )
    elif step == "confirm":
        await show(
            target,
            await _summary(session, wiz),
            kb([(t("wiz.btn.create"), WizCB(f="confirm", v="1"))], _nav(step)),
        )


async def _summary(session: AsyncSession, wiz: dict) -> str:
    starts = parse_local_datetime(wiz["start_local"]) if wiz.get("start_local") else utcnow()
    assert starts is not None
    deadline = starts + timedelta(hours=float(wiz["duration_hours"]))
    if wiz.get("use_all"):
        sources = t("wiz.all_materials")
    else:
        titles = [
            m.title for m in await material_service.ready_materials(session) if m.id in set(wiz.get("material_ids", []))
        ]
        sources = ", ".join(esc(truncate(x, 40)) for x in titles) or "—"
    audience = t("wiz.all_employees")
    if wiz.get("group_id"):
        groups = {g.id: g.title for g, _ in await group_service.list_groups(session)}
        audience = esc(groups.get(wiz["group_id"], "—"))
    yn = {True: t("common.yes"), False: t("common.no")}
    return t(
        "wiz.summary",
        title=esc(wiz["title"]),
        description=esc(wiz.get("description") or "—"),
        count=wiz["count"],
        options=wiz["options"],
        difficulty=t(f"difficulty.{wiz['difficulty']}"),
        sources=sources,
        news=wiz.get("news", 0),
        focus=esc(wiz.get("focus") or "—"),
        audience=audience,
        start=fmt_dt(starts),
        deadline=fmt_dt(deadline),
        duration=fmt_hours(float(wiz["duration_hours"])),
        rand_q=yn[bool(wiz.get("rand_q"))],
        rand_o=yn[bool(wiz.get("rand_o"))],
        reveal=t(f"reveal.{wiz['reveal']}"),
        retakes=yn[bool(wiz.get("retakes"))],
        passing=wiz["passing"],
    )


async def _set(state: FSMContext, **values) -> dict:
    data = await state.get_data()
    wiz = dict(data.get("wiz", {}))
    wiz.update(values)
    await state.update_data(wiz=wiz)
    return wiz


async def _next(target, state: FSMContext, session: AsyncSession, step: str) -> None:
    index = STEPS.index(step) + 1
    data = await state.get_data()
    wiz = data.get("wiz", {})
    # Skip the news step when no active news exist.
    if index < len(STEPS) and STEPS[index] == "news" and not wiz.get("news_available"):
        await _set(state, news=0)
        index += 1
    await _prompt(target, state, session, STEPS[index])


@router.callback_query(AdminCB.filter(F.s == "tst_new"))
async def cb_new(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    if not ai_available():
        await callback.answer(t("ai.not_configured_short"), show_alert=False)
    news_available = len(await material_service.active_news_ids(session))
    await state.update_data(wiz={"news_available": news_available, "options": 4, "difficulty": "mixed"})
    await _prompt(callback, state, session, "title")


@router.callback_query(WizCB.filter(F.f == "_back"))
async def cb_back(callback: CallbackQuery, callback_data: WizCB, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    if "wiz" not in data:
        await callback.answer(t("errors.stale_button"))
        return
    index = max(0, STEPS.index(callback_data.v) - 1)
    if STEPS[index] == "news" and not data["wiz"].get("news_available"):
        index -= 1
    await _prompt(callback, state, session, STEPS[index])


@router.message(WizardStates.text_input, F.text)
async def on_text(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    step = data.get("step")
    value = (message.text or "").strip()
    if step == "title":
        if not 3 <= len(value) <= 255:
            await message.answer(t("wiz.err.title"))
            return
        await _set(state, title=" ".join(value.split()))
    elif step == "description":
        await _set(state, description=value[:2000])
    elif step == "count":
        if not value.isdigit() or not 1 <= int(value) <= 100:
            await message.answer(t("wiz.err.count"))
            return
        await _set(state, count=int(value))
    elif step == "news":
        value = value.rstrip("%").strip()
        if not value.isdigit() or not 0 <= int(value) <= 100:
            await message.answer(t("wiz.err.percent"))
            return
        await _set(state, news=int(value))
    elif step == "focus":
        await _set(state, focus=value[:300])
    elif step == "start":
        parsed = parse_local_datetime(value)
        if parsed is None:
            await message.answer(t("wiz.err.datetime"))
            return
        if parsed < utcnow() - timedelta(minutes=5):
            await message.answer(t("wiz.err.past"))
            return
        await _set(state, start_local=value)
    elif step == "duration":
        parsed = parse_local_datetime(value)
        wiz = data.get("wiz", {})
        start = parse_local_datetime(wiz["start_local"]) if wiz.get("start_local") else utcnow()
        assert start is not None
        if parsed is not None:
            hours = (parsed - start).total_seconds() / 3600
        else:
            try:
                hours = float(value.replace(",", "."))
            except ValueError:
                await message.answer(t("wiz.err.duration"))
                return
        if not 0.25 <= hours <= 24 * 60:
            await message.answer(t("wiz.err.duration"))
            return
        await _set(state, duration_hours=round(hours, 2))
    elif step == "passing":
        value = value.rstrip("%").strip()
        if not value.isdigit() or not 0 <= int(value) <= 100:
            await message.answer(t("wiz.err.percent"))
            return
        await _set(state, passing=int(value))
    else:
        await message.answer(t("common.unexpected_input"))
        return
    await _next(message, state, session, step)


@router.callback_query(WizCB.filter(F.f.in_({"src", "src_all", "src_done"})))
async def cb_sources(callback: CallbackQuery, callback_data: WizCB, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    if data.get("step") != "sources":
        await callback.answer(t("errors.stale_button"))
        return
    wiz = data["wiz"]
    if callback_data.f == "src_all":
        await _set(state, use_all=not wiz.get("use_all", False))
    elif callback_data.f == "src":
        selected = set(wiz.get("material_ids", []))
        mid = int(callback_data.v)
        selected.symmetric_difference_update({mid})
        await _set(state, material_ids=sorted(selected), use_all=False)
    else:
        if not wiz.get("use_all") and not wiz.get("material_ids"):
            if not wiz.get("news_available"):
                await callback.answer(t("wiz.err.no_sources"), show_alert=True)
                return
            await _set(state, news=100)
            await callback.answer(t("wiz.news_only"), show_alert=True)
            await _prompt(callback, state, session, "focus")
            return
        await _next(callback, state, session, "sources")
        return
    await _prompt(callback, state, session, "sources")


@router.callback_query(WizCB.filter())
async def cb_choice(
    callback: CallbackQuery,
    callback_data: WizCB,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    user: User,
    session_maker,
) -> None:
    data = await state.get_data()
    step = callback_data.f
    if "wiz" not in data or data.get("step") != step:
        await callback.answer(t("errors.stale_button"))
        return
    value = callback_data.v
    if value == "_skip":
        if step == "description":
            await _set(state, description=None)
        elif step == "focus":
            await _set(state, focus=None)
    elif step == "count":
        await _set(state, count=int(value))
    elif step == "options":
        await _set(state, options=int(value))
    elif step == "difficulty":
        await _set(state, difficulty=TestDifficulty(value).value)
    elif step == "news":
        await _set(state, news=int(value))
    elif step == "audience":
        await _set(state, group_id=int(value) or None)
    elif step == "start":
        await _set(state, start_local=None)
    elif step == "duration":
        await _set(state, duration_hours=float(value))
    elif step in ("rand_q", "rand_o", "retakes"):
        await _set(state, **{step: value == "1"})
    elif step == "reveal":
        await _set(state, reveal=value)
    elif step == "passing":
        await _set(state, passing=int(value))
    elif step == "confirm":
        await _create(callback, state, session, session_maker, bot, user)
        return
    else:
        await callback.answer(t("errors.stale_button"))
        return
    await _next(callback, state, session, step)


async def _create(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, session_maker, bot: Bot, user: User
) -> None:
    data = await state.get_data()
    wiz = data["wiz"]
    await state.clear()  # prevents duplicate creation on double click
    starts = parse_local_datetime(wiz["start_local"]) if wiz.get("start_local") else utcnow()
    assert starts is not None
    draft = TestDraftData(
        title=wiz["title"],
        description=wiz.get("description"),
        question_count=int(wiz["count"]),
        option_count=int(wiz.get("options", 4)),
        difficulty=TestDifficulty(wiz.get("difficulty", "mixed")),
        material_ids=list(wiz.get("material_ids", [])),
        use_all_materials=bool(wiz.get("use_all")),
        news_percent=int(wiz.get("news", 0)),
        focus_query=wiz.get("focus"),
        group_id=wiz.get("group_id"),
        starts_at=starts,
        deadline_at=starts + timedelta(hours=float(wiz["duration_hours"])),
        randomize_questions=bool(wiz.get("rand_q")),
        randomize_options=bool(wiz.get("rand_o")),
        answer_reveal=wiz.get("reveal", "after"),
        allow_retakes=bool(wiz.get("retakes")),
        passing_percent=int(wiz.get("passing", 60)),
    )
    try:
        test = await create_test(session, draft, user.id)
    except TestStateError as exc:
        await callback.answer(t(f"test_error.{exc.code}"), show_alert=True)
        return
    await callback.answer(t("wiz.created"))
    from app.handlers.admin.tests import start_assembly

    await start_assembly(callback, session_maker, bot, test.id)
