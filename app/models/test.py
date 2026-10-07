from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin, str_enum
from app.models.enums import (
    AnswerReveal,
    AttemptStatus,
    Difficulty,
    GenerationStatus,
    SourceKind,
    TestDifficulty,
    TestStatus,
)


class Test(TimestampMixin, Base):
    __test__ = False  # not a pytest test class
    __tablename__ = "tests"
    __table_args__ = (
        CheckConstraint("question_count BETWEEN 1 AND 100", name="question_count_range"),
        CheckConstraint("option_count BETWEEN 3 AND 5", name="option_count_range"),
        CheckConstraint("news_percent BETWEEN 0 AND 100", name="news_percent_range"),
        CheckConstraint("passing_percent BETWEEN 0 AND 100", name="passing_percent_range"),
        CheckConstraint("deadline_at > starts_at", name="deadline_after_start"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    question_count: Mapped[int] = mapped_column(Integer, nullable=False)
    option_count: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    difficulty: Mapped[TestDifficulty] = mapped_column(
        str_enum(TestDifficulty, "test_difficulty"), default=TestDifficulty.MIXED, nullable=False
    )
    news_percent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    use_all_materials: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    focus_query: Mapped[str | None] = mapped_column(String(500))
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id", ondelete="SET NULL"))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    randomize_questions: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    randomize_options: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    answer_reveal: Mapped[AnswerReveal] = mapped_column(
        str_enum(AnswerReveal, "answer_reveal"), default=AnswerReveal.AFTER_COMPLETION, nullable=False
    )
    allow_retakes: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    passing_percent: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    status: Mapped[TestStatus] = mapped_column(
        str_enum(TestStatus, "test_status"), default=TestStatus.DRAFT, nullable=False, index=True
    )
    generation_status: Mapped[GenerationStatus] = mapped_column(
        str_enum(GenerationStatus, "generation_status"), default=GenerationStatus.IDLE, nullable=False
    )
    generation_error: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    announced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    materials: Mapped[list[TestMaterial]] = relationship(cascade="all, delete-orphan", passive_deletes=True)
    questions: Mapped[list[TestQuestion]] = relationship(
        back_populates="test",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="TestQuestion.position",
    )


class TestMaterial(Base):
    __test__ = False
    __tablename__ = "test_materials"
    __table_args__ = (UniqueConstraint("test_id", "material_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    test_id: Mapped[int] = mapped_column(ForeignKey("tests.id", ondelete="CASCADE"), nullable=False, index=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id", ondelete="CASCADE"), nullable=False)


class TestQuestion(Base):
    """Immutable snapshot of a question as included in a test.

    Old results stay reproducible even if the bank question is later edited or deleted.
    """

    __test__ = False
    __tablename__ = "test_questions"
    __table_args__ = (
        UniqueConstraint("test_id", "position"),
        Index(
            "uq_test_questions_test_question",
            "test_id",
            "question_id",
            unique=True,
            postgresql_where=text("question_id IS NOT NULL"),
        ),
        CheckConstraint("correct_option IN ('A','B','C','D','E')", name="correct_option_letter"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    test_id: Mapped[int] = mapped_column(ForeignKey("tests.id", ondelete="CASCADE"), nullable=False)
    question_id: Mapped[int | None] = mapped_column(ForeignKey("questions.id", ondelete="SET NULL"), index=True)
    question_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    options: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    correct_option: Mapped[str] = mapped_column(String(1), nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    topic_id: Mapped[int | None] = mapped_column(ForeignKey("topics.id", ondelete="SET NULL"), index=True)
    topic_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    difficulty: Mapped[Difficulty] = mapped_column(str_enum(Difficulty, "tq_difficulty"), nullable=False)
    source_kind: Mapped[SourceKind] = mapped_column(str_enum(SourceKind, "tq_source_kind"), nullable=False)
    source_reference: Mapped[str] = mapped_column(Text, nullable=False)
    source_excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    is_approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    test: Mapped[Test] = relationship(back_populates="questions")


class TestAttempt(TimestampMixin, Base):
    """One employee's attempt at a test.

    ``layout`` stores the per-attempt presentation: ``[{"tq": <test_question_id>,
    "opts": ["C", "A", "D", "B"]}, ...]`` where ``opts[i]`` is the ORIGINAL letter shown
    at display position ``i`` (display letters are always A, B, C...). Answer checking maps
    the display letter back to the original letter and compares it with the stored
    ``correct_option`` of the snapshot, so randomisation can never mis-score an answer.
    """

    __test__ = False
    __tablename__ = "test_attempts"
    __table_args__ = (
        UniqueConstraint("test_id", "user_id", "attempt_no"),
        Index(
            "uq_test_attempts_one_in_progress",
            "test_id",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'IN_PROGRESS'"),
        ),
        Index("ix_test_attempts_user_status", "user_id", "status"),
        Index("ix_test_attempts_test_status", "test_id", "status"),
        CheckConstraint("answered_count <= total_questions", name="answered_le_total"),
        CheckConstraint("correct_count <= answered_count", name="correct_le_answered"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    test_id: Mapped[int] = mapped_column(ForeignKey("tests.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[AttemptStatus] = mapped_column(
        str_enum(AttemptStatus, "attempt_status"), default=AttemptStatus.IN_PROGRESS, nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    total_questions: Mapped[int] = mapped_column(Integer, nullable=False)
    answered_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    correct_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    incorrect_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    score_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=0, nullable=False)
    passed: Mapped[bool | None] = mapped_column(Boolean)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    current_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    layout: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    last_message_id: Mapped[int | None] = mapped_column(Integer)
    cancelled_reason: Mapped[str | None] = mapped_column(String(255))

    test: Mapped[Test] = relationship(lazy="joined")


class UserAnswer(Base):
    __tablename__ = "user_answers"
    __table_args__ = (
        UniqueConstraint("attempt_id", "test_question_id"),
        Index("ix_user_answers_user_correct", "user_id", "is_correct"),
        CheckConstraint("selected_option IN ('A','B','C','D','E')", name="selected_option_letter"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    attempt_id: Mapped[int] = mapped_column(ForeignKey("test_attempts.id", ondelete="CASCADE"), nullable=False)
    test_question_id: Mapped[int] = mapped_column(
        ForeignKey("test_questions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    selected_option: Mapped[str] = mapped_column(String(1), nullable=False)  # original letter
    selected_display: Mapped[str] = mapped_column(String(1), nullable=False)  # letter the employee saw
    correct_option: Mapped[str] = mapped_column(String(1), nullable=False)  # original letter (snapshot)
    is_correct: Mapped[bool] = mapped_column(Boolean, nullable=False)
    answered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
