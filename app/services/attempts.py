"""Employee test attempts: start, deterministic answer checking, scoring, expiry.

Idempotency guarantees:
* one IN_PROGRESS attempt per (test, user) – enforced by a partial unique index;
* one answer per (attempt, question) – unique constraint + row lock on the attempt;
* a stale/duplicate button press (wrong position) is ignored without side effects.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AttemptStatus,
    DeliveryMode,
    Group,
    GroupMember,
    QuestionOption,
    Test,
    TestAttempt,
    TestQuestion,
    TestStatus,
    User,
    UserAnswer,
    UserStatus,
)
from app.schemas.ai import LETTERS
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


class StartError(str, Enum):
    NOT_FOUND = "not_found"
    NOT_ACTIVE = "not_active"
    NOT_STARTED_YET = "not_started_yet"
    DEADLINE_PASSED = "deadline_passed"
    USER_NOT_ACTIVE = "user_not_active"
    NOT_IN_GROUP = "not_in_group"
    ALREADY_COMPLETED = "already_completed"
    NO_QUESTIONS = "no_questions"
    GROUP_ONLY = "group_only"


@dataclass
class StartResult:
    attempt: TestAttempt | None = None
    error: StartError | None = None
    resumed: bool = False


class AnswerOutcome(str, Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"  # already answered / stale button
    NOT_IN_PROGRESS = "not_in_progress"
    EXPIRED = "expired"
    INVALID = "invalid"


@dataclass
class AnswerResult:
    outcome: AnswerOutcome
    attempt: TestAttempt | None = None
    is_correct: bool | None = None
    question: TestQuestion | None = None
    selected_display: str | None = None
    correct_display: str | None = None
    finished: bool = False


def build_layout(
    questions: list[TestQuestion], randomize_questions: bool, randomize_options: bool, rng: random.Random
) -> list[dict]:
    order = list(questions)
    if randomize_questions:
        rng.shuffle(order)
    layout = []
    for tq in order:
        letters = list(tq.options.keys())
        if randomize_options:
            rng.shuffle(letters)
        layout.append({"tq": tq.id, "opts": letters})
    return layout


def display_to_original(layout_item: dict, display_letter: str) -> str | None:
    """Display letter (what the employee clicked) → original letter stored in the snapshot."""
    try:
        index = LETTERS.index(display_letter)
    except ValueError:
        return None
    opts = layout_item["opts"]
    return opts[index] if index < len(opts) else None


def original_to_display(layout_item: dict, original_letter: str) -> str | None:
    opts = layout_item["opts"]
    return LETTERS[opts.index(original_letter)] if original_letter in opts else None


def displayed_options(tq: TestQuestion, layout_item: dict) -> list[tuple[str, str]]:
    """``[(display_letter, option_text), ...]`` in the order shown to this employee."""
    return [(LETTERS[i], tq.options[orig]) for i, orig in enumerate(layout_item["opts"])]


def compute_percent(correct: int, total: int) -> Decimal:
    if total <= 0:
        return Decimal("0.00")
    return (Decimal(correct) * 100 / Decimal(total)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


async def user_in_test_audience(session: AsyncSession, test: Test, user: User) -> bool:
    if test.group_id is None:
        return True
    stmt = (
        select(func.count())
        .select_from(GroupMember)
        .join(Group, Group.id == GroupMember.group_id)
        .where(GroupMember.group_id == test.group_id, GroupMember.user_id == user.id, GroupMember.is_member.is_(True))
    )
    return bool((await session.execute(stmt)).scalar_one())


async def get_in_progress(session: AsyncSession, test_id: int, user_id: int) -> TestAttempt | None:
    stmt = select(TestAttempt).where(
        TestAttempt.test_id == test_id, TestAttempt.user_id == user_id, TestAttempt.status == AttemptStatus.IN_PROGRESS
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def start_attempt(
    session: AsyncSession,
    user: User,
    test_id: int,
    chat_id: int | None = None,
    now: datetime | None = None,
    rng: random.Random | None = None,
    layout: list[dict] | None = None,
    from_group: bool = False,
) -> StartResult:
    now = now or utcnow()
    rng = rng or random.SystemRandom()
    user_id = user.id
    test = await session.get(Test, test_id)
    if test is None or test.status == TestStatus.DRAFT:
        return StartResult(error=StartError.NOT_FOUND)
    if user.status != UserStatus.ACTIVE:
        return StartResult(error=StartError.USER_NOT_ACTIVE)

    existing = await get_in_progress(session, test_id, user.id)
    if existing is not None:
        if now >= existing.deadline_at:
            await finalize_attempt(session, existing, AttemptStatus.EXPIRED, now)
            await session.commit()
            return StartResult(error=StartError.DEADLINE_PASSED)
        return StartResult(attempt=existing, resumed=True)

    if test.status == TestStatus.READY and now < test.starts_at:
        return StartResult(error=StartError.NOT_STARTED_YET)
    if test.status in (TestStatus.EXPIRED, TestStatus.CLOSED) or now >= test.deadline_at:
        return StartResult(error=StartError.DEADLINE_PASSED)
    if test.status != TestStatus.ACTIVE:
        return StartResult(error=StartError.NOT_ACTIVE)
    if now < test.starts_at:
        return StartResult(error=StartError.NOT_STARTED_YET)
    if test.delivery_mode == DeliveryMode.GROUP and not from_group:
        return StartResult(error=StartError.GROUP_ONLY)
    if not await user_in_test_audience(session, test, user):
        return StartResult(error=StartError.NOT_IN_GROUP)

    previous = (
        await session.execute(
            select(TestAttempt.status, TestAttempt.attempt_no).where(
                TestAttempt.test_id == test_id, TestAttempt.user_id == user.id
            )
        )
    ).all()
    # One answer per (user, test, question) is final, so a finished attempt can never be retaken.
    if any(st != AttemptStatus.IN_PROGRESS for st, _ in previous):
        return StartResult(error=StartError.ALREADY_COMPLETED)
    attempt_no = max((n for _, n in previous), default=0) + 1

    questions = list(
        (
            await session.execute(
                select(TestQuestion).where(TestQuestion.test_id == test_id).order_by(TestQuestion.position)
            )
        )
        .scalars()
        .all()
    )
    if not questions:
        return StartResult(error=StartError.NO_QUESTIONS)

    attempt = TestAttempt(
        test_id=test_id,
        user_id=user.id,
        attempt_no=attempt_no,
        status=AttemptStatus.IN_PROGRESS,
        started_at=now,
        deadline_at=test.deadline_at,
        total_questions=len(questions),
        layout=layout or build_layout(questions, test.randomize_questions, test.randomize_options, rng),
        chat_id=chat_id,
    )
    session.add(attempt)
    try:
        await session.commit()
    except IntegrityError:
        # A concurrent duplicate click already created the attempt: return that one.
        await session.rollback()
        await session.refresh(user)
        existing = await get_in_progress(session, test_id, user_id)
        if existing is None:
            raise
        return StartResult(attempt=existing, resumed=True)
    logger.info("Attempt started", extra={"attempt_id": attempt.id, "test_id": test_id, "user_id": user.id})
    return StartResult(attempt=attempt)


async def current_question(session: AsyncSession, attempt: TestAttempt) -> tuple[TestQuestion, dict] | None:
    if attempt.current_index >= len(attempt.layout):
        return None
    item = attempt.layout[attempt.current_index]
    tq = await session.get(TestQuestion, item["tq"])
    if tq is None:
        return None
    return tq, item


async def submit_answer(
    session: AsyncSession,
    user: User,
    attempt_id: int,
    position: int,
    display_letter: str,
    now: datetime | None = None,
) -> AnswerResult:
    now = now or utcnow()
    user_id = user.id
    telegram_id = user.telegram_id
    # Row lock serialises concurrent clicks on the same attempt.
    attempt = (
        await session.execute(select(TestAttempt).where(TestAttempt.id == attempt_id).with_for_update(of=TestAttempt))
    ).scalar_one_or_none()
    if attempt is None or attempt.user_id != user_id:
        await session.commit()
        return AnswerResult(AnswerOutcome.INVALID)
    if attempt.status != AttemptStatus.IN_PROGRESS:
        await session.commit()
        return AnswerResult(AnswerOutcome.NOT_IN_PROGRESS, attempt=attempt)
    if now >= attempt.deadline_at:
        await finalize_attempt(session, attempt, AttemptStatus.EXPIRED, now)
        await session.commit()
        return AnswerResult(AnswerOutcome.EXPIRED, attempt=attempt, finished=True)
    if position != attempt.current_index or position >= len(attempt.layout):
        await session.commit()
        return AnswerResult(AnswerOutcome.DUPLICATE, attempt=attempt)

    item = attempt.layout[position]
    original = display_to_original(item, display_letter)
    tq = await session.get(TestQuestion, item["tq"])
    if original is None or tq is None:
        await session.commit()
        return AnswerResult(AnswerOutcome.INVALID, attempt=attempt)

    is_correct = original == tq.correct_option
    option_id = (
        await session.execute(
            select(QuestionOption.id).where(QuestionOption.test_question_id == tq.id, QuestionOption.letter == original)
        )
    ).scalar_one_or_none()
    session.add(
        UserAnswer(
            attempt_id=attempt.id,
            test_id=attempt.test_id,
            test_question_id=tq.id,
            user_id=user_id,
            telegram_id=telegram_id,
            position=position,
            selected_option=original,
            selected_option_id=option_id,
            selected_display=display_letter,
            correct_option=tq.correct_option,
            correct_display=original_to_display(item, tq.correct_option),
            is_correct=is_correct,
            answered_at=now,
        )
    )
    attempt.answered_count += 1
    attempt.correct_count += int(is_correct)
    attempt.incorrect_count += int(not is_correct)
    attempt.current_index = position + 1
    finished = attempt.current_index >= len(attempt.layout)
    if finished:
        await finalize_attempt(session, attempt, AttemptStatus.COMPLETED, now)
    try:
        await session.commit()
    except IntegrityError:
        # Concurrent duplicate answer: roll back, then reload the objects the caller still uses.
        await session.rollback()
        await session.refresh(user)
        await session.refresh(attempt)
        return AnswerResult(AnswerOutcome.DUPLICATE, attempt=attempt)
    return AnswerResult(
        AnswerOutcome.ACCEPTED,
        attempt=attempt,
        is_correct=is_correct,
        question=tq,
        selected_display=display_letter,
        correct_display=original_to_display(item, tq.correct_option),
        finished=finished,
    )


async def finalize_attempt(
    session: AsyncSession, attempt: TestAttempt, status: AttemptStatus, now: datetime | None = None
) -> None:
    """Close an attempt and compute the score (unanswered questions count as not correct)."""
    now = now or utcnow()
    test = await session.get(Test, attempt.test_id)
    attempt.status = status
    attempt.score_percent = compute_percent(attempt.correct_count, attempt.total_questions)
    end = now if status == AttemptStatus.COMPLETED else min(now, attempt.deadline_at)
    if status == AttemptStatus.COMPLETED:
        attempt.completed_at = now
    attempt.duration_seconds = max(0, int((end - attempt.started_at).total_seconds()))
    if status in (AttemptStatus.COMPLETED, AttemptStatus.EXPIRED) and test is not None:
        attempt.passed = float(attempt.score_percent) >= test.passing_percent
    if status == AttemptStatus.COMPLETED:
        logger.info(
            "Attempt completed",
            extra={"attempt_id": attempt.id, "test_id": attempt.test_id, "score": float(attempt.score_percent)},
        )


async def expire_attempts(session: AsyncSession, test_id: int | None = None, now: datetime | None = None) -> int:
    """Expire IN_PROGRESS attempts whose deadline passed (or all of a closed test)."""
    now = now or utcnow()
    stmt = (
        select(TestAttempt)
        .where(TestAttempt.status == AttemptStatus.IN_PROGRESS)
        .with_for_update(of=TestAttempt, skip_locked=True)
    )
    if test_id is not None:
        stmt = stmt.where(TestAttempt.test_id == test_id)
    else:
        stmt = stmt.where(TestAttempt.deadline_at <= now)
    attempts = list((await session.execute(stmt)).scalars().all())
    for attempt in attempts:
        await finalize_attempt(session, attempt, AttemptStatus.EXPIRED, now)
    await session.commit()
    if attempts:
        logger.info("Attempts expired", extra={"count": len(attempts), "test_id": test_id})
    return len(attempts)


async def set_message_ref(session: AsyncSession, attempt_id: int, chat_id: int, message_id: int) -> None:
    await session.execute(
        update(TestAttempt).where(TestAttempt.id == attempt_id).values(chat_id=chat_id, last_message_id=message_id)
    )
    await session.commit()


async def wrong_answers_for_attempt(
    session: AsyncSession, attempt: TestAttempt
) -> list[tuple[UserAnswer, TestQuestion]]:
    stmt = (
        select(UserAnswer, TestQuestion)
        .join(TestQuestion, TestQuestion.id == UserAnswer.test_question_id)
        .where(UserAnswer.attempt_id == attempt.id, UserAnswer.is_correct.is_(False))
        .order_by(UserAnswer.position)
    )
    return [(a, q) for a, q in await session.execute(stmt)]


async def answers_for_attempt(session: AsyncSession, attempt: TestAttempt) -> list[tuple[UserAnswer, TestQuestion]]:
    stmt = (
        select(UserAnswer, TestQuestion)
        .join(TestQuestion, TestQuestion.id == UserAnswer.test_question_id)
        .where(UserAnswer.attempt_id == attempt.id)
        .order_by(UserAnswer.position)
    )
    return [(a, q) for a, q in await session.execute(stmt)]


async def available_tests_for_user(
    session: AsyncSession, user: User, now: datetime | None = None
) -> list[tuple[Test, TestAttempt | None]]:
    """ACTIVE tests the employee may take, with their latest attempt (if any)."""
    now = now or utcnow()
    tests = list(
        (
            await session.execute(
                select(Test)
                .where(Test.status == TestStatus.ACTIVE, Test.starts_at <= now, Test.deadline_at > now)
                .order_by(Test.deadline_at)
            )
        )
        .scalars()
        .all()
    )
    if not tests:
        return []
    group_ids = set(
        (
            await session.execute(
                select(GroupMember.group_id).where(GroupMember.user_id == user.id, GroupMember.is_member.is_(True))
            )
        )
        .scalars()
        .all()
    )
    tests = [t for t in tests if t.group_id is None or t.group_id in group_ids]
    attempts = (
        (
            await session.execute(
                select(TestAttempt)
                .where(TestAttempt.user_id == user.id, TestAttempt.test_id.in_([t.id for t in tests]))
                .order_by(TestAttempt.attempt_no)
            )
        )
        .scalars()
        .all()
    )
    latest: dict[int, TestAttempt] = {}
    for attempt in attempts:
        latest[attempt.test_id] = attempt
    return [(t, latest.get(t.id)) for t in tests]


async def user_attempt_history(session: AsyncSession, user_id: int, limit: int = 10) -> list[TestAttempt]:
    stmt = (
        select(TestAttempt)
        .where(TestAttempt.user_id == user_id, TestAttempt.status != AttemptStatus.IN_PROGRESS)
        .order_by(TestAttempt.started_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())
