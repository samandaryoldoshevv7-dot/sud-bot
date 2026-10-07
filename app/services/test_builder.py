"""Test lifecycle: DRAFT → (assemble & review questions) → READY → ACTIVE → EXPIRED/CLOSED."""

from __future__ import annotations

import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.base import AIError
from app.models import (
    AttemptStatus,
    GenerationStatus,
    Material,
    MaterialStatus,
    Question,
    QuestionStatus,
    SourceKind,
    Test,
    TestAttempt,
    TestDifficulty,
    TestMaterial,
    TestQuestion,
    TestStatus,
    Topic,
)
from app.rag.vector_store import SourceScope
from app.schemas.ai import LETTERS
from app.services.attempts import expire_attempts
from app.services.materials import active_news_ids
from app.services.question_generation import MIXED_PLAN, GenerationRequest, QuestionGenerator
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

PAGE_SIZE = 8
RECENT_TESTS_TO_AVOID = 2


class TestStateError(Exception):
    __test__ = False

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class TestDraftData:
    __test__ = False
    title: str
    question_count: int
    starts_at: datetime
    deadline_at: datetime
    description: str | None = None
    option_count: int = 4
    difficulty: TestDifficulty = TestDifficulty.MIXED
    material_ids: list[int] = field(default_factory=list)
    use_all_materials: bool = False
    news_percent: int = 0
    focus_query: str | None = None
    group_id: int | None = None
    randomize_questions: bool = True
    randomize_options: bool = True
    answer_reveal: str = "after"
    allow_retakes: bool = False
    passing_percent: int = 60


async def create_test(session: AsyncSession, data: TestDraftData, created_by_id: int | None) -> Test:
    from app.models import AnswerReveal

    if data.deadline_at <= data.starts_at:
        raise TestStateError("deadline_before_start")
    test = Test(
        title=data.title.strip()[:255],
        description=(data.description or "").strip() or None,
        question_count=data.question_count,
        option_count=data.option_count,
        difficulty=data.difficulty,
        news_percent=data.news_percent,
        use_all_materials=data.use_all_materials,
        focus_query=(data.focus_query or "").strip()[:500] or None,
        group_id=data.group_id,
        starts_at=data.starts_at,
        deadline_at=data.deadline_at,
        randomize_questions=data.randomize_questions,
        randomize_options=data.randomize_options,
        answer_reveal=AnswerReveal(data.answer_reveal),
        allow_retakes=data.allow_retakes,
        passing_percent=data.passing_percent,
        status=TestStatus.DRAFT,
        created_by_id=created_by_id,
    )
    session.add(test)
    await session.flush()
    for mid in sorted(set(data.material_ids)):
        session.add(TestMaterial(test_id=test.id, material_id=mid))
    await session.commit()
    logger.info("Test created", extra={"test_id": test.id, "questions": test.question_count})
    return test


# ------------------------------------------------------------------------- snapshots


def make_snapshot_options(
    options: dict[str, str], correct: str, option_count: int, rng: random.Random
) -> tuple[dict[str, str], str]:
    """Reduce a question to ``option_count`` options (dropping random distractors, keeping the
    correct one) and relabel them A, B, C... preserving relative order."""
    letters = list(options.keys())
    if len(letters) > option_count:
        wrong = [x for x in letters if x != correct]
        keep_wrong = set(rng.sample(wrong, option_count - 1))
        letters = [x for x in letters if x == correct or x in keep_wrong]
    relabelled = {LETTERS[i]: options[orig] for i, orig in enumerate(letters)}
    return relabelled, LETTERS[letters.index(correct)]


def snapshot_question(
    test: Test, question: Question, position: int, rng: random.Random, topic_name: str = ""
) -> TestQuestion:
    options, correct = make_snapshot_options(question.options, question.correct_option, test.option_count, rng)
    return TestQuestion(
        test_id=test.id,
        question_id=question.id,
        question_version=question.version,
        position=position,
        question_text=question.question_text,
        options=options,
        correct_option=correct,
        explanation=question.explanation,
        topic_id=question.topic_id,
        topic_name=topic_name,
        difficulty=question.difficulty,
        source_kind=question.source_kind,
        source_reference=question.source_reference,
        source_excerpt=question.source_excerpt,
        is_approved=question.status == QuestionStatus.APPROVED,
    )


