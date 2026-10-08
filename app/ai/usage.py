"""AI request and token accounting (input/output tokens per job and per day).

``track()`` measures one job (e.g. "generate 20 questions from this file"); every Groq call made
inside it — also from tasks it starts — is added to that job and to today's totals.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date

logger = logging.getLogger(__name__)


@dataclass
class Usage:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hits: int = 0
    by_purpose: dict[str, int] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, purpose: str, input_tokens: int, output_tokens: int) -> None:
        self.requests += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.by_purpose[purpose] = self.by_purpose.get(purpose, 0) + 1


_job: ContextVar[Usage | None] = ContextVar("ai_job_usage", default=None)
_purpose: ContextVar[str] = ContextVar("ai_purpose", default="")
_daily: dict[date, Usage] = {}


def today() -> Usage:
    """Totals since midnight (UTC) in this bot process."""
    key = date.today()
    for old in [d for d in _daily if d != key]:
        del _daily[old]
    return _daily.setdefault(key, Usage())


@contextlib.contextmanager
def track() -> Iterator[Usage]:
    usage = Usage()
    token = _job.set(usage)
    try:
        yield usage
    finally:
        _job.reset(token)


@contextlib.contextmanager
def purpose(name: str) -> Iterator[None]:
    token = _purpose.set(name)
    try:
        yield
    finally:
        _purpose.reset(token)


def record(model: str, input_tokens: int, output_tokens: int) -> None:
    name = _purpose.get() or "other"
    for usage in (_job.get(), today()):
        if usage is not None:
            usage.add(name, input_tokens, output_tokens)
    logger.info(
        "AI request",
        extra={"purpose": name, "model": model, "input_tokens": input_tokens, "output_tokens": output_tokens,
               "total_tokens": input_tokens + output_tokens},
    )  # fmt: skip


def record_cache_hit() -> None:
    for usage in (_job.get(), today()):
        if usage is not None:
            usage.cache_hits += 1
