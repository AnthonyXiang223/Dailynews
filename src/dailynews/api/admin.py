"""运维接口:手动触发简报生成 / 画像更新(仅限本人)。"""
import asyncio
import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException

from ..agent.run import run_daily_digest
from ..models import User
from ..profile.deriver import derive_user
from .auth import get_current_user

router = APIRouter(tags=["admin"])


def _spawn(coro) -> None:
    """后台任务包装:create_task 的异常默认被静默吞掉,这里强制落日志。"""
    task = asyncio.create_task(coro)

    def _log_exc(done):
        if not done.cancelled() and done.exception() is not None:
            print(f"[background-task] 异常: {done.exception()!r}")

    task.add_done_callback(_log_exc)


async def _own(user_id: uuid.UUID, current: User) -> None:
    if user_id != current.id:
        raise HTTPException(403, "只能操作自己的数据")


@router.post("/admin/users/{user_id}/run")
async def trigger_run(
    user_id: uuid.UUID,
    digest_date: date | None = None,
    current: User = Depends(get_current_user),
):
    """手动触发一次简报生成(202 立即返回,后台执行;结果见 digests/deliveries)。"""
    await _own(user_id, current)
    _spawn(_run(user_id, digest_date))
    return {"ok": True, "message": "已提交,后台执行中"}


async def _run(user_id: uuid.UUID, digest_date: date | None):
    # 手动触发允许指定日期(定时任务固定为当天);带每用户锁与定时任务互斥
    # 手动生成不自动发邮箱:用户看满意后点「发送到邮箱」按钮
    from ..scheduler import _user_lock

    async with _user_lock(user_id):
        result = await run_daily_digest(user_id, digest_date, auto_deliver=False)
        print(f"[admin] 手动触发完成: {result}")


@router.post("/admin/users/{user_id}/derive")
async def trigger_derive(user_id: uuid.UUID, current: User = Depends(get_current_user)):
    """手动触发画像更新。"""
    await _own(user_id, current)
    _spawn(derive_user(user_id))
    return {"ok": True, "message": "画像更新已提交"}
