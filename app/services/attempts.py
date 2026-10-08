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
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AssignmentStatus,
    AttemptStatus,
    Group,
    GroupMember,
    QuestionOption,
    Test,
    TestAssignment,
    TestAttempt,
    TestAudience,
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
    PAUSED = "paused"
    NOT_ASSIGNED = "not_assigned"


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
    PAUSED = "paused"


@dataclass
class AnswerResult:
    outcome: AnswerOutcome
    attempt: TestAttempt | None = None
    is_correct: bool | None = None
    question: TestQuestion | None = None
    selected_display: str | None = None
    correct_display: str | None = None
    finished: bool = False


def _source_key(tq: TestQuestion) -> object:
    return tq.source_id or getattr(tq, "source_name", "") or None


def interleave_by_source(questions: list[TestQuestion], rng: random.Random) -> list[TestQuestion]:
    """Shuffle while mixing sources: the next question never comes from the same source as the
    previous one unless no other source has questions left (e.g. 1-Konstitutsiya, 2-Mehnat kodeksi,
    3-Ma'muriy kodeks, 4-Konstitutsiya ...)."""
    buckets: dict[object, list[TestQuestion]] = {}
    for tq in questions:
        buckets.setdefault(_source_key(tq), []).append(tq)
    for bucket in buckets.values():
        rng.shuffle(bucket)
    order: list[TestQuestion] = []
    last: object = object()
    while any(buckets.values()):
        candidates = [k for k, b in buckets.items() if b and k != last] or [k for k, b in buckets.items() if b]
        most = max(len(buckets[k]) for k in candidates)
        key = rng.choice([k for k in candidates if len(buckets[k]) == most])
        order.append(buckets[key].pop())
        last = key
    return order


def build_layout(
    questions: list[TestQuestion], randomize_questions: bool, randomize_options: bool, rng: random.Random
) -> list[dict]:
    order = list(questions)
    if randomize_questions:
        if len({_source_key(tq) for tq in order}) > 1:
            order = interleave_by_source(order, rng)
        else:
            rng.shuffle(order)
    layout = []
    for tq in order:
        letters = list(tq.options.keys())
        if randomize_options:
            rng.shuffle(letters)
        layout.append({"tq": tq.id, "opts": letters})
    return layout


def attempt_deadline(test: Test, started_at: datetime) -> datetime:
    """Personal deadline: START time + the test duration (never after the test is closed)."""
    return min(started_at + timedelta(seconds=test.duration_seconds), test.deadline_at)


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


async def audience_group_ids(session: AsyncSession, test: Test) -> set[int]:
    """Groups whose members may take the test: its target group plus every group it was posted to."""
    from app.models import GroupTestPost

    ids = set((await session.execute(select(GroupTestPost.group_id).where(GroupTestPost.test_id == test.id))).scalars())
    if test.group_id is not None:
        ids.add(test.group_id)
    return ids


