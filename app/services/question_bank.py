"""Question bank: listing, filtering, review, editing, deletion and regeneration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Difficulty, Question, QuestionStatus, SourceChunk, Topic
from app.services.question_validation import check_question
from app.utils.text import text_hash
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

PAGE_SIZE = 8


@dataclass
class QuestionFilter:
    status: str | None = None  # pending | approved | rejected
    topic_id: int | None = None
    material_id: int | None = None
    news_only: bool = False
    difficulty: str | None = None
    query: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict | None) -> QuestionFilter:
        data = data or {}
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def _apply(stmt: Select, flt: QuestionFilter) -> Select:
    if flt.status:
        stmt = stmt.where(Question.status == QuestionStatus(flt.status))
    if flt.topic_id:
        stmt = stmt.where(Question.topic_id == flt.topic_id)
    if flt.material_id:
        stmt = stmt.where(Question.source_material_id == flt.material_id)
    if flt.news_only:
        stmt = stmt.where(Question.source_news_id.is_not(None))
    if flt.difficulty:
        stmt = stmt.where(Question.difficulty == Difficulty(flt.difficulty))
    q = flt.query.strip()
    if q:
        if q.isdigit():
            stmt = stmt.where(or_(Question.id == int(q), Question.question_text.ilike(f"%{q}%")))
        else:
            stmt = stmt.where(or_(Question.question_text.ilike(f"%{q}%"), Question.source_reference.ilike(f"%{q}%")))
    return stmt


async def list_questions(session: AsyncSession, flt: QuestionFilter, page: int) -> tuple[list[Question], int]:
    total = (await session.execute(_apply(select(func.count(Question.id)), flt))).scalar_one()
    stmt = _apply(select(Question), flt).order_by(Question.id.desc()).offset(page * PAGE_SIZE).limit(PAGE_SIZE)
    return list((await session.execute(stmt)).unique().scalars().all()), total


async def bank_counts(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(select(Question.status, func.count(Question.id)).group_by(Question.status))
    counts = {s.value: 0 for s in QuestionStatus}
    for status, n in rows:
        counts[status.value] = n
    counts["total"] = sum(counts.values())
    return counts


async def topics_with_counts(session: AsyncSession) -> list[tuple[Topic, int]]:
    stmt = (
        select(Topic, func.count(Question.id))
        .join(Question, Question.topic_id == Topic.id)
        .where(Question.status != QuestionStatus.REJECTED)
        .group_by(Topic.id)
        .order_by(func.count(Question.id).desc())
        .limit(30)
    )
    return [(t, int(n)) for t, n in await session.execute(stmt)]


async def set_status(
    session: AsyncSession, question_id: int, status: QuestionStatus, reviewer_id: int | None
) -> Question | None:
    question = await session.get(Question, question_id)
    if question is None:
        return None
    question.status = status
    question.reviewed_by_id = reviewer_id
    question.reviewed_at = utcnow()
    await session.commit()
    logger.info("Question reviewed", extra={"question_id": question_id, "status": status.value})
    return question


async def delete_question(session: AsyncSession, question_id: int) -> bool:
    """Hard delete. Test snapshots keep their copy (FK is SET NULL), so results stay intact."""
    question = await session.get(Question, question_id)
    if question is None:
        return False
    await session.delete(question)
    await session.commit()
    return True


EDITABLE_FIELDS = ("question", "A", "B", "C", "D", "E", "correct", "explanation", "topic", "difficulty")


@dataclass
class EditResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    question: Question | None = None


async def apply_edit(
    session: AsyncSession, question_id: int, field_name: str, value: str, editor_id: int | None
) -> EditResult:
    """Edit one field, re-run the deterministic checks and bump the version.

    Editing never touches test snapshots (old results remain reproducible). The edited question
    goes back to PENDING review unless only the topic/difficulty changed.
    """
    question = await session.get(Question, question_id)
    if question is None:
        return EditResult(False, ["not_found"])
    value = value.strip()
    options = dict(question.options)
    text_ = question.question_text
    correct = question.correct_option
    explanation = question.explanation
    content_changed = True

    if field_name == "question":
        text_ = value
    elif field_name in ("A", "B", "C", "D", "E"):
        if field_name not in options:
            return EditResult(False, ["option_missing"])
        options[field_name] = value
    elif field_name == "correct":
        value = value.upper()
        if value not in options:
            return EditResult(False, ["correct_not_in_options"])
        correct = value
    elif field_name == "explanation":
        explanation = value
    elif field_name == "topic":
        from app.services.question_generation import get_or_create_topic

        topic = await get_or_create_topic(session, value)
        question.topic_id = topic.id if topic else None
        content_changed = False
    elif field_name == "difficulty":
        question.difficulty = Difficulty(value)
        content_changed = False
    else:
        return EditResult(False, ["unknown_field"])

    if content_changed:
        sources: list[tuple[int, str]] = []
        if question.source_chunk_id:
            chunk = await session.get(SourceChunk, question.source_chunk_id)
            if chunk:
                sources = [(chunk.id, chunk.text)]
        check = check_question(
            question=text_,
            options=options,
            correct=correct,
            explanation=explanation,
            excerpt=question.source_excerpt,
            expected_options=None,
            sources=sources,
            min_excerpt_score=80,
        )
        if not check.ok:
            return EditResult(False, check.errors)
        new_hash = text_hash(text_)
        if new_hash != question.text_hash:
            clash = (
                await session.execute(
                    select(Question.id).where(Question.text_hash == new_hash, Question.id != question.id)
                )
            ).scalar_one_or_none()
            if clash:
                return EditResult(False, ["duplicate"])
        question.question_text = text_
        question.options = options
        question.correct_option = correct
        question.explanation = explanation
        question.text_hash = new_hash
        question.status = QuestionStatus.PENDING
        question.validation = {**(question.validation or {}), "edited_by_admin": True}
    question.version += 1
    question.reviewed_by_id = editor_id
    question.reviewed_at = utcnow()
    await session.commit()
    logger.info("Question edited", extra={"question_id": question_id, "field": field_name})
    return EditResult(True, [], question)
