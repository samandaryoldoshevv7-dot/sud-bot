"""Time helpers. Everything is stored in UTC; conversion happens only for display/input."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import get_settings

UZ_MONTHS = [
    "yanvar",
    "fevral",
    "mart",
    "aprel",
    "may",
    "iyun",
    "iyul",
    "avgust",
    "sentabr",
    "oktabr",
    "noyabr",
    "dekabr",
]


def utcnow() -> datetime:
    return datetime.now(UTC)


def local_tz() -> ZoneInfo:
    return get_settings().tz


def to_local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(local_tz())


def fmt_dt(dt: datetime | None, with_year: bool = True) -> str:
    """``07.10.2026 10:00`` in the configured timezone."""
    local = to_local(dt)
    if local is None:
        return "—"
    return local.strftime("%d.%m.%Y %H:%M" if with_year else "%d.%m %H:%M")


def fmt_date_long(dt: datetime | None) -> str:
    local = to_local(dt)
    if local is None:
        return "—"
    return f"{local.day:02d} {UZ_MONTHS[local.month - 1]} {local.year} {local:%H:%M}"


def fmt_duration(seconds: int | float | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(max(0, seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours} soat {minutes} daq {secs} son"
    if minutes:
        return f"{minutes} daq {secs} son"
    return f"{secs} son"


def fmt_hours(hours: float) -> str:
    if hours >= 24 and hours % 24 == 0:
        return f"{int(hours // 24)} kun" if hours != 24 else "24 soat"
    return f"{hours:g} soat"


_DT_PATTERNS = [
    (re.compile(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})\s+(\d{1,2}):(\d{2})$"), "dmy_hm"),
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})$"), "ymd_hm"),
    (re.compile(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})$"), "dmy"),
]


def parse_local_datetime(value: str) -> datetime | None:
    """Parse ``DD.MM.YYYY HH:MM`` (local time) into an aware UTC datetime."""
    value = value.strip()
    for pattern, kind in _DT_PATTERNS:
        m = pattern.match(value)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        try:
            if kind == "dmy_hm":
                naive = datetime(g[2], g[1], g[0], g[3], g[4])
            elif kind == "ymd_hm":
                naive = datetime(g[0], g[1], g[2], g[3], g[4])
            else:
                naive = datetime(g[2], g[1], g[0], 0, 0)
        except ValueError:
            return None
        return naive.replace(tzinfo=local_tz()).astimezone(UTC)
    return None


def local_period_start(period: str, now: datetime | None = None) -> datetime | None:
    """Start (UTC) of the current local day/week/month; ``None`` for all-time."""
    now = now or utcnow()
    local = to_local(now)
    assert local is not None
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        start = midnight
    elif period == "week":
        start = midnight - timedelta(days=local.weekday())
    elif period == "month":
        start = midnight.replace(day=1)
    else:
        return None
    return start.astimezone(UTC)
