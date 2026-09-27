"""用户信息(登录用户仅可访问自己)。注册/登录见 api/auth.py。"""
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import User
from .auth import get_current_user

router = APIRouter(tags=["users"])


class UserUpdate(BaseModel):
    name: str = ""


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    created_at: datetime

    model_config = {"from_attributes": True}


async def _own_user(user_id: uuid.UUID, current: User) -> User:
    """路径中的 user_id 必须与登录用户一致(骨架期即租户自服务)。"""
    if user_id != current.id:
        raise HTTPException(403, "只能访问自己的数据")
    return current


@router.get("/users/{user_id}", response_model=UserOut)
async def get_user(user_id: uuid.UUID, current: User = Depends(get_current_user)):
    return await _own_user(user_id, current)


@router.put("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    user = await _own_user(user_id, current)
    user.name = payload.name
    await session.commit()
    await session.refresh(user)
    return user
