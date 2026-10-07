"""Organisation-wide statistics for the admin dashboard."""

from __future__ import annotations

from dataclasses import dataclass, field

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
    UserRole,
    UserStatus,
)
from app.statistics.employee import TopicStat, split_topics, topic_stats


@dataclass
class QuestionDifficultyRow:
    question: TestQuestion
    test_title: str
    answers: int
    correct: int

    @property
    def percent(self) -> float:
        return round(self.correct / self.answers * 100, 1) if self.answers else 0.0


@dataclass
class CommonMistakeRow:
    question: TestQuestion
    wrong_option: str
    times: int


@dataclass
class Dashboard:
    total_employees: int = 0
    active_employees: int = 0
    pending_employees: int = 0
    tests_total: int = 0
    tests_by_status: dict[str, int] = field(default_factory=dict)
    attempts_completed: int = 0
    attempts_in_progress: int = 0
    attempts_expired: int = 0
    average_score: float | None = None
    participation_rate: float | None = None
    answers_total: int = 0
    answers_correct: int = 0
    weak_topics: list[TopicStat] = field(default_factory=list)


async def dashboard(session: AsyncSession, min_topic_answers: int = 3) -> Dashboard:
    d = Dashboard()
    for status, n in await session.execute(
        select(User.status, func.count(User.id)).where(User.role == UserRole.EMPLOYEE).group_by(User.status)
    ):
        d.total_employees += n
        if status == UserStatus.ACTIVE:
            d.active_employees = n
        elif status == UserStatus.PENDING:
            d.pending_employees = n
    for status, n in await session.execute(select(Test.status, func.count(Test.id)).group_by(Test.status)):
        d.tests_by_status[status.value] = n
        d.tests_total += n
    for status, n in await session.execute(
        select(TestAttempt.status, func.count(TestAttempt.id)).group_by(TestAttempt.status)
    ):
        if status == AttemptStatus.COMPLETED:
            d.attempts_completed = n
        elif status == AttemptStatus.IN_PROGRESS:
            d.attempts_in_progress = n
        elif status == AttemptStatus.EXPIRED:
            d.attempts_expired = n
    avg = (
        await session.execute(
            select(func.avg(TestAttempt.score_percent)).where(TestAttempt.status == AttemptStatus.COMPLETED)
        )
    ).scalar_one()
    d.average_score = round(float(avg), 1) if avg is not None else None
    total, correct = (
        await session.execute(
            select(func.count(UserAnswer.id), func.sum(case((UserAnswer.is_correct.is_(True), 1), else_=0)))
        )
    ).one()
    d.answers_total, d.answers_correct = int(total), int(correct or 0)
    d.participation_rate = await participation_rate(session)
    d.weak_topics, _ = split_topics(await topic_stats(session), min_topic_answers, limit=5)
    return d


async def participation_rate(session: AsyncSession, last_tests: int = 20) -> float | None:
    """Participated (employee, test) pairs / expected pairs over the latest published tests."""
    tests = (
        await session.execute(
            select(Test.id, Test.group_id)
            .where(Test.status.in_([TestStatus.ACTIVE, TestStatus.EXPIRED, TestStatus.CLOSED]))
            .order_by(Test.starts_at.desc())
            .limit(last_tests)
        )
    ).all()
    if not tests:
        return None
    active = select(User.id).where(User.role == UserRole.EMPLOYEE, User.status == UserStatus.ACTIVE)
    active_ids = set((await session.execute(active)).scalars())
    group_ids = {gid for _, gid in tests if gid}
    members: dict[int, set[int]] = {}
    if group_ids:
        for gid, uid in await session.execute(
            select(GroupMember.group_id, GroupMember.user_id).where(
                GroupMember.group_id.in_(group_ids), GroupMember.is_member.is_(True)
            )
        ):
            members.setdefault(gid, set()).add(uid)
    participated = set(
        (
            await session.execute(
                select(TestAttempt.test_id, TestAttempt.user_id)
                .where(TestAttempt.test_id.in_([t for t, _ in tests]), TestAttempt.status != AttemptStatus.CANCELLED)
                .distinct()
            )
        ).all()
    )
    expected = 0
    hit = 0
    for tid, gid in tests:
        audience = active_ids & members.get(gid, set()) if gid else active_ids
        expected += len(audience)
        hit += sum(1 for uid in audience if (tid, uid) in participated)
    return round(hit / expected * 100, 1) if expected else None


async def hardest_questions(
    session: AsyncSession, limit: int = 10, min_answers: int = 3
) -> list[QuestionDifficultyRow]:
    correct = func.sum(case((UserAnswer.is_correct.is_(True), 1), else_=0))
    stmt = (
        select(TestQuestion, Test.title, func.count(UserAnswer.id), correct)
        .join(UserAnswer, UserAnswer.test_question_id == TestQuestion.id)
        .join(Test, Test.id == TestQuestion.test_id)
        .group_by(TestQuestion.id, Test.title)
        .having(func.count(UserAnswer.id) >= min_answers)
        .order_by((correct * 1.0 / func.count(UserAnswer.id)).asc(), func.count(UserAnswer.id).desc())
        .limit(limit)
    )
    return [QuestionDifficultyRow(q, title, int(n), int(c or 0)) for q, title, n, c in await session.execute(stmt)]


async def common_mistakes(session: AsyncSession, limit: int = 10, test_id: int | None = None) -> list[CommonMistakeRow]:
    stmt = (
        select(TestQuestion, UserAnswer.selected_option, func.count(UserAnswer.id))
        .join(UserAnswer, UserAnswer.test_question_id == TestQuestion.id)
        .where(UserAnswer.is_correct.is_(False))
        .group_by(TestQuestion.id, UserAnswer.selected_option)
        .order_by(func.count(UserAnswer.id).desc())
        .limit(limit)
    )
    if test_id is not None:
        stmt = stmt.where(TestQuestion.test_id == test_id)
    return [CommonMistakeRow(q, opt, int(n)) for q, opt, n in await session.execute(stmt)]


async def test_question_stats(session: AsyncSession, test_id: int) -> list[QuestionDifficultyRow]:
    correct = func.sum(case((UserAnswer.is_correct.is_(True), 1), else_=0))
    stmt = (
        select(TestQuestion, Test.title, func.count(UserAnswer.id), correct)
        .join(Test, Test.id == TestQuestion.test_id)
        .outerjoin(UserAnswer, UserAnswer.test_question_id == TestQuestion.id)
        .where(TestQuestion.test_id == test_id)
        .group_by(TestQuestion.id, Test.title)
        .order_by(TestQuestion.position)
    )
    return [QuestionDifficultyRow(q, title, int(n), int(c or 0)) for q, title, n, c in await session.execute(stmt)]
