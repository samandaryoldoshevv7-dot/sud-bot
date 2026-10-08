"""Per-test participation tracking (the core admin view).

Audience = active employees (or the members of the test's group). Every audience member falls
into exactly one bucket based on their best attempt:
COMPLETED > IN_PROGRESS > EXPIRED (started, did not finish) > CANCELLED > NOT_STARTED.
Employees outside the current audience who nevertheless have attempts are included too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AttemptStatus,
    ParticipationStatus,
    Test,
    TestAttempt,
    User,
    UserRole,
)

_PRIORITY = {
    AttemptStatus.COMPLETED: 4,
    AttemptStatus.IN_PROGRESS: 3,
    AttemptStatus.EXPIRED: 2,
    AttemptStatus.CANCELLED: 1,
}


@dataclass
class ParticipantRow:
    user: User
    status: ParticipationStatus
    attempt: TestAttempt | None = None
    attempts_count: int = 0

    @property
    def score(self) -> float | None:
        return (
            float(self.attempt.score_percent)
            if self.attempt and self.attempt.status != AttemptStatus.IN_PROGRESS
            else None
        )


@dataclass
class ParticipationSummary:
    test: Test
    rows: list[ParticipantRow] = field(default_factory=list)

    def by_status(self, status: ParticipationStatus) -> list[ParticipantRow]:
        return [r for r in self.rows if r.status == status]

    @property
    def total(self) -> int:
        return len(self.rows)

    def count(self, status: ParticipationStatus) -> int:
        return sum(1 for r in self.rows if r.status == status)

    @property
    def participated(self) -> int:
        return self.total - self.count(ParticipationStatus.NOT_STARTED)

    @property
    def started_not_finished(self) -> int:
        return (
            self.count(ParticipationStatus.IN_PROGRESS)
            + self.count(ParticipationStatus.EXPIRED)
            + self.count(ParticipationStatus.CANCELLED)
        )

    @property
    def participation_rate(self) -> float:
        return round(self.participated / self.total * 100, 1) if self.total else 0.0

    @property
    def completion_rate(self) -> float:
        return round(self.count(ParticipationStatus.COMPLETED) / self.total * 100, 1) if self.total else 0.0

    @property
    def average_score(self) -> float | None:
        scores = [r.score for r in self.rows if r.status == ParticipationStatus.COMPLETED and r.score is not None]
        return round(sum(scores) / len(scores), 1) if scores else None

    @property
    def passed(self) -> int:
        return sum(1 for r in self.rows if r.status == ParticipationStatus.COMPLETED and r.attempt and r.attempt.passed)


def best_attempt(attempts: list[TestAttempt]) -> TestAttempt | None:
    if not attempts:
        return None
    return max(attempts, key=lambda a: (_PRIORITY[a.status], float(a.score_percent), -a.attempt_no))


async def audience(session: AsyncSession, test: Test) -> list[User]:
    from app.services.assignments import audience_users

    return await audience_users(session, test)


async def participation(session: AsyncSession, test: Test, now: datetime | None = None) -> ParticipationSummary:
    users = {u.id: u for u in await audience(session, test)}
    attempts = list((await session.execute(select(TestAttempt).where(TestAttempt.test_id == test.id))).scalars().all())
    by_user: dict[int, list[TestAttempt]] = {}
    for attempt in attempts:
        by_user.setdefault(attempt.user_id, []).append(attempt)
    missing = [uid for uid in by_user if uid not in users]
    if missing:
        for user in (await session.execute(select(User).where(User.id.in_(missing)))).scalars():
            if user.role == UserRole.EMPLOYEE:
                users[user.id] = user

    rows: list[ParticipantRow] = []
    for user in users.values():
        user_attempts = by_user.get(user.id, [])
        best = best_attempt(user_attempts)
        if best is None:
            status = ParticipationStatus.NOT_STARTED
        elif best.status == AttemptStatus.IN_PROGRESS and now is not None and now >= best.deadline_at:
            status = ParticipationStatus.EXPIRED  # scheduler has not swept it yet
        else:
            status = ParticipationStatus(best.status.value)
        rows.append(ParticipantRow(user=user, status=status, attempt=best, attempts_count=len(user_attempts)))

    order = {
        ParticipationStatus.COMPLETED: 0,
        ParticipationStatus.IN_PROGRESS: 1,
        ParticipationStatus.EXPIRED: 2,
        ParticipationStatus.CANCELLED: 3,
        ParticipationStatus.NOT_STARTED: 4,
    }
    rows.sort(key=lambda r: (order[r.status], -(r.score or 0), r.user.display_name.lower()))
    return ParticipationSummary(test=test, rows=rows)
