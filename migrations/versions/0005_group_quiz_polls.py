"""group questions as native Telegram quiz polls

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-09 15:10:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("group_question_messages", sa.Column("poll_id", sa.String(length=64), nullable=True))
    op.create_unique_constraint(op.f("uq_group_question_messages_poll_id"), "group_question_messages", ["poll_id"])


def downgrade() -> None:
    op.drop_constraint(op.f("uq_group_question_messages_poll_id"), "group_question_messages", type_="unique")
    op.drop_column("group_question_messages", "poll_id")
