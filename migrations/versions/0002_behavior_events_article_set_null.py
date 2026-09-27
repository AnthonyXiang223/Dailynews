"""behavior_events.article_id 外键改为 ON DELETE SET NULL(文章可删除,行为记录保留)

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-22
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "behavior_events_article_id_fkey", "behavior_events", type_="foreignkey"
    )
    op.create_foreign_key(
        "behavior_events_article_id_fkey",
        "behavior_events",
        "articles",
        ["article_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "behavior_events_article_id_fkey", "behavior_events", type_="foreignkey"
    )
    op.create_foreign_key(
        "behavior_events_article_id_fkey",
        "behavior_events",
        "articles",
        ["article_id"],
        ["id"],
        ondelete="NO ACTION",
    )
