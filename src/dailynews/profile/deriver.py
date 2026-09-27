"""画像 Deriver:L2 行为事件 → L3 派生画像(users.profile JSONB)。

设计参照 honcho 的 Derive/Consolidate 思路,但完全自建:
- 事件按主题聚合,加权 + 7 天半衰时间衰减
- trend = 近 7 天 vs 更早的差值符号,表示兴趣走向
- persona 为中文模板句,零 LLM 依赖,便宜且确定

画像结构:
  {"topic_affinity": {主题: {"score": float, "trend": 1|0|-1, "n": int}},
   "persona": "偏好:大模型、智能体。近期对 芯片 兴趣下降",
   "updated_at": iso8601}
"""
import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..db import SessionLocal
from ..models import BehaviorEvent, User

WEIGHTS = {
    "click": 0.2,
    "read": 0.5,
    "upvote": 1.0,
    "downvote": -1.0,
    # 取消事件:撤销对应投票的影响(取消后画像同步回落)
    "upvote_cancel": -1.0,
    "downvote_cancel": 1.0,
    "reduce_topic": -2.0,
}
HALF_LIFE_DAYS = 7.0
LOOKBACK_DAYS = 30


def compute_affinity(events: list[BehaviorEvent]) -> dict[str, dict]:
    """纯函数:事件列表 → 主题亲和度。便于离线测试。"""
    now = datetime.now(timezone.utc)
    recent: dict[str, float] = {}
    older: dict[str, float] = {}
    counts: dict[str, int] = {}

    for ev in events:
        topic = ev.topic
        if not topic:
            continue
        days = max((now - ev.created_at).total_seconds() / 86400, 0)
        weight = WEIGHTS.get(ev.event_type, 0.0) * math.exp(-days * math.log(2) / HALF_LIFE_DAYS)
        bucket = recent if days <= 7 else older
        bucket[topic] = bucket.get(topic, 0.0) + weight
        counts[topic] = counts.get(topic, 0) + 1

    affinity = {}
    for topic in set(recent) | set(older):
        r, o = recent.get(topic, 0.0), older.get(topic, 0.0)
        affinity[topic] = {
            "score": round(r + o, 3),
            "trend": 1 if r > o else (-1 if r < o else 0),
            "n": counts[topic],
        }
    return affinity


def build_persona(affinity: dict[str, dict]) -> str:
    """纯函数:亲和度 → 中文画像描述。"""
    positive = sorted(
        [(t, v) for t, v in affinity.items() if v["score"] > 0],
        key=lambda x: -x[1]["score"],
    )[:2]
    declining = [t for t, v in affinity.items() if v["trend"] < 0][:1]
    parts = []
    if positive:
        parts.append("偏好:" + "、".join(t for t, _ in positive))
    if declining:
        parts.append("近期对 " + "、".join(declining) + " 兴趣下降")
    return "。".join(parts) if parts else "暂无足够行为数据"


async def derive_user(user_id) -> dict | None:
    """重算单个用户的 L3 画像。"""
    since = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    async with SessionLocal() as session:
        user = await session.get(User, user_id)
        if user is None:
            return None
        events = (
            await session.execute(
                select(BehaviorEvent)
                .where(BehaviorEvent.user_id == user_id, BehaviorEvent.created_at >= since)
                .order_by(BehaviorEvent.created_at)
            )
        ).scalars().all()

        affinity = compute_affinity(events)
        profile = dict(user.profile or {})
        profile["topic_affinity"] = affinity
        profile["persona"] = build_persona(affinity)
        profile["updated_at"] = datetime.now(timezone.utc).isoformat()
        user.profile = profile
        await session.commit()
        return profile


async def sweep_all() -> int:
    """对最近有行为的用户批量更新画像(调度器每日执行)。"""
    since = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    async with SessionLocal() as session:
        user_ids = (
            await session.execute(
                select(BehaviorEvent.user_id).distinct().where(BehaviorEvent.created_at >= since)
            )
        ).scalars().all()
    for user_id in user_ids:
        await derive_user(user_id)
    return len(user_ids)
