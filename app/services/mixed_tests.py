"""🔀 Mixed tests from several uploaded files.

Example: Konstitutsiya.pdf + Ma'muriy kodeks.pdf + Mehnat kodeksi.docx + Sug'urta qonunchiligi.pdf
→ "Aralashtirib 40 ta test tuz":

1. wait until every file is extracted, cleaned, chunked and indexed;
2. split the requested number of questions evenly between the files (10 + 10 + 10 + 10);
3. take verified questions of each file from the bank, generate the shortfall with the
   source-grounded pipeline (each question is tied to the chunk — and so the file — it came from);
4. if one file cannot give its share, the other files make up the difference;
5. put the questions in an interleaved order (1-Konstitutsiya, 2-Mehnat kodeksi, 3-Ma'muriy kodeks,
   ...): one source never fills consecutive positions while other sources still have questions;
6. store every question with its source (``test_sources`` row, source name and file name).
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.base import AIError
from app.models import (
    GenerationStatus,
    Material,
    MaterialStatus,
    Question,
    QuestionStatus,
    SourceKind,
    Test,
    TestQuestion,
    TestSource,
    TestStatus,
    Topic,
)
from app.rag.vector_store import SourceScope
from app.services.question_generation import GenerationRequest, QuestionGenerator
from app.services.test_builder import select_bank_questions, snapshot_question

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str, int, int], Awaitable[None]]
GeneratorFactory = Callable[[], QuestionGenerator]


@dataclass
class MixedReport:
    created: int = 0
    requested: int = 0
    per_source: dict[str, int] = field(default_factory=dict)
    failed_sources: list[str] = field(default_factory=list)
    rejected: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    @property
    def missing(self) -> int:
        return max(0, self.requested - self.created)


def split_quota(total: int, parts: int) -> list[int]:
    """40 over 4 sources → [10, 10, 10, 10]; 10 over 3 → [4, 3, 3]."""
    if parts <= 0:
        return []
    base, extra = divmod(total, parts)
    return [base + (1 if i < extra else 0) for i in range(parts)]


def interleave(groups: list[list], rng: random.Random) -> list:
    """Round-robin over sources (random source order each round, never the same source twice in a
    row while another source still has questions)."""
    pools = [list(g) for g in groups if g]
    order: list = []
    last = -1
    while any(pools):
        candidates = [i for i, p in enumerate(pools) if p and i != last] or [i for i, p in enumerate(pools) if p]
        most = max(len(pools[i]) for i in candidates)
        pick = rng.choice([i for i in candidates if len(pools[i]) == most])
        order.append(pools[pick].pop(0))
        last = pick
    return order


async def wait_materials_ready(
    session_maker: async_sessionmaker[AsyncSession], material_ids: list[int], timeout: float = 900, poll: float = 3
) -> dict[int, MaterialStatus]:
    """Files are processed in the background right after upload; wait until none is still running."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        async with session_maker() as session:
            statuses = dict(
                (await session.execute(select(Material.id, Material.status).where(Material.id.in_(material_ids)))).all()
            )
        busy = [m for m, st in statuses.items() if st in (MaterialStatus.UPLOADED, MaterialStatus.PROCESSING)]
        if not busy or loop.time() >= deadline:
            return statuses
        await asyncio.sleep(poll)


async def build_mixed_test(
    session_maker: async_sessionmaker[AsyncSession],
    test_id: int,
    generator_factory: GeneratorFactory | None,
    progress: ProgressFn | None = None,
    rng: random.Random | None = None,
    wait_timeout: float = 900,
) -> MixedReport:
    rng = rng or random.Random()
    report = MixedReport()
    async with session_maker() as session:
        test = await session.get(Test, test_id)
        if test is None:
            report.error = "not_found"
            return report
        report.requested = test.question_count
        sources = list(
            (
                await session.execute(
                    select(TestSource).where(TestSource.test_id == test_id).order_by(TestSource.position)
                )
            )
            .scalars()
            .all()
        )
        material_ids = [s.material_id for s in sources if s.material_id]
    if not material_ids:
        report.error = "no_sources"
        return report
    if progress:
        await progress("processing", 0, report.requested)
    statuses = await wait_materials_ready(session_maker, material_ids, wait_timeout)

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
            await _build(session, test_id, sources, statuses, generator_factory, progress, rng, report)
            if report.created == 0 and not report.error:
                report.error = "not_enough_questions"
            status = GenerationStatus.DONE if report.missing == 0 else GenerationStatus.FAILED
            if report.missing and not report.error:
                report.error = "not_enough_questions"
        except Exception as exc:
            await session.rollback()
            logger.exception("Mixed test build failed", extra={"test_id": test_id})
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
    logger.info(
        "Mixed test built",
        extra={
            "test_id": test_id,
            "created": report.created,
            "requested": report.requested,
            "per_source": report.per_source,
        },
    )
    return report


