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
    DEFAULT_DURATION,
    AnswerReveal,
    AssignmentStatus,
    AttemptStatus,
    DeliveryMode,
    Difficulty,
    GenerationStatus,
    SourceKind,
    TestAudience,
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
    delivery_mode: Mapped[DeliveryMode] = mapped_column(
        str_enum(DeliveryMode, "delivery_mode"), default=DeliveryMode.PRIVATE, server_default="private", nullable=False
    )
    # Availability window: employees can START the test between starts_at and deadline_at.
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    # Personal time limit: an employee's attempt ends at started_at + duration_seconds.
    duration_seconds: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_DURATION, server_default=str(DEFAULT_DURATION), nullable=False
    )
    audience: Mapped[TestAudience] = mapped_column(
        str_enum(TestAudience, "test_audience"), default=TestAudience.ALL, server_default="all", nullable=False
    )
    paused: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
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
    sources: Mapped[list[TestSource]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, order_by="TestSource.position"
    )
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


class TestSource(Base):
    """A source document of a test (e.g. "Konstitutsiya" ← Konstitutsiya.pdf)."""

    __test__ = False
    __tablename__ = "test_sources"
    __table_args__ = (UniqueConstraint("test_id", "material_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    test_id: Mapped[int] = mapped_column(ForeignKey("tests.id", ondelete="CASCADE"), nullable=False, index=True)
    material_id: Mapped[int | None] = mapped_column(ForeignKey("materials.id", ondelete="SET NULL"))
    source_name: Mapped[str] = mapped_column(String(255), nullable=False)
    original_file_name: Mapped[str | None] = mapped_column(String(255))
    file_id: Mapped[str | None] = mapped_column(String(255))
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)


class TestAssignment(TimestampMixin, Base):
    """Per-employee assignment of a test (selected employees) and per-employee admin decisions:
    taking the test away from someone, or allowing one more attempt."""

    __test__ = False
    __tablename__ = "test_assignments"
    __table_args__ = (UniqueConstraint("test_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    test_id: Mapped[int] = mapped_column(ForeignKey("tests.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    status: Mapped[AssignmentStatus] = mapped_column(
        str_enum(AssignmentStatus, "assignment_status"), default=AssignmentStatus.ASSIGNED, nullable=False
    )
    retake_allowed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    assigned_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


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
    source_id: Mapped[int | None] = mapped_column(ForeignKey("test_sources.id", ondelete="SET NULL"), index=True)
    source_name: Mapped[str] = mapped_column(String(255), default="", server_default="", nullable=False)
    source_file: Mapped[str | None] = mapped_column(String(255))
    is_approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    test: Mapped[Test] = relationship(back_populates="questions")
    option_rows: Mapped[list[QuestionOption]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, order_by="QuestionOption.letter"
    )


class QuestionOption(Base):
    """One answer option of a test-question snapshot (immutable once the test is published)."""

    __tablename__ = "question_options"
    __table_args__ = (UniqueConstraint("test_question_id", "letter"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    test_question_id: Mapped[int] = mapped_column(ForeignKey("test_questions.id", ondelete="CASCADE"), nullable=False)
    letter: Mapped[str] = mapped_column(String(1), nullable=False)  # snapshot (original) letter
    text: Mapped[str] = mapped_column(Text, nullable=False)
    is_correct: Mapped[bool] = mapped_column(Boolean, nullable=False)


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
    """An employee's answer. Exactly ONE answer per question within an attempt — enforced by a unique
    constraint — and rows are immutable (a database trigger rejects every UPDATE). A new attempt
    (and so a new answer) is only possible when an admin explicitly allows a retake."""

    __tablename__ = "user_answers"
    __table_args__ = (
        UniqueConstraint("attempt_id", "test_question_id"),
        Index("ix_user_answers_user_correct", "user_id", "is_correct"),
        Index("ix_user_answers_test_question", "test_id", "test_question_id"),
        CheckConstraint("selected_option IN ('A','B','C','D','E')", name="selected_option_letter"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    attempt_id: Mapped[int] = mapped_column(ForeignKey("test_attempts.id", ondelete="CASCADE"), nullable=False)
    test_id: Mapped[int] = mapped_column(ForeignKey("tests.id", ondelete="CASCADE"), nullable=False)
    test_question_id: Mapped[int] = mapped_column(
        ForeignKey("test_questions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    selected_option: Mapped[str] = mapped_column(String(1), nullable=False)  # original (snapshot) letter
    selected_option_id: Mapped[int | None] = mapped_column(ForeignKey("question_options.id", ondelete="SET NULL"))
    selected_display: Mapped[str] = mapped_column(String(1), nullable=False)  # letter the employee saw
    correct_option: Mapped[str] = mapped_column(String(1), nullable=False)  # original letter (snapshot)
    correct_display: Mapped[str | None] = mapped_column(String(1))  # correct letter as the employee saw it
    is_correct: Mapped[bool] = mapped_column(Boolean, nullable=False)
    answered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GroupTestPost(TimestampMixin, Base):
    """A test running inside a Telegram group (header message + one message per question)."""

    __tablename__ = "group_test_posts"
    __table_args__ = (UniqueConstraint("test_id", "group_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    test_id: Mapped[int] = mapped_column(ForeignKey("tests.id", ondelete="CASCADE"), nullable=False)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    header_message_id: Mapped[int | None] = mapped_column(Integer)
    posted_all: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    messages: Mapped[list[GroupQuestionMessage]] = relationship(
        back_populates="post",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="GroupQuestionMessage.position",
    )


class GroupQuestionMessage(Base):
    """One shared question message in the group. ``opts[i]`` = snapshot letter shown as A, B, C..."""

    __tablename__ = "group_question_messages"
    __table_args__ = (UniqueConstraint("post_id", "test_question_id"), UniqueConstraint("post_id", "position"))

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("group_test_posts.id", ondelete="CASCADE"), nullable=False)
    test_question_id: Mapped[int] = mapped_column(ForeignKey("test_questions.id", ondelete="CASCADE"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)  # 0-based
    opts: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    message_id: Mapped[int | None] = mapped_column(Integer)
    rendered_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rendered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    post: Mapped[GroupTestPost] = relationship(back_populates="messages")