async def renumber_positions(session: AsyncSession, test_id: int) -> None:
    await session.execute(
        text("UPDATE test_questions SET position = position + 100000 WHERE test_id = :t"), {"t": test_id}
    )
    await session.execute(
        text(
            "UPDATE test_questions tq SET position = s.rn FROM ("
            " SELECT id, row_number() OVER (ORDER BY position, id) AS rn FROM test_questions WHERE test_id = :t"
            ") s WHERE tq.id = s.id"
        ),
        {"t": test_id},
    )


# ------------------------------------------------------------------------- assembly


async def test_scope(session: AsyncSession, test: Test) -> tuple[list[int], list[int]]:
    if test.use_all_materials:
        stmt = select(Material.id).where(Material.status == MaterialStatus.READY, Material.is_active.is_(True))
    else:
        stmt = (
            select(Material.id)
            .join(TestMaterial, TestMaterial.material_id == Material.id)
            .where(
                TestMaterial.test_id == test.id,
                Material.status == MaterialStatus.READY,
                Material.is_active.is_(True),
            )
        )
    material_ids = list((await session.execute(stmt)).scalars().all())
    news_ids = await active_news_ids(session) if test.news_percent > 0 else []
    return material_ids, news_ids


async def _recently_used_question_ids(session: AsyncSession, exclude_test_id: int) -> set[int]:
    recent_tests = (
        select(Test.id)
        .where(Test.id != exclude_test_id, Test.status != TestStatus.DRAFT)
        .order_by(Test.created_at.desc())
        .limit(RECENT_TESTS_TO_AVOID)
        .subquery()
    )
    stmt = select(TestQuestion.question_id).where(
        TestQuestion.test_id.in_(select(recent_tests.c.id)), TestQuestion.question_id.is_not(None)
    )
    return set((await session.execute(stmt)).scalars().all())


async def select_bank_questions(
    session: AsyncSession,
    test: Test,
    kind: SourceKind,
    count: int,
    source_ids: list[int],
    exclude_ids: set[int],
    rng: random.Random,
) -> list[Question]:
    """Pick approved bank questions: least used first, avoiding the last tests when possible,
    balancing difficulty for MIXED tests. Never returns a question already in ``exclude_ids``."""
    if count <= 0 or not source_ids:
        return []
    stmt = select(Question).where(
        Question.status == QuestionStatus.APPROVED,
        Question.source_kind == kind,
        Question.option_count >= test.option_count,
    )
    if kind == SourceKind.MATERIAL:
        stmt = stmt.where(Question.source_material_id.in_(source_ids))
    else:
        stmt = stmt.where(Question.source_news_id.in_(source_ids))
    if test.difficulty != TestDifficulty.MIXED:
        stmt = stmt.where(Question.difficulty == test.difficulty.value)
    if exclude_ids:
        stmt = stmt.where(Question.id.not_in(exclude_ids))
    stmt = stmt.order_by(Question.times_used, func.random()).limit(max(count * 5, 40))
    candidates = list((await session.execute(stmt)).unique().scalars().all())
    recent = await _recently_used_question_ids(session, test.id)
    fresh = [q for q in candidates if q.id not in recent]
    stale = [q for q in candidates if q.id in recent]
    pool = fresh + stale  # repeats from recent tests only when nothing else is available

    if test.difficulty != TestDifficulty.MIXED:
        return pool[:count]
    by_diff: dict[str, list[Question]] = {}
    for q in pool:
        by_diff.setdefault(q.difficulty.value, []).append(q)
    chosen: list[Question] = []
    plan_index = 0
    while len(chosen) < count and any(by_diff.values()):
        wanted = MIXED_PLAN[plan_index % len(MIXED_PLAN)]
        plan_index += 1
        bucket = by_diff.get(wanted) or next(b for b in by_diff.values() if b)
        chosen.append(bucket.pop(0))
    return chosen


