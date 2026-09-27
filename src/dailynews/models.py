"""ORM 模型:users / subscriptions / articles / digests / deliveries / behavior_events。

画像三层:
- L1 显式订阅 = subscriptions 表
- L2 隐式行为 = behavior_events 表(简报内反馈链接埋点)
- L3 派生画像 = users.profile JSONB(deriver 定时更新)
"""
import uuid
from datetime import date, datetime, time

from pgvector.sqlalchemy import Vector
from sqlalchemy import (BigInteger, Date, DateTime, ForeignKey, Integer, String, Text, Time, UniqueConstraint,
                        text)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .db import Base


class User(Base):
    """用户即租户(To C)。邮箱为登录账号,id 为不可猜测的租户键。"""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text, default="", server_default="")
    email: Mapped[str] = mapped_column(Text, unique=True)
    password_hash: Mapped[str | None] = mapped_column(Text)  # scrypt 格式,注册时必填
    timezone: Mapped[str] = mapped_column(Text, default="Asia/Shanghai", server_default="Asia/Shanghai")
    profile: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))  # L3 派生画像
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AuthToken(Base):
    """登录会话:不透明 bearer token,库里只存 sha256 哈希。"""

    __tablename__ = "auth_tokens"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Subscription(Base):
    """L1 显式订阅偏好,与用户 1:1。"""

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    topics: Mapped[list] = mapped_column(ARRAY(Text), default=list, server_default=text("'{}'::text[]"))
    keywords: Mapped[list] = mapped_column(ARRAY(Text), default=list, server_default=text("'{}'::text[]"))
    exclude_keywords: Mapped[list] = mapped_column(ARRAY(Text), default=list, server_default=text("'{}'::text[]"))
    sources: Mapped[list] = mapped_column(ARRAY(Text), default=list, server_default=text("'{}'::text[]"))
    digest_time: Mapped[time] = mapped_column(Time, default=time(8, 0), server_default=text("'08:00'::time"))
    tone: Mapped[str] = mapped_column(Text, default="concise", server_default="concise")  # concise|detailed|casual|formal
    max_items: Mapped[int] = mapped_column(Integer, default=10, server_default="10")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TopicTerm(Base):
    """主题词表:主题 → 检索词。种子词在代码里,LLM 补充的词存这里。"""

    __tablename__ = "topic_terms"
    __table_args__ = (UniqueConstraint("topic", "term", name="uq_topic_terms_topic_term"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    topic: Mapped[str] = mapped_column(Text)
    term: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text, default="llm", server_default="llm")  # llm | seed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Article(Base):
    """采集文章池,全租户共享;url_hash 去重。"""

    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    url_hash: Mapped[str] = mapped_column(String(32), unique=True)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    embedding: Mapped[list | None] = mapped_column(Vector(1536))  # 仅当配置了 embedding 模型时填充
    meta: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Digest(Base):
    """每日简报,每用户每天一条(重跑覆盖)。"""

    __tablename__ = "digests"
    __table_args__ = (UniqueConstraint("user_id", "date", name="uq_digests_user_date"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    date: Mapped[date] = mapped_column(Date)
    title: Mapped[str | None] = mapped_column(Text)
    content_md: Mapped[str] = mapped_column(Text, default="", server_default="")
    items: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    status: Mapped[str] = mapped_column(Text, default="composed", server_default="composed")  # composed|delivered|failed
    model_used: Mapped[str | None] = mapped_column(Text)  # NULL = 确定性模式
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Delivery(Base):
    """推送投递记录。"""

    __tablename__ = "deliveries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    digest_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("digests.id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="pending", server_default="pending")  # pending|sent|failed
    error: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BehaviorEvent(Base):
    """L2 隐式行为:简报内反馈链接埋点。"""

    __tablename__ = "behavior_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    digest_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("digests.id", ondelete="CASCADE")
    )
    article_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("articles.id", ondelete="SET NULL")  # 文章可删,行为记录保留
    )
    event_type: Mapped[str] = mapped_column(Text)  # click|read|upvote|downvote|reduce_topic
    topic: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
