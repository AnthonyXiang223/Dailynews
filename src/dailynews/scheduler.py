"""进程内调度:每用户 digest_time 的每日简报任务 + 画像 deriver 每日清扫。

APScheduler 3.x(AsyncIOScheduler),进程内即可满足骨架需求;
生产环境多进程/多机时再迁移 Celery/Temporal。
"""
import asyncio
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from .agent.run import run_daily_digest
from .db import SessionLocal
from .models import Subscription, User
from .profile.deriver import sweep_all

_scheduler: AsyncIOScheduler | None = None
_locks: dict[str, asyncio.Lock] = {}


def _user_lock(user_id) -> asyncio.Lock:
    """每用户一把锁:防止手动触发与定时任务并发跑同一天的简报。"""
    key = str(user_id)
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]


async def run_user_digest_safe(user_id) -> dict:
    """调度入口:加锁后执行完整流水线。"""
    async with _user_lock(user_id):
        return await run_daily_digest(user_id)


async def install_scheduler() -> None:
    """启动调度器:注册 deriver 清扫任务 + 为所有已有订阅注册每日任务。"""
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    _scheduler.add_job(
        _derive_sweep,
        CronTrigger(hour=2, minute=7),  # 避开整点,减少全局瞬时负载
        id="derive_sweep",
        coalesce=True,
        max_instances=1,
    )
    await rebuild_jobs()
    _scheduler.start()
    print("[scheduler] 调度器已启动")


async def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


async def _derive_sweep() -> None:
    n = await sweep_all()
    print(f"[scheduler] 画像清扫完成,共更新 {n} 个用户")


async def reschedule_user(user_id) -> None:
    """订阅变更后重建该用户的每日任务(按用户时区 + digest_time)。"""
    if _scheduler is None:
        return
    async with SessionLocal() as session:
        sub = (
            await session.execute(select(Subscription).where(Subscription.user_id == user_id))
        ).scalar_one_or_none()
        user = await session.get(User, user_id)
    if sub is None or user is None:
        return
    tz_name = user.timezone or "Asia/Shanghai"
    try:
        ZoneInfo(tz_name)  # 校验时区合法性
    except Exception:  # noqa: BLE001
        tz_name = "Asia/Shanghai"

    job_id = f"user_{user_id}"
    old = _scheduler.get_job(job_id)
    if old is not None:
        old.remove()
    _scheduler.add_job(
        run_user_digest_safe,
        CronTrigger(hour=sub.digest_time.hour, minute=sub.digest_time.minute, timezone=tz_name),
        args=[user_id],
        id=job_id,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    print(f"[scheduler] 用户 {user_id} 定时任务已更新:每日 {sub.digest_time:%H:%M}({tz_name})")


async def rebuild_jobs() -> None:
    """启动时为所有订阅注册任务。"""
    assert _scheduler is not None
    async with SessionLocal() as session:
        subs = (await session.execute(select(Subscription))).scalars().all()
    for sub in subs:
        await reschedule_user(sub.user_id)
    print(f"[scheduler] 已注册 {len(subs)} 个用户的每日任务")
