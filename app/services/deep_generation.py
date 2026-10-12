"""🧠 Maksimal savol yaratish: up to 300 unique, source-grounded questions from ONE material.

The material is read chunk by chunk (articles, items, deadlines, powers, exceptions — whatever the
chunk holds), several passes over the whole text. A chunk that gives no new unique question is
marked exhausted; when every chunk is exhausted the material has no more unique questions and the
run ends with the number it really reached (e.g. 126), never padded with near-duplicates.

Progress is a checkpoint in ``material_generation_jobs`` (pass, last chunk, counters) and every
question is saved the moment it passes the checks. An API limit, an outage or a restart therefore
only pauses the run: resuming continues from the checkpoint and saved questions are never made
again. "Paused (API limit / error)" and "exhausted (nothing left in the material)" are separate
states, so the admin is never told a material is used up when only the AI quota ran out.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.base import AIError
from app.models import (
    Difficulty,
    Material,
    MaterialGenerationJob,
    MaterialStatus,
    Question,
    QuestionStatus,
    SourceChunk,
)
from app.services.question_generation import QuestionGenerator
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

MAX_QUESTIONS = 300
PER_CALL = 3  # questions asked from one chunk in one request
MAX_PASSES = 12  # passes over the material (one request per chunk each); a chunk stops once it gives nothing new
DUPLICATE_RETRIES = 2  # a chunk whose answer was only already-known rules is asked again at once
MIN_CHUNK_CHARS = 120
MAX_CONSECUTIVE_ERRORS = 3

RUNNING, PAUSED, INTERRUPTED, EXHAUSTED, DONE, FAILED = (
    "running", "paused", "interrupted", "exhausted", "done", "failed",
)  # fmt: skip
STOP_ERRORS = ("rate limit", "authentication", "unavailable", "unreachable", "not configured", "no ai api key")

ProgressFn = Callable[[int, int, int, int], Awaitable[None]]  # created total, target, chunks done, chunks
GeneratorFactory = Callable[[], QuestionGenerator]


@dataclass
class MaterialQuestionStats:
    total: int = 0  # live (not rejected) questions of the material
    medium: int = 0
    hard: int = 0
    easy: int = 0  # older questions made before the medium/hard rule
    rejected: int = 0
    chunks: int = 0
    chunks_exhausted: int = 0
    job: MaterialGenerationJob | None = None

    @property
    def remaining_chunks(self) -> int:
        return max(0, self.chunks - self.chunks_exhausted)


@dataclass
class DeepReport:
    status: str = RUNNING
    created_now: int = 0
    total: int = 0
    target: int = MAX_QUESTIONS
    duplicates_now: int = 0
    rejected_now: int = 0
    medium: int = 0
    hard: int = 0
    error: str | None = None
    duplicate_reasons: Counter = field(default_factory=Counter)


async def material_stats(session: AsyncSession, material_id: int) -> MaterialQuestionStats:
    stats = MaterialQuestionStats()
    rows = await session.execute(
        select(Question.status, Question.difficulty, func.count(Question.id))
        .where(Question.source_material_id == material_id)
        .group_by(Question.status, Question.difficulty)
    )
    for status, difficulty, n in rows:
        if status == QuestionStatus.REJECTED:
            stats.rejected += n
            continue
        stats.total += n
        if difficulty == Difficulty.HARD:
            stats.hard += n
        elif difficulty == Difficulty.MEDIUM:
            stats.medium += n
        else:
            stats.easy += n
    usable = (SourceChunk.material_id == material_id, SourceChunk.char_count >= MIN_CHUNK_CHARS)
    stats.chunks = (await session.execute(select(func.count(SourceChunk.id)).where(*usable))).scalar_one()
    stats.chunks_exhausted = (
        await session.execute(select(func.count(SourceChunk.id)).where(*usable, SourceChunk.gen_exhausted.is_(True)))
    ).scalar_one()
    stats.job = await session.get(MaterialGenerationJob, material_id)
    return stats


async def claim(
    session: AsyncSession, material_id: int, target: int = MAX_QUESTIONS, notify_chat_id: int | None = None
) -> MaterialGenerationJob | None:
    """Start or resume the material's job. None when it is already running (one run per material)."""
    job = (
        await session.execute(
            select(MaterialGenerationJob).where(MaterialGenerationJob.material_id == material_id).with_for_update()
        )
    ).scalar_one_or_none()
    if job is None:
        job = MaterialGenerationJob(
            material_id=material_id, status=RUNNING, target=target, created=0, duplicates=0, rejected=0,
            pass_no=1, cursor=-1, notify_chat_id=notify_chat_id,
        )  # fmt: skip
        session.add(job)
    elif job.status == RUNNING:
        await session.commit()
        return None
    else:
        material = await session.get(Material, material_id)
        if material is not None and material.processed_at and job.updated_at and material.processed_at > job.updated_at:
            job.pass_no, job.cursor = 1, -1  # the file was read again: new chunks, start from the top
        if job.status in (EXHAUSTED, DONE, FAILED):
            # A new run over a finished material (e.g. after "more questions" or new chunks): start a
            # fresh pass. Exhausted chunks stay exhausted, so nothing is repeated.
            job.pass_no, job.cursor = 1, -1
        job.status, job.target, job.last_error, job.finished_at = RUNNING, target, None, None
        if notify_chat_id:
            job.notify_chat_id = notify_chat_id
    await session.commit()
    return job