@dataclass
class AssemblyReport:
    added_from_bank: int = 0
    generated: int = 0
    material_questions: int = 0
    news_questions: int = 0
    missing: int = 0
    rejected: dict = field(default_factory=dict)
    error: str | None = None


GeneratorFactory = Callable[[], QuestionGenerator]
ProgressFn = Callable[[str, int, int], Awaitable[None]]


async def assemble_questions(
    session_maker: async_sessionmaker[AsyncSession],
    test_id: int,
    generator_factory: GeneratorFactory | None,
    progress: ProgressFn | None = None,
    force_generate: bool = False,
    rng: random.Random | None = None,
) -> AssemblyReport:
    """Fill a DRAFT test up to ``question_count``: bank first, then RAG generation for the shortfall.

    Enforces the requested news percentage as closely as the available sources permit.
    """
    rng = rng or random.Random()
    report = AssemblyReport()
    async with session_maker() as session:
        claimed = (
            await session.execute(
                update(Test)
                .where(
                    Test.id == test_id,
                    Test.status == TestStatus.DRAFT,
                    Test.generation_status != GenerationStatus.RUNNING,
                )
                .values(generation_status=GenerationStatus.RUNNING, generation_error=None)
                .returning(Test.id)
            )
        ).scalar_one_or_none()
        await session.commit()
        if claimed is None:
            report.error = "busy_or_not_draft"
            return report
        try:
            await _assemble(session, test_id, generator_factory, progress, force_generate, rng, report)
            status = GenerationStatus.DONE if report.missing == 0 else GenerationStatus.FAILED
            if report.missing and not report.error:
                report.error = "not_enough_questions"
        except Exception as exc:
            await session.rollback()
            logger.exception("Test assembly failed", extra={"test_id": test_id})
            report.error = report.error or str(exc)[:300]
            status = GenerationStatus.FAILED
        await session.execute(
            update(Test)
            .where(Test.id == test_id)
            .values(
                generation_status=status, generation_error=report.error if status == GenerationStatus.FAILED else None
            )
        )
        await session.commit()
    return report


