"""per-user test duration, test sources, assignments, blocked users

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-08 10:20:15.213630
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(*values: str, name: str) -> sa.Enum:
    # The CHECK constraint is added explicitly below (with the naming convention).
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=False, length=32)


def upgrade() -> None:
    # --- users: a separate BLOCKED status (blocked users stay visible in the admin lists)
    op.drop_constraint(op.f("ck_users_user_status"), "users", type_="check")
    op.create_check_constraint(
        op.f("ck_users_user_status"), "users", "status IN ('pending', 'active', 'inactive', 'blocked')"
    )

    # --- tests: personal duration, audience, pause flag
    op.add_column("tests", sa.Column("duration_seconds", sa.Integer(), server_default="86400", nullable=False))
    op.add_column(
        "tests", sa.Column("audience", _enum("all", "group", "users", name="test_audience"), server_default="all",
                           nullable=False)
    )  # fmt: skip
    op.create_check_constraint(op.f("ck_tests_test_audience"), "tests", "audience IN ('all', 'group', 'users')")
    op.add_column("tests", sa.Column("paused", sa.Boolean(), server_default="false", nullable=False))
    # Existing tests keep their behaviour: personal time = the whole availability window.
    op.execute(
        "UPDATE tests SET duration_seconds = GREATEST(60, EXTRACT(EPOCH FROM (deadline_at - starts_at))::int)"
    )
    op.execute("UPDATE tests SET audience = 'group' WHERE group_id IS NOT NULL")

    # --- test sources
    op.create_table(
        "test_sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("test_id", sa.Integer(), nullable=False),
        sa.Column("material_id", sa.Integer(), nullable=True),
        sa.Column("source_name", sa.String(length=255), nullable=False),
        sa.Column("original_file_name", sa.String(length=255), nullable=True),
        sa.Column("file_id", sa.String(length=255), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["material_id"], ["materials.id"], name=op.f("fk_test_sources_material_id_materials"),
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["test_id"], ["tests.id"], name=op.f("fk_test_sources_test_id_tests"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_test_sources")),
        sa.UniqueConstraint("test_id", "material_id", name=op.f("uq_test_sources_test_id_material_id")),
    )  # fmt: skip
    op.create_index(op.f("ix_test_sources_test_id"), "test_sources", ["test_id"], unique=False)

    op.add_column("test_questions", sa.Column("source_id", sa.Integer(), nullable=True))
    op.add_column("test_questions", sa.Column("source_name", sa.String(length=255), server_default="", nullable=False))
    op.add_column("test_questions", sa.Column("source_file", sa.String(length=255), nullable=True))
    op.create_index(op.f("ix_test_questions_source_id"), "test_questions", ["source_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_test_questions_source_id_test_sources"), "test_questions", "test_sources", ["source_id"], ["id"],
        ondelete="SET NULL",
    )  # fmt: skip

    # Backfill: sources of existing tests = materials their questions came from.
    op.execute(
        """
        INSERT INTO test_sources (test_id, material_id, source_name, original_file_name, file_id, position)
        SELECT s.test_id, m.id, m.title, m.file_name, m.telegram_file_id,
               row_number() OVER (PARTITION BY s.test_id ORDER BY m.id) - 1
        FROM (SELECT DISTINCT tq.test_id, q.source_material_id AS mid
              FROM test_questions tq JOIN questions q ON q.id = tq.question_id
              WHERE q.source_material_id IS NOT NULL) s
        JOIN materials m ON m.id = s.mid
        """
    )
    op.execute(
        """
        UPDATE test_questions tq
        SET source_id = ts.id, source_name = ts.source_name, source_file = ts.original_file_name
        FROM questions q JOIN test_sources ts ON ts.material_id = q.source_material_id
        WHERE q.id = tq.question_id AND ts.test_id = tq.test_id
        """
    )
    op.execute(
        """
        UPDATE test_questions tq SET source_name = n.title
        FROM questions q JOIN news n ON n.id = q.source_news_id
        WHERE q.id = tq.question_id AND tq.source_name = ''
        """
    )

    # --- per-employee assignments
    op.create_table(
        "test_assignments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("test_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("status", _enum("assigned", "removed", name="assignment_status"), nullable=False),
        sa.Column("retake_allowed", sa.Boolean(), nullable=False),
        sa.Column("assigned_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('assigned', 'removed')", name=op.f("ck_test_assignments_assignment_status")),
        sa.ForeignKeyConstraint(["assigned_by_id"], ["users.id"], name=op.f("fk_test_assignments_assigned_by_id_users"),
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["test_id"], ["tests.id"], name=op.f("fk_test_assignments_test_id_tests"),
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_test_assignments_user_id_users"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_test_assignments")),
        sa.UniqueConstraint("test_id", "user_id", name=op.f("uq_test_assignments_test_id_user_id")),
    )  # fmt: skip
    op.create_index(op.f("ix_test_assignments_test_id"), "test_assignments", ["test_id"], unique=False)
    op.create_index(op.f("ix_test_assignments_user_id"), "test_assignments", ["user_id"], unique=False)

    # --- answers: final within an attempt (UNIQUE attempt_id + test_question_id stays). A second
    # attempt — and so a second answer to the same question — exists only after an admin allows a retake.
    op.drop_constraint(op.f("uq_user_answers_user_test_question"), "user_answers", type_="unique")


def downgrade() -> None:
    op.create_unique_constraint(
        op.f("uq_user_answers_user_test_question"), "user_answers", ["user_id", "test_id", "test_question_id"]
    )
    op.drop_index(op.f("ix_test_assignments_user_id"), table_name="test_assignments")
    op.drop_index(op.f("ix_test_assignments_test_id"), table_name="test_assignments")
    op.drop_table("test_assignments")
    op.drop_constraint(op.f("fk_test_questions_source_id_test_sources"), "test_questions", type_="foreignkey")
    op.drop_index(op.f("ix_test_questions_source_id"), table_name="test_questions")
    op.drop_column("test_questions", "source_file")
    op.drop_column("test_questions", "source_name")
    op.drop_column("test_questions", "source_id")
    op.drop_index(op.f("ix_test_sources_test_id"), table_name="test_sources")
    op.drop_table("test_sources")
    op.drop_column("tests", "paused")
    op.drop_constraint(op.f("ck_tests_test_audience"), "tests", type_="check")
    op.drop_column("tests", "audience")
    op.drop_column("tests", "duration_seconds")
    op.execute("UPDATE users SET status = 'inactive' WHERE status = 'blocked'")
    op.drop_constraint(op.f("ck_users_user_status"), "users", type_="check")
    op.create_check_constraint(op.f("ck_users_user_status"), "users", "status IN ('pending', 'active', 'inactive')")