async def mark_interrupted(session: AsyncSession) -> list[int]:
    """At startup: runs cut off by a restart are paused at their checkpoint (resumable)."""
    ids = list(
        (
            await session.execute(
                update(MaterialGenerationJob)
                .where(MaterialGenerationJob.status == RUNNING)
                .values(status=INTERRUPTED, last_error="interrupted_by_restart")
                .returning(MaterialGenerationJob.material_id)
            )
        ).scalars()
    )
    await session.commit()
    return ids


def _plan(medium: int, hard: int, n: int) -> list[str]:
    """Difficulties to ask for, steering the material towards half medium, half hard."""
    out = []
    for _ in range(n):
        level = "hard" if hard < medium else "medium"
        out.append(level)
        if level == "hard":
            hard += 1
        else:
            medium += 1
    return out


async def run(
    session_maker: async_sessionmaker[AsyncSession],
    material_id: int,
    generator_factory: GeneratorFactory,
    progress: ProgressFn | None = None,
    target: int = MAX_QUESTIONS,
    notify_chat_id: int | None = None,
) -> DeepReport:
    target = max(1, min(target, MAX_QUESTIONS))
    report = DeepReport(target=target)
    async with session_maker() as session:
        material = await session.get(Material, material_id)
        if material is None or material.status != MaterialStatus.READY:
            report.status, report.error = FAILED, "material_not_ready"
            return report
        job = await claim(session, material_id, target, notify_chat_id)
        if job is None:
            report.status, report.error = RUNNING, "already_running"
            return report
    try:
        await _run(session_maker, material_id, generator_factory, progress, target, report)
    except Exception as exc:  # never leave the job "running" forever
        logger.exception("Deep generation crashed", extra={"material_id": material_id})
        report.status, report.error = PAUSED, f"{type(exc).__name__}: {str(exc)[:200]}"
    async with session_maker() as session:
        job = await session.get(MaterialGenerationJob, material_id)
        if job is not None:
            job.status = report.status
            job.last_error = report.error
            job.created += report.created_now
            job.duplicates += report.duplicates_now
            job.rejected += report.rejected_now
            if report.status in (EXHAUSTED, DONE):
                job.finished_at = utcnow()
            await session.commit()
        stats = await material_stats(session, material_id)
    report.total, report.medium, report.hard = stats.total, stats.medium, stats.hard
    logger.info(
        "Deep generation finished",
        extra={"material_id": material_id, "status": report.status, "created_count": report.created_now,
               "total": report.total, "duplicates": report.duplicates_now},
    )  # fmt: skip
    return report