async def _assemble(
    session: AsyncSession,
    test_id: int,
    generator_factory: GeneratorFactory | None,
    progress: ProgressFn | None,
    force_generate: bool,
    rng: random.Random,
    report: AssemblyReport,
) -> None:
    test = await session.get(Test, test_id)
    assert test is not None
    existing = list(
        (await session.execute(select(TestQuestion).where(TestQuestion.test_id == test_id))).scalars().all()
    )
    used_ids = {tq.question_id for tq in existing if tq.question_id}
    # Rejected questions must never come back into this test.
    used_ids |= set(
        (await session.execute(select(Question.id).where(Question.status == QuestionStatus.REJECTED))).scalars().all()
    )
    have_news = sum(1 for tq in existing if tq.source_kind == SourceKind.NEWS)
    needed = test.question_count - len(existing)
    if needed <= 0:
        return
    material_ids, news_ids = await test_scope(session, test)
    if not material_ids and not news_ids:
        report.missing = needed
        report.error = "no_sources"
        return

    news_target_total = round(test.question_count * test.news_percent / 100) if news_ids else 0
    need_news = max(0, min(needed, news_target_total - have_news))
    need_material = needed - need_news
    if not material_ids:  # only news available
        need_news, need_material = needed, 0

    next_position = (max((tq.position for tq in existing), default=0)) + 1
    added: list[TestQuestion] = []

    async def add(questions: list[Question]) -> None:
        nonlocal next_position
        topic_ids = {q.topic_id for q in questions if q.topic_id}
        names: dict[int, str] = {}
        if topic_ids:
            names = dict((await session.execute(select(Topic.id, Topic.name).where(Topic.id.in_(topic_ids)))).all())
        for q in questions:
            if q.id in used_ids:
                continue
            used_ids.add(q.id)
            tq = snapshot_question(test, q, next_position, rng, names.get(q.topic_id or 0, ""))
            next_position += 1
            session.add(tq)
            added.append(tq)
            if q.source_kind == SourceKind.NEWS:
                report.news_questions += 1
            else:
                report.material_questions += 1
        await session.commit()

    async def fill(kind: SourceKind, count: int, source_ids: list[int]) -> int:
        if count <= 0 or not source_ids:
            return count
        if not force_generate:
            bank = await select_bank_questions(session, test, kind, count, source_ids, used_ids, rng)
            await add(bank)
            report.added_from_bank += len(bank)
            count -= len(bank)
        if count <= 0:
            return 0
        if generator_factory is None:
            report.error = report.error or "ai_not_configured"
            return count
        try:
            generator = generator_factory()
        except AIError as exc:
            report.error = str(exc)[:300]
            return count
        scope = (
            SourceScope(material_ids=source_ids) if kind == SourceKind.MATERIAL else SourceScope(news_ids=source_ids)
        )

        async def gen_progress(done: int, total: int) -> None:
            if progress:
                await progress(kind.value, report.generated + done, report.generated + total)

        result = await generator.generate(
            session,
            GenerationRequest(
                scope=scope,
                count=count,
                option_count=test.option_count,
                difficulty=test.difficulty,
                focus_query=test.focus_query,
            ),
            progress=gen_progress,
        )
        for key, value in result.rejected.items():
            report.rejected[key] = report.rejected.get(key, 0) + value
        if result.error and not result.questions:
            report.error = report.error or result.error
        await add(result.questions)
        report.generated += result.created
        return count - result.created

    if progress:
        await progress("start", 0, needed)
    left_news = await fill(SourceKind.NEWS, need_news, news_ids)
    # Not enough news questions: top up from materials (closest possible distribution).
    left_material = await fill(SourceKind.MATERIAL, need_material + left_news, material_ids)
    if left_material > 0 and news_ids and need_news < needed:
        left_material = await fill(SourceKind.NEWS, left_material, news_ids)
    report.missing = max(0, left_material)
    logger.info(
        "Test assembled",
        extra={
            "test_id": test_id,
            "bank": report.added_from_bank,
            "generated": report.generated,
            "missing": report.missing,
        },
    )


# ------------------------------------------------------------------------- review


async def get_test_question(session: AsyncSession, test_id: int, position: int) -> TestQuestion | None:
    stmt = select(TestQuestion).where(TestQuestion.test_id == test_id, TestQuestion.position == position)
    return (await session.execute(stmt)).scalar_one_or_none()


async def question_counts(session: AsyncSession, test_id: int) -> tuple[int, int]:
    """``(total, approved)`` snapshot counts."""
    total, approved = (
        await session.execute(
            select(
                func.count(TestQuestion.id), func.count(TestQuestion.id).filter(TestQuestion.is_approved.is_(True))
            ).where(TestQuestion.test_id == test_id)
        )
    ).one()
    return int(total), int(approved)


async def _require_draft(session: AsyncSession, test_id: int) -> Test:
    test = await session.get(Test, test_id)
    if test is None:
        raise TestStateError("not_found")
    if test.status != TestStatus.DRAFT:
        raise TestStateError("not_draft")
    return test


async def approve_test_question(session: AsyncSession, tq_id: int, reviewer_id: int | None) -> TestQuestion:
    tq = await session.get(TestQuestion, tq_id)
    if tq is None:
        raise TestStateError("not_found")
    await _require_draft(session, tq.test_id)
    tq.is_approved = True
    if tq.question_id:
        question = await session.get(Question, tq.question_id)
        if question and question.status == QuestionStatus.PENDING:
            question.status = QuestionStatus.APPROVED
            question.reviewed_by_id = reviewer_id
            question.reviewed_at = utcnow()
    await session.commit()
    return tq


