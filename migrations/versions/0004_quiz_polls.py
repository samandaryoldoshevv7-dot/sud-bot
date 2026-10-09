"""native Telegram quiz polls

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-09 12:40:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "quiz_polls",
        sa.Column("poll_id", sa.String(length=64), nullable=False),
        sa.Column("attempt_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("explanation_cut", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["test_attempts.id"], name=op.f("fk_quiz_polls_attempt_id_test_attempts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("poll_id", name=op.f("pk_quiz_polls")),
    )  # fmt: skip
    op.create_index(op.f("ix_quiz_polls_attempt_id"), "quiz_polls", ["attempt_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_quiz_polls_attempt_id"), table_name="quiz_polls")
    op.drop_table("quiz_polls")