async def get_assignment(session: AsyncSession, test_id: int, user_id: int) -> TestAssignment | None:
    stmt = select(TestAssignment).where(TestAssignment.test_id == test_id, TestAssignment.user_id == user_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _member_of_any(session: AsyncSession, user_id: int, group_ids: set[int]) -> bool:
    if not group_ids:
        return False
    stmt = (
        select(func.count())
        .select_from(GroupMember)
        .join(Group, Group.id == GroupMember.group_id)
        .where(GroupMember.group_id.in_(group_ids), GroupMember.user_id == user_id, GroupMember.is_member.is_(True))
    )
    return bool((await session.execute(stmt)).scalar_one())


async def user_in_test_audience(session: AsyncSession, test: Test, user: User) -> bool:
    """Was this test given to the employee? (all employees / a group / selected employees, minus
    employees the admin took the test away from)."""
    assignment = await get_assignment(session, test.id, user.id)
    if assignment is not None and assignment.status == AssignmentStatus.REMOVED:
        return False
    if assignment is not None:
        return True  # explicitly assigned (selected / single employee) or granted a retake
    if test.audience == TestAudience.USERS:
        return False
    if test.audience == TestAudience.ALL and test.group_id is None:
        return True
    return await _member_of_any(session, user.id, await audience_group_ids(session, test))


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
    if test.paused:
        return StartResult(error=StartError.PAUSED)
    if not await user_in_test_audience(session, test, user):
        assignment = await get_assignment(session, test_id, user.id)
        if test.audience == TestAudience.GROUP and assignment is None:
            return StartResult(error=StartError.NOT_IN_GROUP)
        return StartResult(error=StartError.NOT_ASSIGNED)

    previous = (
        await session.execute(
            select(TestAttempt.status, TestAttempt.attempt_no).where(
                TestAttempt.test_id == test_id, TestAttempt.user_id == user.id
            )
        )
    ).all()
    # Answers are final, so a finished attempt is retaken only when an admin allowed it (once).
    if any(st != AttemptStatus.IN_PROGRESS for st, _ in previous):
        assignment = await get_assignment(session, test_id, user.id)
        if assignment is None or not assignment.retake_allowed:
            return StartResult(error=StartError.ALREADY_COMPLETED)
        assignment.retake_allowed = False  # committed together with the new attempt
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
        deadline_at=attempt_deadline(test, now),
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


async def answered_question_ids(session: AsyncSession, attempt_id: int) -> set[int]:
    rows = await session.execute(select(UserAnswer.test_question_id).where(UserAnswer.attempt_id == attempt_id))
    return set(rows.scalars().all())


def next_unanswered_index(layout: list[dict], answered: set[int]) -> int:
    """First layout position without an answer (``len(layout)`` when everything is answered).

    A group test can be answered partly in the group (any order) and continued in the private
    chat, so "the current question" is always the first unanswered one in the attempt's layout.
    """
    for index, item in enumerate(layout):
        if item["tq"] not in answered:
            return index
    return len(layout)


async def current_question(session: AsyncSession, attempt: TestAttempt) -> tuple[TestQuestion, dict] | None:
    index = next_unanswered_index(attempt.layout, await answered_question_ids(session, attempt.id))
    if index >= len(attempt.layout):
        return None
    if attempt.current_index != index:
        attempt.current_index = index
        await session.commit()
    item = attempt.layout[index]
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
    if (await session.get(Test, attempt.test_id)).paused:  # type: ignore[union-attr]
        await session.commit()
        return AnswerResult(AnswerOutcome.PAUSED, attempt=attempt)
    answered = await answered_question_ids(session, attempt.id)
    if position != next_unanswered_index(attempt.layout, answered) or position >= len(attempt.layout):
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
    answered.add(tq.id)
    attempt.current_index = next_unanswered_index(attempt.layout, answered)
    finished = attempt.answered_count >= attempt.total_questions
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
    return len(await expire_attempts_list(session, test_id, now))


async def expire_attempts_list(
    session: AsyncSession, test_id: int | None = None, now: datetime | None = None
) -> list[TestAttempt]:
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
    return attempts


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


async def _user_group_ids(session: AsyncSession, user_id: int) -> set[int]:
    rows = await session.execute(
        select(GroupMember.group_id).where(GroupMember.user_id == user_id, GroupMember.is_member.is_(True))
    )
    return set(rows.scalars().all())


async def _visible(session: AsyncSession, tests: list[Test], user: User) -> list[Test]:
    if not tests:
        return []
    assignments = {
        a.test_id: a
        for a in (
            await session.execute(
                select(TestAssignment).where(
                    TestAssignment.user_id == user.id, TestAssignment.test_id.in_([x.id for x in tests])
                )
            )
        ).scalars()
    }
    group_ids = await _user_group_ids(session, user.id)
    visible = []
    for test in tests:
        assignment = assignments.get(test.id)
        if assignment is not None:
            if assignment.status == AssignmentStatus.ASSIGNED:
                visible.append(test)
            continue
        if test.audience == TestAudience.USERS:
            continue
        if (test.audience == TestAudience.ALL and test.group_id is None) or (
            await audience_group_ids(session, test)
        ) & group_ids:
            visible.append(test)
    return visible


async def _latest_attempts(session: AsyncSession, user_id: int, test_ids: list[int]) -> dict[int, TestAttempt]:
    latest: dict[int, TestAttempt] = {}
    if not test_ids:
        return latest
    rows = await session.execute(
        select(TestAttempt)
        .where(TestAttempt.user_id == user_id, TestAttempt.test_id.in_(test_ids))
        .order_by(TestAttempt.attempt_no)
    )
    for attempt in rows.scalars():
        latest[attempt.test_id] = attempt
    return latest


class MyTestState(str, Enum):
    NOT_STARTED = "new"  # 🟢 Ishlanmagan
    IN_PROGRESS = "progress"  # 🟡 Jarayonda
    COMPLETED = "done"  # 🔵 Tugatilgan
    EXPIRED = "expired"  # 🔴 Muddati tugagan


@dataclass
class MyTest:
    test: Test
    attempt: TestAttempt | None
    state: MyTestState
    can_start: bool  # ▶️ start / continue / retake is possible now


async def my_tests(session: AsyncSession, user: User, now: datetime | None = None, limit: int = 15) -> list[MyTest]:
    """ "📚 Testlarim": running tests given to the employee plus the ones they already took."""
    now = now or utcnow()
    running = list(
        (
            await session.execute(
                select(Test).where(Test.status == TestStatus.ACTIVE, Test.starts_at <= now, Test.deadline_at > now)
            )
        )
        .scalars()
        .all()
    )
    running = await _visible(session, running, user)
    taken_ids = list(
        (
            await session.execute(
                select(TestAttempt.test_id)
                .where(TestAttempt.user_id == user.id)
                .group_by(TestAttempt.test_id)
                .order_by(func.max(TestAttempt.started_at).desc())
                .limit(limit)
            )
        ).scalars()
    )
    by_id = {x.id: x for x in running}
    missing = [i for i in taken_ids if i not in by_id]
    if missing:
        for test in (await session.execute(select(Test).where(Test.id.in_(missing)))).scalars():
            by_id[test.id] = test
    latest = await _latest_attempts(session, user.id, list(by_id))
    retakes = set(
        (
            await session.execute(
                select(TestAssignment.test_id).where(
                    TestAssignment.user_id == user.id,
                    TestAssignment.retake_allowed.is_(True),
                    TestAssignment.status == AssignmentStatus.ASSIGNED,
                )
            )
        ).scalars()
    )
    items: list[MyTest] = []
    for test in by_id.values():
        attempt = latest.get(test.id)
        open_now = test.id in {x.id for x in running} and not test.paused
        if attempt is None:
            state = MyTestState.NOT_STARTED if open_now else MyTestState.EXPIRED
            can_start = open_now
        elif attempt.status == AttemptStatus.IN_PROGRESS and now < attempt.deadline_at:
            state, can_start = MyTestState.IN_PROGRESS, open_now
        elif attempt.status == AttemptStatus.COMPLETED:
            state, can_start = MyTestState.COMPLETED, open_now and test.id in retakes
        else:
            state, can_start = MyTestState.EXPIRED, open_now and test.id in retakes
        items.append(MyTest(test, attempt, state, can_start))
    order = {MyTestState.IN_PROGRESS: 0, MyTestState.NOT_STARTED: 1, MyTestState.COMPLETED: 2, MyTestState.EXPIRED: 3}
    items.sort(key=lambda m: (order[m.state], -(m.test.id)))
    return items[:limit]


async def add_time(session: AsyncSession, attempt_id: int, seconds: int, now: datetime | None = None) -> TestAttempt:
    """Give an employee extra time. An attempt that already ran out of time (with unanswered
    questions) is reopened from the first unanswered question."""
    now = now or utcnow()
    attempt = (
        await session.execute(select(TestAttempt).where(TestAttempt.id == attempt_id).with_for_update(of=TestAttempt))
    ).scalar_one_or_none()
    if attempt is None:
        raise ValueError("not_found")
    test = await session.get(Test, attempt.test_id)
    assert test is not None
    if test.status != TestStatus.ACTIVE:
        await session.commit()
        raise ValueError("test_not_active")
    if attempt.status == AttemptStatus.IN_PROGRESS:
        attempt.deadline_at = max(attempt.deadline_at, now) + timedelta(seconds=seconds)
    elif attempt.status == AttemptStatus.EXPIRED and attempt.answered_count < attempt.total_questions:
        attempt.status = AttemptStatus.IN_PROGRESS
        attempt.deadline_at = now + timedelta(seconds=seconds)
        attempt.completed_at = None
        attempt.passed = None
        attempt.duration_seconds = None
    else:
        await session.commit()
        raise ValueError("attempt_finished")
    if test.deadline_at < attempt.deadline_at:
        test.deadline_at = attempt.deadline_at  # keep the test open while this employee still works
    try:
        await session.commit()
    except IntegrityError as exc:  # a newer attempt is already running
        await session.rollback()
        raise ValueError("other_attempt_running") from exc
    logger.info("Extra time given", extra={"attempt_id": attempt_id, "seconds": seconds})
    return attempt


async def user_attempt_history(session: AsyncSession, user_id: int, limit: int = 10) -> list[TestAttempt]:
    stmt = (
        select(TestAttempt)
        .where(TestAttempt.user_id == user_id, TestAttempt.status != AttemptStatus.IN_PROGRESS)
        .order_by(TestAttempt.started_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())
