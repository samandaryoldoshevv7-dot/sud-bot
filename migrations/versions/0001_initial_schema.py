"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-07 18:12:36.192247
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.rag.schema import add_embedding_column

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_embedding_column() -> None:
    """Create ``source_chunks.embedding`` as ``vector(N)`` when pgvector is installable,
    otherwise as ``real[]`` (cosine similarity is then computed in Python)."""
    add_embedding_column(op.get_bind())


def upgrade() -> None:
    op.create_table(
        "bot_settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_bot_settings")),
    )
    op.create_table(
        "topics",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("normalized_name", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_topics")),
        sa.UniqueConstraint("normalized_name", name=op.f("uq_topics_normalized_name")),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("first_name", sa.String(length=255), nullable=True),
        sa.Column("last_name", sa.String(length=255), nullable=True),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("full_name", sa.String(length=255), nullable=True),
        sa.Column(
            "role",
            sa.Enum(
                "admin",
                "employee",
                name="user_role",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "active",
                "inactive",
                name="user_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("has_private_chat", sa.Boolean(), nullable=False),
        sa.Column("bot_blocked", sa.Boolean(), nullable=False),
        sa.Column("language", sa.String(length=8), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('admin', 'employee')", name=op.f("ck_users_user_role")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'inactive')",
            name=op.f("ck_users_user_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("telegram_id", name=op.f("uq_users_telegram_id")),
    )
    op.create_index(op.f("ix_users_status"), "users", ["status"], unique=False)
    op.create_index(op.f("ix_users_username"), "users", ["username"], unique=False)
    op.create_table(
        "groups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("registered_by_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["registered_by_id"],
            ["users.id"],
            name=op.f("fk_groups_registered_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_groups")),
        sa.UniqueConstraint("chat_id", name=op.f("uq_groups_chat_id")),
    )
    op.create_table(
        "materials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=True),
        sa.Column(
            "file_type",
            sa.Enum(
                "pdf",
                "docx",
                "txt",
                "md",
                "text",
                name="file_type",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("telegram_file_id", sa.String(length=255), nullable=True),
        sa.Column("telegram_file_unique_id", sa.String(length=128), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("category", sa.String(length=128), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "UPLOADED",
                "PROCESSING",
                "READY",
                "FAILED",
                name="material_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("uploaded_by_id", sa.Integer(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "file_type IN ('pdf', 'docx', 'txt', 'md', 'text')",
            name=op.f("ck_materials_file_type"),
        ),
        sa.CheckConstraint(
            "status IN ('UPLOADED', 'PROCESSING', 'READY', 'FAILED')",
            name=op.f("ck_materials_material_status"),
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by_id"],
            ["users.id"],
            name=op.f("fk_materials_uploaded_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_materials")),
    )
    op.create_index(
        op.f("ix_materials_category"), "materials", ["category"], unique=False
    )
    op.create_index(
        op.f("ix_materials_content_hash"), "materials", ["content_hash"], unique=False
    )
    op.create_index(op.f("ix_materials_status"), "materials", ["status"], unique=False)
    op.create_table(
        "news",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("news_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=255), nullable=True),
        sa.Column("category", sa.String(length=128), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "UPLOADED",
                "PROCESSING",
                "READY",
                "FAILED",
                name="news_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('UPLOADED', 'PROCESSING', 'READY', 'FAILED')",
            name=op.f("ck_news_news_status"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_news_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_news")),
    )
    op.create_index(op.f("ix_news_is_active"), "news", ["is_active"], unique=False)
    op.create_index(op.f("ix_news_news_date"), "news", ["news_date"], unique=False)
    op.create_table(
        "group_members",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("is_member", sa.Boolean(), nullable=False),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["groups.id"],
            name=op.f("fk_group_members_group_id_groups"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_group_members_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_group_members")),
        sa.UniqueConstraint(
            "group_id", "user_id", name=op.f("uq_group_members_group_id_user_id")
        ),
    )
    op.create_index(
        "ix_group_members_user_id", "group_members", ["user_id"], unique=False
    )
    op.create_table(
        "source_chunks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("material_id", sa.Integer(), nullable=True),
        sa.Column("news_id", sa.Integer(), nullable=True),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("heading", sa.String(length=500), nullable=True),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("embedding_model", sa.String(length=255), nullable=True),
        sa.Column("generation_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple', text)", persisted=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(material_id IS NOT NULL AND news_id IS NULL) OR (material_id IS NULL AND news_id IS NOT NULL)",
            name=op.f("ck_source_chunks_one_source"),
        ),
        sa.ForeignKeyConstraint(
            ["material_id"],
            ["materials.id"],
            name=op.f("fk_source_chunks_material_id_materials"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["news_id"],
            ["news.id"],
            name=op.f("fk_source_chunks_news_id_news"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_chunks")),
    )
    op.create_index(
        "ix_source_chunks_material_idx",
        "source_chunks",
        ["material_id", "chunk_index"],
        unique=False,
    )
    op.create_index(
        "ix_source_chunks_news_idx",
        "source_chunks",
        ["news_id", "chunk_index"],
        unique=False,
    )
    op.create_index(
        "ix_source_chunks_search_vector",
        "source_chunks",
        ["search_vector"],
        unique=False,
        postgresql_using="gin",
    )
    _add_embedding_column()
    op.create_table(
        "tests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("question_count", sa.Integer(), nullable=False),
        sa.Column("option_count", sa.Integer(), nullable=False),
        sa.Column(
            "difficulty",
            sa.Enum(
                "easy",
                "medium",
                "hard",
                "mixed",
                name="test_difficulty",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("news_percent", sa.Integer(), nullable=False),
        sa.Column("use_all_materials", sa.Boolean(), nullable=False),
        sa.Column("focus_query", sa.String(length=500), nullable=True),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("randomize_questions", sa.Boolean(), nullable=False),
        sa.Column("randomize_options", sa.Boolean(), nullable=False),
        sa.Column(
            "answer_reveal",
            sa.Enum(
                "immediate",
                "after",
                "never",
                name="answer_reveal",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("allow_retakes", sa.Boolean(), nullable=False),
        sa.Column("passing_percent", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "DRAFT",
                "READY",
                "ACTIVE",
                "EXPIRED",
                "CLOSED",
                name="test_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "generation_status",
            sa.Enum(
                "idle",
                "running",
                "done",
                "failed",
                name="generation_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("generation_error", sa.Text(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("announced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reminder_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "answer_reveal IN ('immediate', 'after', 'never')",
            name=op.f("ck_tests_answer_reveal"),
        ),
        sa.CheckConstraint(
            "difficulty IN ('easy', 'medium', 'hard', 'mixed')",
            name=op.f("ck_tests_test_difficulty"),
        ),
        sa.CheckConstraint(
            "generation_status IN ('idle', 'running', 'done', 'failed')",
            name=op.f("ck_tests_generation_status"),
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'READY', 'ACTIVE', 'EXPIRED', 'CLOSED')",
            name=op.f("ck_tests_test_status"),
        ),
        sa.CheckConstraint(
            "deadline_at > starts_at", name=op.f("ck_tests_deadline_after_start")
        ),
        sa.CheckConstraint(
            "news_percent BETWEEN 0 AND 100", name=op.f("ck_tests_news_percent_range")
        ),
        sa.CheckConstraint(
            "option_count BETWEEN 3 AND 5", name=op.f("ck_tests_option_count_range")
        ),
        sa.CheckConstraint(
            "passing_percent BETWEEN 0 AND 100",
            name=op.f("ck_tests_passing_percent_range"),
        ),
        sa.CheckConstraint(
            "question_count BETWEEN 1 AND 100",
            name=op.f("ck_tests_question_count_range"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_tests_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["groups.id"],
            name=op.f("fk_tests_group_id_groups"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tests")),
    )
    op.create_index(
        op.f("ix_tests_deadline_at"), "tests", ["deadline_at"], unique=False
    )
    op.create_index(op.f("ix_tests_status"), "tests", ["status"], unique=False)
    op.create_table(
        "questions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("options", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("option_count", sa.Integer(), nullable=False),
        sa.Column("correct_option", sa.String(length=1), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("topic_id", sa.Integer(), nullable=True),
        sa.Column(
            "difficulty",
            sa.Enum(
                "easy",
                "medium",
                "hard",
                name="difficulty",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "approved",
                "rejected",
                name="question_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "origin",
            sa.Enum(
                "ai",
                "manual",
                name="question_origin",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "source_kind",
            sa.Enum(
                "material",
                "news",
                name="source_kind",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("source_material_id", sa.Integer(), nullable=True),
        sa.Column("source_news_id", sa.Integer(), nullable=True),
        sa.Column("source_chunk_id", sa.Integer(), nullable=True),
        sa.Column("source_page", sa.String(length=32), nullable=True),
        sa.Column("source_reference", sa.Text(), nullable=False),
        sa.Column("source_excerpt", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.String(length=64), nullable=False),
        sa.Column("validation", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("times_used", sa.Integer(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(source_kind = 'material' AND source_material_id IS NOT NULL) OR (source_kind = 'news' AND source_news_id IS NOT NULL) OR (source_material_id IS NULL AND source_news_id IS NULL)",
            name=op.f("ck_questions_source_matches_kind"),
        ),
        sa.CheckConstraint(
            "correct_option IN ('A','B','C','D','E')",
            name=op.f("ck_questions_correct_option_letter"),
        ),
        sa.CheckConstraint(
            "difficulty IN ('easy', 'medium', 'hard')",
            name=op.f("ck_questions_difficulty"),
        ),
        sa.CheckConstraint(
            "origin IN ('ai', 'manual')", name=op.f("ck_questions_question_origin")
        ),
        sa.CheckConstraint(
            "source_kind IN ('material', 'news')", name=op.f("ck_questions_source_kind")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name=op.f("ck_questions_question_status"),
        ),
        sa.CheckConstraint(
            "option_count BETWEEN 3 AND 5", name=op.f("ck_questions_option_count_range")
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_id"],
            ["users.id"],
            name=op.f("fk_questions_reviewed_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_chunk_id"],
            ["source_chunks.id"],
            name=op.f("fk_questions_source_chunk_id_source_chunks"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_material_id"],
            ["materials.id"],
            name=op.f("fk_questions_source_material_id_materials"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_news_id"],
            ["news.id"],
            name=op.f("fk_questions_source_news_id_news"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["topic_id"],
            ["topics.id"],
            name=op.f("fk_questions_topic_id_topics"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_questions")),
    )
    op.create_index(
        op.f("ix_questions_difficulty"), "questions", ["difficulty"], unique=False
    )
    op.create_index(
        op.f("ix_questions_source_chunk_id"),
        "questions",
        ["source_chunk_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_questions_source_material_id"),
        "questions",
        ["source_material_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_questions_source_news_id"),
        "questions",
        ["source_news_id"],
        unique=False,
    )
    op.create_index(
        "ix_questions_status_kind", "questions", ["status", "source_kind"], unique=False
    )
    op.create_index("ix_questions_text_hash", "questions", ["text_hash"], unique=True)
    op.create_index(
        op.f("ix_questions_topic_id"), "questions", ["topic_id"], unique=False
    )
    op.create_table(
        "test_attempts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("test_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "IN_PROGRESS",
                "COMPLETED",
                "EXPIRED",
                "CANCELLED",
                name="attempt_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("total_questions", sa.Integer(), nullable=False),
        sa.Column("answered_count", sa.Integer(), nullable=False),
        sa.Column("correct_count", sa.Integer(), nullable=False),
        sa.Column("incorrect_count", sa.Integer(), nullable=False),
        sa.Column("score_percent", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("current_index", sa.Integer(), nullable=False),
        sa.Column("layout", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("last_message_id", sa.Integer(), nullable=True),
        sa.Column("cancelled_reason", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('IN_PROGRESS', 'COMPLETED', 'EXPIRED', 'CANCELLED')",
            name=op.f("ck_test_attempts_attempt_status"),
        ),
        sa.CheckConstraint(
            "answered_count <= total_questions",
            name=op.f("ck_test_attempts_answered_le_total"),
        ),
        sa.CheckConstraint(
            "correct_count <= answered_count",
            name=op.f("ck_test_attempts_correct_le_answered"),
        ),
        sa.ForeignKeyConstraint(
            ["test_id"],
            ["tests.id"],
            name=op.f("fk_test_attempts_test_id_tests"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_test_attempts_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_test_attempts")),
        sa.UniqueConstraint(
            "test_id",
            "user_id",
            "attempt_no",
            name=op.f("uq_test_attempts_test_id_user_id_attempt_no"),
        ),
    )
    op.create_index(
        "ix_test_attempts_test_status",
        "test_attempts",
        ["test_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_test_attempts_user_status",
        "test_attempts",
        ["user_id", "status"],
        unique=False,
    )
    op.create_index(
        "uq_test_attempts_one_in_progress",
        "test_attempts",
        ["test_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'IN_PROGRESS'"),
    )
    op.create_table(
        "test_materials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("test_id", sa.Integer(), nullable=False),
        sa.Column("material_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["material_id"],
            ["materials.id"],
            name=op.f("fk_test_materials_material_id_materials"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["test_id"],
            ["tests.id"],
            name=op.f("fk_test_materials_test_id_tests"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_test_materials")),
        sa.UniqueConstraint(
            "test_id", "material_id", name=op.f("uq_test_materials_test_id_material_id")
        ),
    )
    op.create_index(
        op.f("ix_test_materials_test_id"), "test_materials", ["test_id"], unique=False
    )
    op.create_table(
        "test_questions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("test_id", sa.Integer(), nullable=False),
        sa.Column("question_id", sa.Integer(), nullable=True),
        sa.Column("question_version", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("options", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("correct_option", sa.String(length=1), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("topic_id", sa.Integer(), nullable=True),
        sa.Column("topic_name", sa.String(length=255), nullable=False),
        sa.Column(
            "difficulty",
            sa.Enum(
                "easy",
                "medium",
                "hard",
                name="tq_difficulty",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "source_kind",
            sa.Enum(
                "material",
                "news",
                name="tq_source_kind",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("source_reference", sa.Text(), nullable=False),
        sa.Column("source_excerpt", sa.Text(), nullable=False),
        sa.Column("is_approved", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "correct_option IN ('A','B','C','D','E')",
            name=op.f("ck_test_questions_correct_option_letter"),
        ),
        sa.CheckConstraint(
            "difficulty IN ('easy', 'medium', 'hard')",
            name=op.f("ck_test_questions_tq_difficulty"),
        ),
        sa.CheckConstraint(
            "source_kind IN ('material', 'news')",
            name=op.f("ck_test_questions_tq_source_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["questions.id"],
            name=op.f("fk_test_questions_question_id_questions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["test_id"],
            ["tests.id"],
            name=op.f("fk_test_questions_test_id_tests"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["topic_id"],
            ["topics.id"],
            name=op.f("fk_test_questions_topic_id_topics"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_test_questions")),
        sa.UniqueConstraint(
            "test_id", "position", name=op.f("uq_test_questions_test_id_position")
        ),
    )
    op.create_index(
        op.f("ix_test_questions_question_id"),
        "test_questions",
        ["question_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_test_questions_topic_id"), "test_questions", ["topic_id"], unique=False
    )
    op.create_index(
        "uq_test_questions_test_question",
        "test_questions",
        ["test_id", "question_id"],
        unique=True,
        postgresql_where=sa.text("question_id IS NOT NULL"),
    )
    op.create_table(
        "user_answers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("attempt_id", sa.Integer(), nullable=False),
        sa.Column("test_question_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("selected_option", sa.String(length=1), nullable=False),
        sa.Column("selected_display", sa.String(length=1), nullable=False),
        sa.Column("correct_option", sa.String(length=1), nullable=False),
        sa.Column("is_correct", sa.Boolean(), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "selected_option IN ('A','B','C','D','E')",
            name=op.f("ck_user_answers_selected_option_letter"),
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["test_attempts.id"],
            name=op.f("fk_user_answers_attempt_id_test_attempts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["test_question_id"],
            ["test_questions.id"],
            name=op.f("fk_user_answers_test_question_id_test_questions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_answers_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_answers")),
        sa.UniqueConstraint(
            "attempt_id",
            "test_question_id",
            name=op.f("uq_user_answers_attempt_id_test_question_id"),
        ),
    )
    op.create_index(
        op.f("ix_user_answers_test_question_id"),
        "user_answers",
        ["test_question_id"],
        unique=False,
    )
    op.create_index(
        "ix_user_answers_user_correct",
        "user_answers",
        ["user_id", "is_correct"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_user_answers_user_correct", table_name="user_answers")
    op.drop_index(op.f("ix_user_answers_test_question_id"), table_name="user_answers")
    op.drop_table("user_answers")
    op.drop_index(
        "uq_test_questions_test_question",
        table_name="test_questions",
        postgresql_where=sa.text("question_id IS NOT NULL"),
    )
    op.drop_index(op.f("ix_test_questions_topic_id"), table_name="test_questions")
    op.drop_index(op.f("ix_test_questions_question_id"), table_name="test_questions")
    op.drop_table("test_questions")
    op.drop_index(op.f("ix_test_materials_test_id"), table_name="test_materials")
    op.drop_table("test_materials")
    op.drop_index(
        "uq_test_attempts_one_in_progress",
        table_name="test_attempts",
        postgresql_where=sa.text("status = 'IN_PROGRESS'"),
    )
    op.drop_index("ix_test_attempts_user_status", table_name="test_attempts")
    op.drop_index("ix_test_attempts_test_status", table_name="test_attempts")
    op.drop_table("test_attempts")
    op.drop_index(op.f("ix_questions_topic_id"), table_name="questions")
    op.drop_index("ix_questions_text_hash", table_name="questions")
    op.drop_index("ix_questions_status_kind", table_name="questions")
    op.drop_index(op.f("ix_questions_source_news_id"), table_name="questions")
    op.drop_index(op.f("ix_questions_source_material_id"), table_name="questions")
    op.drop_index(op.f("ix_questions_source_chunk_id"), table_name="questions")
    op.drop_index(op.f("ix_questions_difficulty"), table_name="questions")
    op.drop_table("questions")
    op.drop_index(op.f("ix_tests_status"), table_name="tests")
    op.drop_index(op.f("ix_tests_deadline_at"), table_name="tests")
    op.drop_table("tests")
    op.drop_index(
        "ix_source_chunks_search_vector",
        table_name="source_chunks",
        postgresql_using="gin",
    )
    op.drop_index("ix_source_chunks_news_idx", table_name="source_chunks")
    op.drop_index("ix_source_chunks_material_idx", table_name="source_chunks")
    op.drop_table("source_chunks")
    op.drop_index("ix_group_members_user_id", table_name="group_members")
    op.drop_table("group_members")
    op.drop_index(op.f("ix_news_news_date"), table_name="news")
    op.drop_index(op.f("ix_news_is_active"), table_name="news")
    op.drop_table("news")
    op.drop_index(op.f("ix_materials_status"), table_name="materials")
    op.drop_index(op.f("ix_materials_content_hash"), table_name="materials")
    op.drop_index(op.f("ix_materials_category"), table_name="materials")
    op.drop_table("materials")
    op.drop_table("groups")
    op.drop_index(op.f("ix_users_username"), table_name="users")
    op.drop_index(op.f("ix_users_status"), table_name="users")
    op.drop_table("users")
    op.drop_table("topics")
    op.drop_table("bot_settings")
