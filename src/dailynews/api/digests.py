"""历史简报与点赞/点踩(登录用户仅可访问自己)。

投票事件直接进入 behavior_events(L2 行为),并立即触发 deriver
更新用户画像(L3);取消投票以 cancel 事件抵消权重,画像同步回落。
"""
import uuid
from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from ..db import get_session
from ..models import Article, BehaviorEvent, Digest, Subscription, User
from ..pipeline.filter import match_and_score
from ..pipeline.topics import load_topic_terms
from .admin import _spawn
from .auth import get_current_user

router = APIRouter(tags=["digests"])

VOTE_EVENT_TYPES = {"upvote", "downvote", "upvote_cancel", "downvote_cancel"}
# 状态迁移表:(当前状态, 目标状态) → 要记录的事件
VOTE_TRANSITIONS = {
    ("none", "up"): "upvote",
    ("none", "down"): "downvote",
    ("up", "none"): "upvote_cancel",    # 取消 → 负权重抵消
    ("down", "none"): "downvote_cancel",
    ("up", "down"): "downvote",         # 切换:downvote 的 -1 与 upvote 的 +1 相抵
    ("down", "up"): "upvote",
}


class DigestSummary(BaseModel):
    id: uuid.UUID
    date: date
    title: str | None
    status: str
    model_used: str | None
    created_at: datetime
    updated_at: datetime  # 前端轮询用:同一天重生成时 created_at 不变,updated_at 会变

    model_config = {"from_attributes": True}


class DigestDetail(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    date: date
    title: str | None
    content_md: str
    items: list
    status: str
    model_used: str | None
    created_at: datetime
    votes: dict[int, str]  # article_id → "up" | "down"

    model_config = {"from_attributes": True}


class VoteIn(BaseModel):
    article_id: int
    vote: Literal["up", "down", "none"]


async def _own_digests(user_id: uuid.UUID, current: User) -> None:
    if user_id != current.id:
        raise HTTPException(403, "只能访问自己的数据")


async def _current_votes(session: AsyncSession, digest_id: uuid.UUID) -> dict[int, str]:
    """按事件时间线折叠出每篇文章的当前投票状态(最后一条投票事件生效)。"""
    rows = (
        await session.execute(
            select(BehaviorEvent)
            .where(
                BehaviorEvent.digest_id == digest_id,
                BehaviorEvent.event_type.in_(VOTE_EVENT_TYPES),
            )
            .order_by(BehaviorEvent.id)
        )
    ).scalars().all()
    state: dict[int, str] = {}
    for ev in rows:
        if ev.article_id is None:
            continue
        if ev.event_type == "upvote":
            state[ev.article_id] = "up"
        elif ev.event_type == "downvote":
            state[ev.article_id] = "down"
        else:  # cancel 事件 → 回到未投票
            state.pop(ev.article_id, None)
    return state


async def _match_topic(session: AsyncSession, user: User, article: Article) -> str | None:
    """用主题词表给文章打主主题(供画像按主题聚合)。"""
    sub = (
        await session.execute(select(Subscription).where(Subscription.user_id == user.id))
    ).scalar_one_or_none()
    if sub is None:
        return None
    topic_terms = await load_topic_terms(session, sub.topics or [])
    art = {
        "id": article.id,
        "url": article.url,
        "title": article.title,
        "summary": article.summary,
        "content": article.content,
        "source": article.source,
        "published_at": article.published_at,
    }
    scored = match_and_score(art, sub, None, None, topic_terms)
    return (scored.get("matched_topics") or [None])[0] if scored else None


@router.get("/users/{user_id}/digests", response_model=list[DigestSummary])
async def list_digests(
    user_id: uuid.UUID,
    limit: int = Query(default=30, ge=1, le=200),
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await _own_digests(user_id, current)
    # 列表只取摘要字段:content_md/items 体积大,详情接口按需加载即可
    rows = (
        await session.execute(
            select(Digest)
            .options(load_only(
                Digest.id, Digest.date, Digest.title, Digest.status,
                Digest.model_used, Digest.created_at, Digest.updated_at,
            ))
            .where(Digest.user_id == user_id)
            .order_by(Digest.date.desc())
            .limit(limit)
        )
    ).scalars().all()
    return rows


@router.get("/digests/{digest_id}", response_model=DigestDetail)
async def get_digest(
    digest_id: uuid.UUID,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    digest = await session.get(Digest, digest_id)
    if digest is None or digest.user_id != current.id:
        raise HTTPException(404, "简报不存在")
    votes = await _current_votes(session, digest_id)
    return DigestDetail(
        id=digest.id,
        user_id=digest.user_id,
        date=digest.date,
        title=digest.title,
        content_md=digest.content_md,
        items=digest.items or [],
        status=digest.status,
        model_used=digest.model_used,
        created_at=digest.created_at,
        votes=votes,
    )


@router.post("/digests/{digest_id}/vote")
async def vote_digest(
    digest_id: uuid.UUID,
    payload: VoteIn,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """点赞/点踩/取消:状态真正变化时记录事件并立即更新画像。"""
    digest = await session.get(Digest, digest_id)
    if digest is None or digest.user_id != current.id:
        raise HTTPException(404, "简报不存在")
    if not any(it.get("article_id") == payload.article_id for it in (digest.items or [])):
        raise HTTPException(400, "文章不属于该简报")

    current_state = (await _current_votes(session, digest_id)).get(payload.article_id, "none")
    if current_state == payload.vote:
        return {"ok": True, "vote": current_state, "changed": False}

    event_type = VOTE_TRANSITIONS.get((current_state, payload.vote))
    if event_type is None:
        raise HTTPException(400, "非法投票状态迁移")

    article = await session.get(Article, payload.article_id)
    topic = await _match_topic(session, current, article) if article else None
    session.add(
        BehaviorEvent(
            user_id=current.id,
            digest_id=digest.id,
            article_id=payload.article_id,
            event_type=event_type,
            topic=topic,
        )
    )
    await session.commit()
    # 立即更新画像(含取消:负权重抵消后画像同步回落)
    from ..profile.deriver import derive_user

    _spawn(derive_user(current.id))
    return {"ok": True, "vote": payload.vote, "changed": True}


@router.get("/digests/{digest_id}.md", response_class=PlainTextResponse)
async def get_digest_markdown(
    digest_id: uuid.UUID,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    digest = await session.get(Digest, digest_id)
    if digest is None or digest.user_id != current.id:
        raise HTTPException(404, "简报不存在")
    return digest.content_md


@router.post("/digests/{digest_id}/deliver")
async def deliver_digest_endpoint(
    digest_id: uuid.UUID,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """把某期简报发送到用户邮箱(手动触发;生成流程本身不投递)。"""
    digest = await session.get(Digest, digest_id)
    if digest is None or digest.user_id != current.id:
        raise HTTPException(404, "简报不存在")
    from ..agent.tools import deliver_digest

    result = await deliver_digest(digest_id)
    if result.get("status") == "sent":
        return {"ok": True, "message": f"已发送到邮箱({result['channel']})"}
    return {"ok": False, "detail": result.get("error") or "发送失败"}