async def approve_all(session: AsyncSession, test_id: int, reviewer_id: int | None) -> int:
    await _require_draft(session, test_id)
    tqs = list(
        (
            await session.execute(
                select(TestQuestion).where(TestQuestion.test_id == test_id, TestQuestion.is_approved.is_(False))
            )
        )
        .scalars()
        .all()
    )
    qids = [tq.question_id for tq in tqs if tq.question_id]
    for tq in tqs:
        tq.is_approved = True
    if qids:
        await session.execute(
            update(Question)
            .where(Question.id.in_(qids), Question.status == QuestionStatus.PENDING)
            .values(status=QuestionStatus.APPROVED, reviewed_by_id=reviewer_id, reviewed_at=utcnow())
        )
    await session.commit()
    return len(tqs)


async def reject_test_question(session: AsyncSession, tq_id: int, reviewer_id: int | None) -> int:
    """Remove a question from a draft test; a pending (unreviewed) AI question is rejected in the bank."""
    tq = await session.get(TestQuestion, tq_id)
    if tq is None:
        raise TestStateError("not_found")
    test_id = tq.test_id
    await _require_draft(session, test_id)
    if tq.question_id:
        question = await session.get(Question, tq.question_id)
        if question and question.status == QuestionStatus.PENDING:
            question.status = QuestionStatus.REJECTED
            question.reviewed_by_id = reviewer_id
            question.reviewed_at = utcnow()
    await session.delete(tq)
    await session.flush()
    await renumber_positions(session, test_id)
    await session.commit()
    return test_id


async def mark_ready(session: AsyncSession, test_id: int) -> Test:
    test = await _require_draft(session, test_id)
    if test.generation_status == GenerationStatus.RUNNING:
        raise TestStateError("generation_running")
    total, approved = await question_counts(session, test_id)
    if total != test.question_count:
        raise TestStateError("question_count_mismatch")
    if approved != total:
        raise TestStateError("not_all_approved")
    test.status = TestStatus.READY
    await session.commit()
    return test


async def back_to_draft(session: AsyncSession, test_id: int) -> Test:
    test = await session.get(Test, test_id)
    if test is None:
        raise TestStateError("not_found")
    if test.status != TestStatus.READY or test.published_at is not None:
        raise TestStateError("cannot_edit")
    test.status = TestStatus.DRAFT
    await session.commit()
    return test


async def publish(session: AsyncSession, test_id: int, now: datetime | None = None) -> Test:
    """Publish a READY test: becomes ACTIVE immediately or at ``starts_at`` (scheduler)."""
    now = now or utcnow()
    test = await session.get(Test, test_id)
    if test is None:
        raise TestStateError("not_found")
    if test.status != TestStatus.READY:
        raise TestStateError("not_ready")
    if test.published_at is not None:
        return test  # idempotent
    if test.deadline_at <= now:
        raise TestStateError("deadline_passed")
    test.published_at = now
    if test.starts_at <= now:
        test.status = TestStatus.ACTIVE
        test.activated_at = now
    qids = list(
        (await session.execute(select(TestQuestion.question_id).where(TestQuestion.test_id == test_id))).scalars().all()
    )
    qids = [q for q in qids if q]
    if qids:
        await session.execute(
            update(Question).where(Question.id.in_(qids)).values(times_used=Question.times_used + 1, last_used_at=now)
        )
    await session.commit()
    logger.info("Test published", extra={"test_id": test_id, "status": test.status.value})
    return test


async def activate_due(session: AsyncSession, now: datetime | None = None) -> list[int]:
    now = now or utcnow()
    ids = list(
        (
            await session.execute(
                update(Test)
                .where(
                    Test.status == TestStatus.READY,
                    Test.published_at.is_not(None),
                    Test.starts_at <= now,
                    Test.deadline_at > now,
                )
                .values(status=TestStatus.ACTIVE, activated_at=now)
                .returning(Test.id)
            )
        )
        .scalars()
        .all()
    )
    await session.commit()
    for tid in ids:
        logger.info("Test activated", extra={"test_id": tid})
    return ids


