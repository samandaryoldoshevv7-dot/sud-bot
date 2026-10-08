"""Results per source document ("📚 MANBA BO'YICHA NATIJA")."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TestAttempt, TestQuestion, TestSource, UserAnswer


@dataclass
class SourceRow:
    name: str
    total: int  # questions from this source
    correct: int
    answered: int = 0

    @property
    def percent(self) -> float:
        return round(self.correct / self.total * 100, 1) if self.total else 0.0


async def _ordered_names(session: AsyncSession, test_id: int) -> list[str]:
    rows = await session.execute(
        select(TestSource.source_name).where(TestSource.test_id == test_id).order_by(TestSource.position, TestSource.id)
    )
    return list(rows.scalars().all())


def _sorted(rows: dict[str, SourceRow], order: list[str]) -> list[SourceRow]:
    rank = {name: i for i, name in enumerate(order)}
    return sorted(rows.values(), key=lambda r: (rank.get(r.name, len(rank)), r.name))


async def attempt_source_breakdown(session: AsyncSession, attempt_id: int) -> list[SourceRow]:
    """One employee: questions and correct answers per source (unanswered questions count as not correct)."""
    attempt = await session.get(TestAttempt, attempt_id)
    if attempt is None:
        return []
    tq_ids = [item["tq"] for item in attempt.layout]
    names = dict(
        (
            await session.execute(select(TestQuestion.id, TestQuestion.source_name).where(TestQuestion.id.in_(tq_ids)))
        ).all()
    )
    answers = dict(
        (
            await session.execute(
                select(UserAnswer.test_question_id, UserAnswer.is_correct).where(UserAnswer.attempt_id == attempt_id)
            )
        ).all()
    )
    rows: dict[str, SourceRow] = {}
    for tq_id in tq_ids:
        name = names.get(tq_id) or "—"
        row = rows.setdefault(name, SourceRow(name, 0, 0))
        row.total += 1
        if tq_id in answers:
            row.answered += 1
            row.correct += int(bool(answers[tq_id]))
    return _sorted(rows, await _ordered_names(session, attempt.test_id))


async def test_source_breakdown(session: AsyncSession, test_id: int) -> list[SourceRow]:
    """Whole test (admin): answers and correct answers per source over all employees."""
    stmt = (
        select(
            TestQuestion.source_name,
            func.count(UserAnswer.id),
            func.count(UserAnswer.id).filter(UserAnswer.is_correct.is_(True)),
        )
        .join(UserAnswer, UserAnswer.test_question_id == TestQuestion.id)
        .where(TestQuestion.test_id == test_id)
        .group_by(TestQuestion.source_name)
    )
    rows = {(name or "—"): SourceRow(name or "—", int(n), int(c), int(n)) for name, n, c in await session.execute(stmt)}
    return _sorted(rows, await _ordered_names(session, test_id))
