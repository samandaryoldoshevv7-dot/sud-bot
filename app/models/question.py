from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
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
