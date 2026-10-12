from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin, str_enum
from app.models.enums import Difficulty, QuestionOrigin, QuestionStatus, SourceKind


class Topic(TimestampMixin, Base):
    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)


class Question(TimestampMixin, Base):
    """A reusable, source-grounded multiple-choice question in the question bank.

    ``options`` is an ordered mapping ``{"A": "...", "B": "...", ...}`` (3–5 options).
    """

    __tablename__ = "questions"
    __table_args__ = (
        CheckConstraint("option_count BETWEEN 3 AND 5", name="option_count_range"),
        CheckConstraint("correct_option IN ('A','B','C','D','E')", name="correct_option_letter"),
        CheckConstraint(
            "(source_kind = 'material' AND source_material_id IS NOT NULL) OR "
            "(source_kind = 'news' AND source_news_id IS NOT NULL) OR "
            "(source_material_id IS NULL AND source_news_id IS NULL)",
            name="source_matches_kind",
        ),
        Index("ix_questions_status_kind", "status", "source_kind"),
        Index("ix_questions_text_hash", "text_hash", unique=True),
        # The same legal rule is tested by at most one live question (rejected ones do not block it).
        Index(
            "uq_questions_fact_hash",
            "fact_hash",
            unique=True,
            postgresql_where=text("fact_hash IS NOT NULL AND status <> 'rejected'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    options: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    option_count: Mapped[int] = mapped_column(Integer, nullable=False)
    correct_option: Mapped[str] = mapped_column(String(1), nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    topic_id: Mapped[int | None] = mapped_column(ForeignKey("topics.id", ondelete="SET NULL"), index=True)
    difficulty: Mapped[Difficulty] = mapped_column(
        str_enum(Difficulty, "difficulty"), default=Difficulty.MEDIUM, nullable=False, index=True
    )
    status: Mapped[QuestionStatus] = mapped_column(
        str_enum(QuestionStatus, "question_status"), default=QuestionStatus.PENDING, nullable=False
    )
    origin: Mapped[QuestionOrigin] = mapped_column(
        str_enum(QuestionOrigin, "question_origin"), default=QuestionOrigin.AI, nullable=False
    )
    source_kind: Mapped[SourceKind] = mapped_column(str_enum(SourceKind, "source_kind"), nullable=False)
    source_material_id: Mapped[int | None] = mapped_column(ForeignKey("materials.id", ondelete="SET NULL"), index=True)
    source_news_id: Mapped[int | None] = mapped_column(ForeignKey("news.id", ondelete="SET NULL"), index=True)
    source_chunk_id: Mapped[int | None] = mapped_column(ForeignKey("source_chunks.id", ondelete="SET NULL"), index=True)
    source_page: Mapped[str | None] = mapped_column(String(32))
    source_reference: Mapped[str] = mapped_column(Text, nullable=False)
    source_excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Duplicate detection: the rule/fact the question tests (one sentence from the AI, checked by
    # the server), its normalized hash, the normalized correct answer and an optional meaning vector.
    article_ref: Mapped[str | None] = mapped_column(String(120))  # "12-modda 2-qism" as in the source
    fact: Mapped[str | None] = mapped_column(Text)
    fact_hash: Mapped[str | None] = mapped_column(String(64))
    answer_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    dedup_vec: Mapped[list[float] | None] = mapped_column(JSONB)
    validation: Mapped[dict | None] = mapped_column(JSONB)  # internal AI metadata, never shown to employees
    confidence: Mapped[float | None] = mapped_column(Float)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    times_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    topic: Mapped[Topic | None] = relationship(lazy="joined")

    @property
    def topic_name(self) -> str:
        return self.topic.name if self.topic else "—"


class MaterialGenerationJob(TimestampMixin, Base):
    """Checkpoint of the "up to 300 questions" run for one material: survives restarts and API
    limits, so a resumed run continues where it stopped and never re-creates saved questions."""

    __tablename__ = "material_generation_jobs"

    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id", ondelete="CASCADE"), primary_key=True)
    # running | paused (API limit / errors) | interrupted (restart) | exhausted (no more unique
    # questions in the material) | done (target reached) | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    target: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # by this job, all runs
    duplicates: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pass_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    cursor: Mapped[int] = mapped_column(Integer, nullable=False, default=-1)  # last chunk_index done in the pass
    last_error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notify_chat_id: Mapped[int | None] = mapped_column(BigInteger)
