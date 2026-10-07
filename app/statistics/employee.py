"""Individual employee statistics, topic analysis and wrong-answer history."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AttemptStatus,
    GroupMember,
    Test,
    TestAttempt,
    TestQuestion,
    TestStatus,
    User,
    UserAnswer,
)


@dataclass
class TopicStat:
    topic: str
    total: int
    correct: int

    @property
    def percent(self) -> float:
        return round(self.correct / self.total * 100, 1) if self.total else 0.0


@dataclass
class EmployeeStats:
    user: User
    total_tests: int = 0  # tests the employee was expected to take (published, in audience)
    attempted_tests: int = 0
    completed_tests: int = 0
    expired_tests: int = 0
    missed_tests: int = 0
    average_score: float | None = None
    best_score: float | None = None
    worst_score: float | None = None
    total_questions: int = 0
    correct: int = 0
    incorrect: int = 0
    total_time_seconds: int = 0
    weak_topics: list[TopicStat] = field(default_factory=list)
    strong_topics: list[TopicStat] = field(default_factory=list)
    recent: list[TestAttempt] = field(default_factory=list)


async def topic_stats(
    session: AsyncSession, user_id: int | None = None, since: datetime | None = None
) -> list[TopicStat]:
    topic = func.coalesce(func.nullif(TestQuestion.topic_name, ""), "—")
    stmt = (
        select(
            topic.label("topic"),
            func.count(UserAnswer.id),
            func.sum(case((UserAnswer.is_correct.is_(True), 1), else_=0)),
        )
        .join(TestQuestion, TestQuestion.id == UserAnswer.test_question_id)
        .join(TestAttempt, TestAttempt.id == UserAnswer.attempt_id)
        .where(TestAttempt.status != AttemptStatus.CANCELLED)
        .group_by(topic)
    )
    if user_id is not None:
        stmt = stmt.where(UserAnswer.user_id == user_id)
    if since is not None:
        stmt = stmt.where(UserAnswer.answered_at >= since)
    return [TopicStat(t, int(n), int(c or 0)) for t, n, c in await session.execute(stmt)]


def split_topics(stats: list[TopicStat], min_answers: int, limit: int = 5) -> tuple[list[TopicStat], list[TopicStat]]:
    eligible = [s for s in stats if s.total >= min_answers]
    weak = sorted([s for s in eligible if s.percent < 80], key=lambda s: (s.percent, -s.total))[:limit]
    strong = sorted([s for s in eligible if s.percent >= 80], key=lambda s: (-s.percent, -s.total))[:limit]
    return weak, strong


async def employee_stats(session: AsyncSession, user: User, min_topic_answers: int = 3) -> EmployeeStats:
    stats = EmployeeStats(user=user)
    attempts = list(
        (
            await session.execute(
                select(TestAttempt)
                .where(TestAttempt.user_id == user.id, TestAttempt.status != AttemptStatus.CANCELLED)
                .order_by(TestAttempt.started_at.desc())
            )
        )
        .scalars()
        .all()
    )
    finished = [a for a in attempts if a.status in (AttemptStatus.COMPLETED, AttemptStatus.EXPIRED)]
    completed = [a for a in attempts if a.status == AttemptStatus.COMPLETED]
    tests_attempted = {a.test_id for a in attempts}
    tests_completed = {a.test_id for a in completed}
    tests_expired = {a.test_id for a in attempts if a.status == AttemptStatus.EXPIRED} - tests_completed
    stats.attempted_tests = len(tests_attempted)
    stats.completed_tests = len(tests_completed)
    stats.expired_tests = len(tests_expired)

    # Expected tests: published tests whose audience included the employee.
    group_ids = set(
        (
            await session.execute(
                select(GroupMember.group_id).where(GroupMember.user_id == user.id, GroupMember.is_member.is_(True))
            )
        ).scalars()
    )
    published = (
        await session.execute(
            select(Test.id, Test.group_id, Test.status).where(
                Test.status.in_([TestStatus.ACTIVE, TestStatus.EXPIRED, TestStatus.CLOSED]),
                Test.deadline_at > user.created_at,  # tests the employee could actually have taken
            )
        )
    ).all()
    expected = {tid for tid, gid, _ in published if gid is None or gid in group_ids} | tests_attempted
    stats.total_tests = len(expected)
    ended = {tid for tid, _, st in published if st in (TestStatus.EXPIRED, TestStatus.CLOSED)}
    stats.missed_tests = len({t for t in expected if t in ended} - tests_attempted)

    best_per_test: dict[int, float] = {}
    for a in finished:
        best_per_test[a.test_id] = max(best_per_test.get(a.test_id, 0.0), float(a.score_percent))
    if best_per_test:
        scores = list(best_per_test.values())
        stats.average_score = round(sum(scores) / len(scores), 1)
        stats.best_score = max(scores)
        stats.worst_score = min(scores)
    stats.total_questions = sum(a.total_questions for a in finished)
    stats.correct = sum(a.correct_count for a in finished)
    stats.incorrect = sum(a.incorrect_count for a in finished)
    stats.total_time_seconds = sum(a.duration_seconds or 0 for a in finished)
    stats.weak_topics, stats.strong_topics = split_topics(await topic_stats(session, user.id), min_topic_answers)
    stats.recent = attempts[:5]
    return stats


@dataclass
class WrongAnswerRow:
    answer: UserAnswer
    question: TestQuestion
    test_title: str
    attempt: TestAttempt


async def wrong_answers(
    session: AsyncSession, user_id: int, page: int, page_size: int = 5
) -> tuple[list[WrongAnswerRow], int]:
    base = (
        select(UserAnswer, TestQuestion, Test.title, TestAttempt)
        .join(TestQuestion, TestQuestion.id == UserAnswer.test_question_id)
        .join(TestAttempt, TestAttempt.id == UserAnswer.attempt_id)
        .join(Test, Test.id == TestAttempt.test_id)
        .where(UserAnswer.user_id == user_id, UserAnswer.is_correct.is_(False))
    )
    total = (
        await session.execute(
            select(func.count(UserAnswer.id)).where(UserAnswer.user_id == user_id, UserAnswer.is_correct.is_(False))
        )
    ).scalar_one()
    rows = await session.execute(base.order_by(UserAnswer.answered_at.desc()).offset(page * page_size).limit(page_size))
    return [WrongAnswerRow(a, q, title, att) for a, q, title, att in rows], int(total)
