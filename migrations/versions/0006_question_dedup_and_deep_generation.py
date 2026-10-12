"""question dedup fields, chunk coverage flag, material generation jobs

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-12 10:00:00

Only adds columns, an index and a table: existing questions and data are kept as they are.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("questions", sa.Column("article_ref", sa.String(length=120), nullable=True))
    op.add_column("questions", sa.Column("fact", sa.Text(), nullable=True))
    op.add_column("questions", sa.Column("fact_hash", sa.String(length=64), nullable=True))
    op.add_column("questions", sa.Column("answer_hash", sa.String(length=64), nullable=True))
    op.add_column("questions", sa.Column("dedup_vec", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_index(op.f("ix_questions_answer_hash"), "questions", ["answer_hash"], unique=False)
    op.create_index(
        "uq_questions_fact_hash",
        "questions",
        ["fact_hash"],
        unique=True,
        postgresql_where=sa.text("fact_hash IS NOT NULL AND status <> 'rejected'"),
    )
    op.add_column(
        "source_chunks", sa.Column("gen_exhausted", sa.Boolean(), server_default="false", nullable=False)
    )
    op.create_table(
        "material_generation_jobs",
        sa.Column("material_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("target", sa.Integer(), nullable=False),
        sa.Column("created", sa.Integer(), nullable=False),
        sa.Column("duplicates", sa.Integer(), nullable=False),
        sa.Column("rejected", sa.Integer(), nullable=False),
        sa.Column("pass_no", sa.Integer(), nullable=False),
        sa.Column("cursor", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notify_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["material_id"], ["materials.id"], name=op.f("fk_material_generation_jobs_material_id_materials"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("material_id", name=op.f("pk_material_generation_jobs")),
    )  # fmt: skip


def downgrade() -> None:
    op.drop_table("material_generation_jobs")
    op.drop_column("source_chunks", "gen_exhausted")
    op.drop_index("uq_questions_fact_hash", table_name="questions")
    op.drop_index(op.f("ix_questions_answer_hash"), table_name="questions")
    for column in ("dedup_vec", "answer_hash", "fact_hash", "fact", "article_ref"):
        op.drop_column("questions", column)
