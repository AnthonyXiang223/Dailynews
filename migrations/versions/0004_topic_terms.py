"""主题词表 topic_terms(LLM 可补充检索词,持久化跨运行生效)

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-24
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "topic_terms",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("term", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False, server_default="llm"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("topic", "term", name="uq_topic_terms_topic_term"),
    )


def downgrade() -> None:
    op.drop_table("topic_terms")
