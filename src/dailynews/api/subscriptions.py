"""订阅偏好(L1 显式画像):主题 + 关键词;变更后重建定时任务。

其余偏好(推送时间/条数上限等)骨架期用后端默认值,前端不暴露。
"""
import uuid
from datetime import datetime, time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_serializer, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Subscription, User
from ..scheduler import reschedule_user
from .auth import get_current_user

router = APIRouter(tags=["subscriptions"])


class SubscriptionIn(BaseModel):
    topics: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    digest_time: str = "08:00"  # HH:MM,用户本地时间
    max_items: int = Field(default=10, ge=1, le=30)

    @field_validator("digest_time")
    @classmethod
    def _check_digest_time(cls, v: str) -> str:
        try:
            time.fromisoformat(v)
        except ValueError:
            raise ValueError("digest_time 格式应为 HH:MM")
        return v


class SubscriptionOut(BaseModel):
    user_id: uuid.UUID
    topics: list[str]
    keywords: list[str]
    digest_time: time  # 校验接受 ORM 的 time 对象,序列化时格式化为 'HH:MM'
    max_items: int
    updated_at: datetime

    model_config = {"from_attributes": True}

    @field_serializer("digest_time")
    def _fmt_digest_time(self, v) -> str:
        return v.strftime("%H:%M") if hasattr(v, "strftime") else str(v)


async def _own_sub(session: AsyncSession, user_id: uuid.UUID, current: User) -> Subscription | None:
    if user_id != current.id:
        raise HTTPException(403, "只能访问自己的数据")
    return (
        await session.execute(select(Subscription).where(Subscription.user_id == user_id))
    ).scalar_one_or_none()


@router.get("/users/{user_id}/subscription", response_model=SubscriptionOut)
async def get_subscription(
    user_id: uuid.UUID,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    sub = await _own_sub(session, user_id, current)
    if sub is None:
        raise HTTPException(404, "该用户还没有订阅偏好,请先 PUT 设置")
    return sub


@router.put("/users/{user_id}/subscription", response_model=SubscriptionOut)
async def put_subscription(
    user_id: uuid.UUID,
    payload: SubscriptionIn,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    sub = await _own_sub(session, user_id, current)
    if sub is None:
        sub = Subscription(user_id=user_id)
        session.add(sub)
    sub.topics = payload.topics
    sub.keywords = payload.keywords
    sub.digest_time = time.fromisoformat(payload.digest_time)
    sub.max_items = payload.max_items
    await session.commit()
    await session.refresh(sub)
    await reschedule_user(user_id)  # 推送时间可能变化,重建定时任务(幂等)
    return sub
