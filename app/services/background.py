"""Tracked fire-and-forget tasks (references kept so they are not garbage-collected)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

_tasks: set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.error("Background task failed", extra={"task": name}, exc_info=exc)

    task.add_done_callback(_done)
    return task


async def shutdown(timeout: float = 10.0) -> None:
    if not _tasks:
        return
    pending = list(_tasks)
    _, still = await asyncio.wait(pending, timeout=timeout)
    for task in still:
        task.cancel()


def running_count() -> int:
    return len(_tasks)