async def _run(
    session_maker: async_sessionmaker[AsyncSession],
    material_id: int,
    generator_factory: GeneratorFactory,
    progress: ProgressFn | None,
    target: int,
    report: DeepReport,
) -> None:
    try:
        generator = generator_factory()
    except AIError as exc:
        report.status, report.error = PAUSED, str(exc)[:300]
        return
    report.duplicate_reasons = generator.duplicates  # filled as the run goes
    errors_in_row = 0
    async with session_maker() as session:
        job = await session.get(MaterialGenerationJob, material_id)
        assert job is not None
        stats = await material_stats(session, material_id)
        medium, hard, have = stats.medium, stats.hard, stats.total
        await session.execute(  # too short to hold a question: never asked
            update(SourceChunk)
            .where(SourceChunk.material_id == material_id, SourceChunk.char_count < MIN_CHUNK_CHARS)
            .values(gen_exhausted=True)
        )
        await session.commit()
        while job.pass_no <= MAX_PASSES:
            if have >= target:
                report.status = DONE
                return
            full_pass = job.cursor < 0  # a resumed run starts in the middle of a pass
            chunk_ids = list(
                (
                    await session.execute(
                        select(SourceChunk.id)
                        .where(
                            SourceChunk.material_id == material_id,
                            SourceChunk.gen_exhausted.is_(False),
                            SourceChunk.chunk_index > job.cursor,
                        )
                        .order_by(SourceChunk.chunk_index)
                    )
                ).scalars()
            )
            new_in_pass = 0
            learning = False
            for chunk_id in chunk_ids:
                if have >= target:
                    report.status = DONE
                    return
                chunk = await session.get(SourceChunk, chunk_id, populate_existing=True)
                if chunk is None:
                    continue
                n = min(PER_CALL, target - have)
                created = []
                failed = only_known = False
                for _attempt in range(1 + DUPLICATE_RETRIES):
                    try:
                        result = await generator.generate_from_chunk(session, chunk, n, _plan(medium, hard, n))
                    except AIError as exc:
                        await session.rollback()
                        message = str(exc)
                        logger.warning("Deep generation: AI error", extra={"material_id": material_id,
                                                                           "chunk_id": chunk_id, "error": message[:200]})  # fmt: skip
                        errors_in_row += 1
                        if any(m in message.lower() for m in STOP_ERRORS) or errors_in_row >= MAX_CONSECUTIVE_ERRORS:
                            report.status, report.error = PAUSED, message[:300]
                            return  # the checkpoint points to the chunk before this one: it is retried
                        failed = True
                        break
                    errors_in_row = 0
                    created = result.questions
                    report.created_now += len(created)
                    report.duplicates_now += result.rejected.get("duplicate", 0)
                    report.rejected_now += sum(v for k, v in result.rejected.items() if k != "duplicate")
                    only_known = not created and bool(result.rejected.get("duplicate"))
                    if not only_known:
                        break
                    # Only rules the bank already has (e.g. from another file): the next request lists
                    # them as covered, so the model can still find this chunk's new facts.
                if failed:
                    continue
                for q in created:
                    if q.difficulty == Difficulty.HARD:
                        hard += 1
                    else:
                        medium += 1
                have += len(created)
                new_in_pass += len(created)
                if only_known:
                    learning = True  # not used up: asked again next pass, the known rules listed as covered
                elif not created:
                    chunk.gen_exhausted = True  # nothing new here any more
                job = await session.get(MaterialGenerationJob, material_id)  # type: ignore[assignment]
                job.cursor = chunk.chunk_index
                job.updated_at = utcnow()
                await session.commit()
                if progress:
                    stats_now = await material_stats(session, material_id)
                    await progress(have, target, stats_now.chunks_exhausted, stats_now.chunks)
            remaining = (
                await session.execute(
                    select(func.count(SourceChunk.id)).where(
                        SourceChunk.material_id == material_id, SourceChunk.gen_exhausted.is_(False)
                    )
                )
            ).scalar_one()
            if remaining == 0 or (full_pass and new_in_pass == 0 and not learning):
                break  # every chunk is used up, or a whole pass over the material found nothing new
            job = await session.get(MaterialGenerationJob, material_id)  # type: ignore[assignment]
            job.pass_no += 1
            job.cursor = -1
            await session.commit()
        # Every chunk gave all it could (or the pass limit was reached): the material is used up.
        await session.execute(
            update(SourceChunk).where(SourceChunk.material_id == material_id).values(gen_exhausted=True)
        )
        await session.commit()
        report.status = DONE if have >= target else EXHAUSTED
