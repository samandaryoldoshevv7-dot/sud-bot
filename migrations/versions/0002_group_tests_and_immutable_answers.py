"""group tests and immutable answers

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-07 20:05:15.031441
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "group_test_posts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("test_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("header_message_id", sa.Integer(), nullable=True),
        sa.Column("posted_all", sa.Boolean(), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["group_id"], ["groups.id"], name=op.f("fk_group_test_posts_group_id_groups"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["test_id"], ["tests.id"], name=op.f("fk_group_test_posts_test_id_tests"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_group_test_posts")),
        sa.UniqueConstraint("test_id", "group_id", name=op.f("uq_group_test_posts_test_id_group_id")),
    )
    op.create_table(
        "group_question_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("test_question_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("opts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("rendered_count", sa.Integer(), nullable=False),
        sa.Column("rendered_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["post_id"],
            ["group_test_posts.id"],
            name=op.f("fk_group_question_messages_post_id_group_test_posts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["test_question_id"],
            ["test_questions.id"],
            name=op.f("fk_group_question_messages_test_question_id_test_questions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_group_question_messages")),
        sa.UniqueConstraint("post_id", "position", name=op.f("uq_group_question_messages_post_id_position")),
        sa.UniqueConstraint(
            "post_id", "test_question_id", name=op.f("uq_group_question_messages_post_id_test_question_id")
        ),
    )
    op.create_table(
        "question_options",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("test_question_id", sa.Integer(), nullable=False),
        sa.Column("letter", sa.String(length=1), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("is_correct", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["test_question_id"],
            ["test_questions.id"],
            name=op.f("fk_question_options_test_question_id_test_questions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_question_options")),
        sa.UniqueConstraint("test_question_id", "letter", name=op.f("uq_question_options_test_question_id_letter")),
    )
    op.add_column(
        "tests",
        sa.Column(
            "delivery_mode",
            sa.Enum("group", "private", name="delivery_mode", native_enum=False, create_constraint=True, length=32),
            server_default="private",
            nullable=False,
        ),
    )
    # New answer columns are added nullable, back-filled from existing rows, then tightened.
    op.add_column("user_answers", sa.Column("test_id", sa.Integer(), nullable=True))
    op.add_column("user_answers", sa.Column("telegram_id", sa.BigInteger(), nullable=True))
    op.add_column("user_answers", sa.Column("selected_option_id", sa.Integer(), nullable=True))
    op.add_column("user_answers", sa.Column("correct_display", sa.String(length=1), nullable=True))
    op.execute("UPDATE user_answers ua SET test_id = ta.test_id FROM test_attempts ta WHERE ta.id = ua.attempt_id")
    op.execute("UPDATE user_answers ua SET telegram_id = u.telegram_id FROM users u WHERE u.id = ua.user_id")
    # Normalised snapshot options for every existing test question.
    op.execute(
        "INSERT INTO question_options (test_question_id, letter, text, is_correct) "
        "SELECT tq.id, kv.key, kv.value, kv.key = tq.correct_option "
        "FROM test_questions tq, jsonb_each_text(tq.options) AS kv"
    )
    op.execute(
        "UPDATE user_answers ua SET selected_option_id = qo.id FROM question_options qo "
        "WHERE qo.test_question_id = ua.test_question_id AND qo.letter = ua.selected_option"
    )
    # One answer per (user, test, question): keep only the first answer if retakes created more.
    op.execute(
        "DELETE FROM user_answers a USING user_answers b WHERE a.user_id = b.user_id "
        "AND a.test_id = b.test_id AND a.test_question_id = b.test_question_id AND a.id > b.id"
    )
    op.alter_column("user_answers", "test_id", nullable=False)
    op.alter_column("user_answers", "telegram_id", nullable=False)
    op.create_index("ix_user_answers_test_question", "user_answers", ["test_id", "test_question_id"], unique=False)
    op.create_unique_constraint(
        "uq_user_answers_user_test_question", "user_answers", ["user_id", "test_id", "test_question_id"]
    )
    op.create_foreign_key(
        op.f("fk_user_answers_test_id_tests"), "user_answers", "tests", ["test_id"], ["id"], ondelete="CASCADE"
    )
    op.create_foreign_key(
        op.f("fk_user_answers_selected_option_id_question_options"),
        "user_answers",
        "question_options",
        ["selected_option_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Answers are immutable: the database itself rejects any UPDATE.
    op.execute(
        "CREATE OR REPLACE FUNCTION forbid_user_answer_update() RETURNS trigger AS $$ "
        "BEGIN RAISE EXCEPTION 'user_answers rows are immutable (answer % cannot be changed)', OLD.id; END; "
        "$$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER trg_user_answers_immutable BEFORE UPDATE ON user_answers "
        "FOR EACH ROW EXECUTE FUNCTION forbid_user_answer_update()"
    )
    # Convenience view: chunks that belong to uploaded materials (news chunks live in the same table).
    op.execute(
        "CREATE OR REPLACE VIEW material_chunks AS SELECT id, material_id, chunk_index, text, page_start, "
        "page_end, heading, char_count, created_at FROM source_chunks WHERE material_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS material_chunks")
    op.execute("DROP TRIGGER IF EXISTS trg_user_answers_immutable ON user_answers")
    op.execute("DROP FUNCTION IF EXISTS forbid_user_answer_update()")
    op.drop_constraint(op.f("fk_user_answers_selected_option_id_question_options"), "user_answers", type_="foreignkey")
    op.drop_constraint(op.f("fk_user_answers_test_id_tests"), "user_answers", type_="foreignkey")
    op.drop_constraint("uq_user_answers_user_test_question", "user_answers", type_="unique")
    op.drop_index("ix_user_answers_test_question", table_name="user_answers")
    op.drop_column("user_answers", "correct_display")
    op.drop_column("user_answers", "selected_option_id")
    op.drop_column("user_answers", "telegram_id")
    op.drop_column("user_answers", "test_id")
    op.drop_column("tests", "delivery_mode")
    op.drop_table("question_options")
    op.drop_table("group_question_messages")
    op.drop_table("group_test_posts")