async def _build(
    session: AsyncSession,
    test_id: int,
    sources: list[TestSource],
    statuses: dict[int, MaterialStatus],
    generator_factory: GeneratorFactory | None,
    progress: ProgressFn | None,
    rng: random.Random,
    report: MixedReport,
) -> None:
    test = await session.get(Test, test_id)
    assert test is not None
    ready = [s for s in sources if s.material_id and statuses.get(s.material_id) == MaterialStatus.READY]
    report.failed_sources = [s.source_name for s in sources if s not in ready]
    if not ready:
        report.error = "no_ready_sources"
        return
    used: set[int] = set(
        (await session.execute(select(Question.id).where(Question.status == QuestionStatus.REJECTED))).scalars().all()
    )
    picked: dict[int, list[Question]] = {s.id: [] for s in ready}
    generator: QuestionGenerator | None = None

    async def take(source: TestSource, count: int) -> int:
        """Up to ``count`` more questions of this one file: bank first, then generation."""
        nonlocal generator
        if count <= 0:
            return 0
        got = await select_bank_questions(session, test, SourceKind.MATERIAL, count, [source.material_id], used, rng)
        if len(got) < count and generator_factory is not None:
            try:
                generator = generator or generator_factory()
            except AIError as exc:
                report.error = report.error or str(exc)[:300]
            else:
                result = await generator.generate(
                    session,
                    GenerationRequest(
                        scope=SourceScope(material_ids=[source.material_id]),  # type: ignore[list-item]
                        count=count - len(got),
                        option_count=test.option_count,
                        difficulty=test.difficulty,
                    ),
                )
                for key, value in result.rejected.items():
                    report.rejected[key] = report.rejected.get(key, 0) + value
                if result.error and not result.questions:
                    report.error = report.error or result.error
                got += [q for q in result.questions if q.source_material_id == source.material_id]
        elif len(got) < count and generator_factory is None:
            report.error = report.error or "ai_not_configured"
        got = [q for q in got if q.id not in used][:count]
        used.update(q.id for q in got)
        picked[source.id].extend(got)
        if progress:
            await progress("generating", sum(len(v) for v in picked.values()), test.question_count)
        return len(got)

    quotas = split_quota(test.question_count, len(ready))
    order = list(ready)
    rng.shuffle(order)  # which file gets the extra question of an uneven split
    for source, quota in zip(order, quotas, strict=True):
        await take(source, quota)
    # Files that could not give their share are compensated by the others.
    remaining = test.question_count - sum(len(v) for v in picked.values())
    for source in sorted(ready, key=lambda s: -len(picked[s.id])):
        if remaining <= 0:
            break
        remaining -= await take(source, remaining)

    groups = [picked[s.id] for s in ready]
    for g in groups:
        rng.shuffle(g)
    ordered: list[Question] = interleave(groups, rng)
    by_question = {q.id: s for s in ready for q in picked[s.id]}
    topic_ids = {q.topic_id for q in ordered if q.topic_id}
    names: dict[int, str] = {}
    if topic_ids:
        names = dict((await session.execute(select(Topic.id, Topic.name).where(Topic.id.in_(topic_ids)))).all())
    await session.execute(TestQuestion.__table__.delete().where(TestQuestion.test_id == test_id))
    for position, question in enumerate(ordered, start=1):
        source = by_question[question.id]
        tq = snapshot_question(test, question, position, rng, names.get(question.topic_id or 0, ""), source)
        tq.is_approved = True  # every question passed automatic source verification
        session.add(tq)
    await session.commit()
    report.created = len(ordered)
    report.per_source = {s.source_name: len(picked[s.id]) for s in ready}
