"""Notice when the same bot token is polled from somewhere else (TelegramConflictError).

Telegram gives updates to only one getUpdates caller at a time: with a second copy of the bot
running (a computer, another Railway service/project, an old host) both copies win in turns, so
the bot answers only now and then and seems to freeze. aiogram just logs the error and retries;
this handler counts those log lines and tells the admins once an hour, with what to do.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable

CONFLICT_MARK = "TelegramConflictError"
WINDOW_SECONDS = 180.0
THRESHOLD = 6  # conflicts within the window: not just the few seconds of a redeploy overlap
NOTIFY_EVERY_SECONDS = 3600.0


class ConflictWatch(logging.Handler):
    def __init__(self, notify: Callable[[], Awaitable[None]]):
        super().__init__(level=logging.ERROR)
        self._notify = notify
        self._seen: deque[float] = deque()
        self._last_notice: float | None = None  # monotonic time may start near 0 after boot
        self.active = False  # shown by /health

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if CONFLICT_MARK not in record.getMessage():
                return
            now = time.monotonic()
            self._seen.append(now)
            while self._seen and now - self._seen[0] > WINDOW_SECONDS:
                self._seen.popleft()
            self.active = len(self._seen) >= THRESHOLD
            if self.active and (self._last_notice is None or now - self._last_notice >= NOTIFY_EVERY_SECONDS):
                self._last_notice = now
                from app.services import background

                background.spawn(self._notify(), name="notify-polling-conflict")
        except Exception:  # pragma: no cover - logging must never raise
            self.handleError(record)

    def recent(self) -> bool:
        now = time.monotonic()
        return bool(self._seen) and now - self._seen[-1] < WINDOW_SECONDS and self.active


_watch: ConflictWatch | None = None


def install(notify: Callable[[], Awaitable[None]]) -> ConflictWatch:
    global _watch
    _watch = ConflictWatch(notify)
    logging.getLogger("aiogram").addHandler(_watch)
    return _watch


def conflict_now() -> bool:
    return _watch.recent() if _watch is not None else False
