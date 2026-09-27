"""注册 / 登录 / 登出 / 当前用户 + Bearer 鉴权依赖。

密码用 stdlib hashlib.scrypt 加盐哈希(格式 scrypt$salt$digest),零额外依赖;
登录令牌为不透明随机串,库里只存 sha256 哈希,30 天过期。
"""
import hashlib
import hmac
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import AuthToken, User

router = APIRouter(tags=["auth"])

TOKEN_TTL_DAYS = 30


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)
    name: str = ""


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    created_at: datetime

    model_config = {"from_attributes": True}


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=16384, r=8, p=1)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _issue_token(session: AsyncSession, user: User) -> str:
    token = secrets.token_urlsafe(32)
    session.add(
        AuthToken(
            token_hash=_hash_token(token),
            user_id=user.id,
            expires_at=datetime.now(timezone.utc) + timedelta(days=TOKEN_TTL_DAYS),
        )
    )
    await session.commit()
    return token


async def get_current_user(request: Request, session: AsyncSession = Depends(get_session)) -> User:
    """从 Authorization: Bearer <token> 解析当前用户;无效/过期返回 401。"""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    token = header.removeprefix("Bearer ").strip()
    row = (
        await session.execute(
            select(AuthToken).where(AuthToken.token_hash == _hash_token(token))
        )
    ).scalar_one_or_none()
    if row is None or row.expires_at < datetime.now(timezone.utc):
        raise HTTPException(401, "登录已过期,请重新登录")
    user = await session.get(User, row.user_id)
    if user is None:
        raise HTTPException(401, "用户不存在")
    return user


@router.post("/auth/register", response_model=dict, status_code=201)
async def register(payload: RegisterIn, session: AsyncSession = Depends(get_session)):
    existing = (
        await session.execute(select(User).where(User.email == str(payload.email)))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, "该邮箱已注册,请直接登录")
    user = User(email=str(payload.email), name=payload.name, password_hash=hash_password(payload.password))
    session.add(user)
    await session.commit()
    await session.refresh(user)
    token = await _issue_token(session, user)
    return {"token": token, "user": UserOut.model_validate(user)}


@router.post("/auth/login", response_model=dict)
async def login(payload: LoginIn, session: AsyncSession = Depends(get_session)):
    user = (
        await session.execute(select(User).where(User.email == str(payload.email)))
    ).scalar_one_or_none()
    if user is None or not user.password_hash or not verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "邮箱或密码错误")
    token = await _issue_token(session, user)
    return {"token": token, "user": UserOut.model_validate(user)}


@router.post("/auth/logout")
async def logout(request: Request, session: AsyncSession = Depends(get_session)):
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        token = header.removeprefix("Bearer ").strip()
        row = (
            await session.execute(
                select(AuthToken).where(AuthToken.token_hash == _hash_token(token))
            )
        ).scalar_one_or_none()
        if row is not None:
            await session.delete(row)
            await session.commit()
    return {"ok": True}


@router.get("/auth/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return user
