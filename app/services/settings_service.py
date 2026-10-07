"""Runtime settings stored in the ``bot_settings`` table (editable from Telegram)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BotSetting


@dataclass(frozen=True)
class SettingSpec:
    key: str
    default: object
    kind: str  # bool | int
    min_value: int | None = None
    max_value: int | None = None


SPECS: dict[str, SettingSpec] = {
    s.key: s
    for s in [
        SettingSpec("auto_approve_group_members", True, "bool"),
        SettingSpec("auto_approve_all", False, "bool"),
        SettingSpec("notify_employees_dm", True, "bool"),
        SettingSpec("announce_in_groups", True, "bool"),
        SettingSpec("ranking_min_attempts", 1, "int", 1, 50),
        SettingSpec("reminder_hours_before", 3, "int", 0, 72),
        SettingSpec("weak_topic_min_answers", 3, "int", 1, 50),
        SettingSpec("auto_generate_questions", 20, "int", 0, 50),
    ]
}


async def get_all(session: AsyncSession) -> dict[str, object]:
    values = {key: spec.default for key, spec in SPECS.items()}
    rows = (await session.execute(select(BotSetting))).scalars().all()
    for row in rows:
        if row.key in SPECS:
            values[row.key] = row.value
    return values


async def get_value(session: AsyncSession, key: str) -> object:
    spec = SPECS[key]
    row = await session.get(BotSetting, key)
    return spec.default if row is None else row.value


async def set_value(session: AsyncSession, key: str, value: object) -> object:
    spec = SPECS[key]
    if spec.kind == "bool":
        value = bool(value)
    else:
        value = int(value)  # type: ignore[arg-type]
        if spec.min_value is not None:
            value = max(spec.min_value, value)
        if spec.max_value is not None:
            value = min(spec.max_value, value)
    stmt = insert(BotSetting).values(key=key, value=value)
    stmt = stmt.on_conflict_do_update(index_elements=[BotSetting.key], set_={"value": value})
    await session.execute(stmt)
    await session.commit()
    return value