async def expire_due(session: AsyncSession, now: datetime | None = None) -> list[int]:
    """ACTIVE (or published READY) tests past their deadline → EXPIRED; open attempts expire."""
    now = now or utcnow()
    ids = list(
        (
            await session.execute(
                update(Test)
                .where(
                    Test.status.in_([TestStatus.ACTIVE, TestStatus.READY]),
                    Test.published_at.is_not(None),
                    Test.deadline_at <= now,
                )
                .values(status=TestStatus.EXPIRED, ended_at=now)
                .returning(Test.id)
            )
        )
        .scalars()
        .all()
    )
    await session.commit()
    for tid in ids:
        await expire_attempts(session, tid, now)
        logger.info("Test expired", extra={"test_id": tid})
    return ids


async def close_test(session: AsyncSession, test_id: int, now: datetime | None = None) -> Test:
    now = now or utcnow()
    test = await session.get(Test, test_id)
    if test is None:
        raise TestStateError("not_found")
    if test.status not in (TestStatus.ACTIVE, TestStatus.READY, TestStatus.EXPIRED):
        raise TestStateError("cannot_close")
    test.status = TestStatus.CLOSED
    test.ended_at = test.ended_at or now
    await session.commit()
    await expire_attempts(session, test_id, now)
    logger.info("Test closed", extra={"test_id": test_id})
    return test


async def extend_deadline(
    session: AsyncSession, test_id: int, new_deadline: datetime, now: datetime | None = None
) -> Test:
    now = now or utcnow()
    test = await session.get(Test, test_id)
    if test is None:
        raise TestStateError("not_found")
    if test.status not in (TestStatus.ACTIVE, TestStatus.READY, TestStatus.DRAFT):
        raise TestStateError("cannot_extend")
    if new_deadline <= max(now, test.starts_at):
        raise TestStateError("deadline_before_start")
    test.deadline_at = new_deadline
    test.reminder_sent_at = None
    await session.execute(
        update(TestAttempt)
        .where(TestAttempt.test_id == test_id, TestAttempt.status == AttemptStatus.IN_PROGRESS)
        .values(deadline_at=new_deadline)
    )
    await session.commit()
    return test


async def delete_test(session: AsyncSession, test_id: int) -> None:
    test = await session.get(Test, test_id)
    if test is None:
        raise TestStateError("not_found")
    attempts = (
        await session.execute(select(func.count(TestAttempt.id)).where(TestAttempt.test_id == test_id))
    ).scalar_one()
    if attempts or test.status not in (TestStatus.DRAFT, TestStatus.READY) or test.published_at is not None:
        raise TestStateError("cannot_delete")
    await session.execute(delete(Test).where(Test.id == test_id))
    await session.commit()


async def list_tests(session: AsyncSession, page: int, status: TestStatus | None = None) -> tuple[list[Test], int]:
    base = select(Test)
    if status:
        base = base.where(Test.status == status)
    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = await session.execute(base.order_by(Test.created_at.desc()).offset(page * PAGE_SIZE).limit(PAGE_SIZE))
    return list(rows.scalars().all()), total


async def test_material_titles(session: AsyncSession, test: Test) -> list[str]:
    if test.use_all_materials:
        return []
    stmt = (
        select(Material.title)
        .join(TestMaterial, TestMaterial.material_id == Material.id)
        .where(TestMaterial.test_id == test.id)
        .order_by(Material.title)
    )
    return list((await session.execute(stmt)).scalars().all())


async def reset_stuck_generation(session: AsyncSession) -> int:
    """On startup, jobs marked RUNNING belong to a previous process that died."""
    result = await session.execute(
        update(Test)
        .where(Test.generation_status == GenerationStatus.RUNNING)
        .values(generation_status=GenerationStatus.FAILED, generation_error="interrupted_by_restart")
        .returning(Test.id)
    )
    ids = list(result.scalars().all())
    await session.commit()
    return len(ids)
