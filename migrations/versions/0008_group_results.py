"""Durable delivery markers for personal expiry results and group rankings.

Existing questions, answers and scores are unchanged. Historical expired attempts
are marked handled so upgrading does not send old notifications to employees.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("test_attempts", sa.Column("expiry_notified_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE test_attempts SET expiry_notified_at = now() WHERE status = 'EXPIRED'")
    op.add_column("group_test_posts", sa.Column("result_message_ids", postgresql.JSONB(), nullable=False, server_default="[]"))


def downgrade():
    op.drop_column("group_test_posts", "result_message_ids")
    op.drop_column("test_attempts", "expiry_notified_at")
