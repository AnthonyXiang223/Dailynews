"""粗筛与确定性打分:纯函数 match_and_score / select_candidates,便于离线测试。

说明:主题命中采用"主题词出现在文章文本中"的朴素匹配,骨架期够用;
生产环境应把主题映射为关键词组,或用 embedding 相似度召回。
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select

from ..config import get_settings
from ..models import Article, Digest, Subscription, User
from .ingest import STUB_SOURCE
from .topics import load_topic_terms


def _haystack(art: dict) -> str:
    return " ".join(x or "" for x in (art.get("title"), art.get("summary"), art.get("content"))).lower()


def match_and_score(
    art: dict,
    subscription,
    profile: dict | None,
    today_cutoff: datetime | None = None,
    topic_terms: dict[str, list[str]] | None = None,
) -> dict | None:
    """单篇文章对订阅的匹配与打分;不命中返回 None。

    得分 = 关键词/主题命中权重 + 时效衰减(6h 半衰)+ 今日优先加成 + L3 画像加成。
    主题命中优先用词表(topic_terms):组内任一词命中标题 +2.0、仅命中正文 +0.5
    (压制营销文案噪音);未传词表时退化为主题词字面匹配(1.5)。
    """
    keywords = subscription.keywords or []
    topics = subscription.topics or []
    exclude = subscription.exclude_keywords or []
    sources = subscription.sources or []

    text = _haystack(art)
    if any(ex.lower() in text for ex in exclude):
        return None
    if sources and art.get("source") not in sources:
        return None

    matched_keywords = [k for k in keywords if k.lower() in text]
    if topic_terms is None:
        matched_topics = [t for t in topics if t.lower() in text]
        topic_score = 1.5 * len(matched_topics)
    else:
        matched_topics = []
        topic_score = 0.0
        title = (art.get("title") or "").lower()
        for t in topics:
            group = topic_terms.get(t, [])
            hit_title = t.lower() in title or any(term.lower() in title for term in group)
            hit_text = t.lower() in text or any(term.lower() in text for term in group)
            if hit_title:
                matched_topics.append(t)
                topic_score += 2.0
            elif hit_text:
                matched_topics.append(t)
                topic_score += 0.5
    if not matched_keywords and not matched_topics:
        return None

    score = 2.0 * len(matched_keywords) + topic_score

    # 时效衰减:6 小时半衰
    published_at = art.get("published_at")
    if published_at:
        hours = max((datetime.now(timezone.utc) - published_at).total_seconds() / 3600, 0)
        score *= 0.5 ** (hours / 6)

    # 今日优先:用户时区零点后发布的文章 +3,保证今日新闻占满前列
    if today_cutoff is not None and published_at is not None and published_at >= today_cutoff:
        score += 3.0

    # L3 画像加成(有上限,避免画像完全支配排序)
    affinity = (profile or {}).get("topic_affinity", {})
    for topic in matched_topics:
        score += min(affinity.get(topic, {}).get("score", 0.0), 3.0) * 0.5

    return {
        **art,
        "matched_keywords": matched_keywords,
        "matched_topics": matched_topics,
        "score": round(score, 3),
    }


def select_candidates(
    articles: list[dict],
    subscription,
    profile: dict | None,
    today_cutoff: datetime | None = None,
    topic_terms: dict[str, list[str]] | None = None,
) -> list[dict]:
    """纯函数:对文章列表批量打分,过滤并降序排序。"""
    scored = [
        s for s in (match_and_score(a, subscription, profile, today_cutoff, topic_terms) for a in articles) if s
    ]
    scored.sort(key=lambda x: (-x["score"], x.get("id") or 0))
    return scored


async def fetch_candidates(session, user: User, subscription: Subscription, limit: int = 30) -> list[dict]:
    """从库中取时间窗口内的文章,按订阅粗筛打分,返回 top-N 候选。

    窗口下限 = 上一次简报的生成时间(不重复推已看过的内容),
    并封顶 24 小时(避免手动补跑时窗口无界)。
    """
    settings = get_settings()
    fallback = datetime.now(timezone.utc) - timedelta(hours=settings.news_window_hours)
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(user.timezone or "Asia/Shanghai")
    except Exception:  # noqa: BLE001
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Asia/Shanghai")
    local_today = datetime.now(tz).date()

    # 窗口下限:上一次简报是"之前日期"的 → 以它的生成时间为下限(不重复推已看过的);
    # 同一天的重生成 → 回退 24h 窗口(否则重跑会因窗口过窄而空)
    prev_digest = (
        await session.execute(
            select(Digest).where(Digest.user_id == user.id).order_by(Digest.date.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if prev_digest is not None and prev_digest.date < local_today:
        cutoff = max(prev_digest.updated_at, fallback)
    else:
        cutoff = fallback
    conditions = [
        or_(
            Article.published_at >= cutoff,
            Article.published_at.is_(None),
            Article.source == STUB_SOURCE,  # 样例数据不参与窗口过滤(离线演示永远"新鲜")
        )
    ]
    if settings.news_sources.strip():
        conditions.append(Article.source != STUB_SOURCE)  # 配置了真实源就不再混入样例
    rows = (
        await session.execute(
            select(Article).where(*conditions).order_by(Article.published_at.desc().nulls_last())
        )
    ).scalars().all()
    articles = [
        {
            "id": r.id,
            "url": r.url,
            "title": r.title,
            "summary": r.summary,
            "content": r.content,
            "source": r.source,
            "published_at": r.published_at,
        }
        for r in rows
    ]
    # 今日优先的截止线:用户时区的今天零点
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(user.timezone or "Asia/Shanghai")
    except Exception:  # noqa: BLE001
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Asia/Shanghai")
    local_midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    topic_terms = await load_topic_terms(session, subscription.topics or [])
    scored = select_candidates(articles, subscription, user.profile or {}, local_midnight, topic_terms)

    # 数据层硬约束:今日优先,昨天(及更早)的候选最多给 ⅓ 名额,
    # 确保简报以今日新闻为主;提示词层面数不清,这里兜底
    def _is_today(s: dict) -> bool:
        return bool(s.get("published_at") and s["published_at"] >= local_midnight)

    today_items = [s for s in scored if _is_today(s)]
    older_items = [s for s in scored if not _is_today(s)]
    max_older = max(2, limit // 3)
    return (today_items + older_items[:max_older])[:limit]
