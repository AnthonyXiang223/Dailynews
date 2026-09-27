"""API 路由聚合。"""
from fastapi import APIRouter

from . import admin, auth, digests, subscriptions, users

router = APIRouter()
router.include_router(auth.router)
router.include_router(admin.router)
router.include_router(users.router)
router.include_router(subscriptions.router)
router.include_router(digests.router)
