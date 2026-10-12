"""tests may hold up to 300 questions

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-12 23:30:00

Only widens a check constraint (1–100 → 1–300); no data changes.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_tests_question_count_range"), "tests", type_="check")
    op.create_check_constraint(op.f("ck_tests_question_count_range"), "tests", "question_count BETWEEN 1 AND 300")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_tests_question_count_range"), "tests", type_="check")
    # NOT VALID: tests already made with more than 100 questions are kept as they are.
    op.execute(
        "ALTER TABLE tests ADD CONSTRAINT ck_tests_question_count_range "
        "CHECK (question_count BETWEEN 1 AND 100) NOT VALID"
    )
