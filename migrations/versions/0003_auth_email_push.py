"""注册/登录(users.password_hash + auth_tokens)与邮箱推送(去掉 webhook 字段)

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-23
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("password_hash", sa.Text(), nullable=True))
    op.drop_column("users", "push_channel")
    op.drop_column("users", "webhook_url")
    op.drop_column("users", "webhook_type")

    op.create_table(
        "auth_tokens",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_auth_tokens_user_id", "auth_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_table("auth_tokens")
    op.add_column("users", sa.Column("push_channel", sa.Text(), nullable=False, server_default="console"))
    op.add_column("users", sa.Column("webhook_url", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("webhook_type", sa.Text(), nullable=False, server_default="wecom"))
    op.drop_column("users", "password_hash")
