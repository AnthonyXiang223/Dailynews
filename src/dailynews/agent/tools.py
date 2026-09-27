"""Agent 的工具层:业务能力封装为 6 个工具,由主 Agent 自主调用驱动全流程。

工具集:
- ingest_sources     采集 RSS → 文章库(带去重)
- fetch_candidates   查候选文章(按订阅粗筛 + 确定性打分)
- get_profile        读用户画像(L1 订阅 + L3 派生)
- save_digest        保存简报正文
- read_digest        核对简报落库状态与内容
- deliver_digest     推送简报(附加反馈 footer、记投递、触发画像更新)

确定性 fallback 直接调用同一批底层业务函数(不经 LLM 工具包装),
保证 Agent 模式与离线模式行为一致、实现不重复。
"""
import asyncio
import json
import uuid
from datetime import date

from langchain_core.tools import tool
from sqlalchemy import func, select

from ..config import get_settings
from ..db import SessionLocal
from ..models import Article, Delivery, Digest, Subscription, TopicTerm, User
from ..notify import build_notifier
from ..pipeline.compose import rewrite_item_links, sanitize_digest
from ..pipeline.filter import fetch_candidates
from ..pipeline.ingest import ingest_all
from ..pipeline.topics import KNOWN_TOPICS
from ..profile.deriver import derive_user
from .llm import build_chat_model

SUMMARY_PROMPT = """你是新闻摘要编辑。用 2-3 个句子客观概括下面这条新闻,让读者快速了解它说了什么。

要求:
- 只陈述新闻内容本身,平实客观;不要评论、不要推荐、不要"值得关注"之类的引导语
- 直接输出摘要正文,不要任何前缀、标题或解释
- 输出中文"""


@tool
async def summarize_article_tool(article_id: int) -> str:
    """为单篇文章生成 2-3 句客观摘要(逐条独立调用,保证质量)。

    简报写作时对每篇入选文章调用一次,把返回的摘要原文放进简报对应条目。

    Args:
        article_id: 文章 id。

    Returns:
        2-3 句中文摘要(客观陈述,无点评推荐)。
    """
    async with SessionLocal() as session:
        article = await session.get(Article, article_id)
        if article is None:
            return "错误:文章不存在"
        raw = "\n".join(x for x in (article.title, article.summary, article.content) if x)[:2000]
        settings = get_settings()
        if not settings.llm_api_key:
            # 无 LLM 时退回原文摘要截断
            return (article.summary or article.title or "")[:120]
    llm = build_chat_model()
    resp = await llm.ainvoke([{"role": "user", "content": f"{SUMMARY_PROMPT}\n\n新闻内容:\n{raw}"}])
    text = resp.content if isinstance(resp.content, str) else "".join(
        b.get("text", "") for b in resp.content if isinstance(b, dict)
    )
    return text.strip()


def _spawn_derive(user_id) -> None:
    """投递后触发画像更新(fire-and-forget,异常落日志)。"""
    try:
        task = asyncio.create_task(derive_user(user_id))
    except RuntimeError:
        return  # 无事件循环(测试环境)

    def _log(done):
        if not done.cancelled() and done.exception() is not None:
            print(f"[background-task] 画像更新异常: {done.exception()!r}")

    task.add_done_callback(_log)


async def deliver_digest(digest_id) -> dict:
    """统一投递实现(工具与确定性 fallback 共用):
    修正正文标题链接(原文直链)→ 推送 → 记 Delivery → 更新状态 → 触发画像更新。

    注意:发信(尤其 SMTP)可能耗时数秒~十几秒,期间必须**释放数据库连接**
    (先把清洗结果提交、关会话,发完再开新会话记投递),避免占着连接池
    让其他请求排队。
    """
    # 1. 读取 + 清洗 + 落库(会话尽快释放)
    async with SessionLocal() as session:
        digest = await session.get(Digest, uuid.UUID(str(digest_id)))
        if digest is None:
            return {"status": "failed", "error": f"简报 {digest_id} 不存在"}
        user = await session.get(User, digest.user_id)
        if user is None:
            return {"status": "failed", "error": "用户不存在"}
        sub = (
            await session.execute(select(Subscription).where(Subscription.user_id == user.id))
        ).scalar_one_or_none()

        sanitize_digest(digest, (sub.topics if sub else []) or [])  # 清洗栏目标题/裸URL/内部字段
        rewrite_item_links(digest)  # 正文标题链接统一修正为原文直链
        notifier = build_notifier(user)
        await session.commit()
        user_id = digest.user_id
        digest_id_uuid = digest.id

    # 2. 推送(不占连接;SMTP 在 to_thread 中执行,不冻结事件循环;
    #    user/digest 已脱离会话但属性都在内存里,expire_on_commit=False 保证可读)
    result = await notifier.send(user, digest)

    # 3. 记投递结果 + 更新状态(新会话)
    async with SessionLocal() as session:
        digest = await session.get(Digest, digest_id_uuid)
        if digest is not None:
            digest.status = "delivered" if result.get("status") == "sent" else "failed"
            session.add(
                Delivery(
                    digest_id=digest_id_uuid,
                    channel=notifier.channel,
                    status=result.get("status", "sent"),
                    error=result.get("error"),
                    payload=result.get("payload"),
                )
            )
            await session.commit()
    _spawn_derive(user_id)
    return {"status": result.get("status"), "channel": notifier.channel, "digest_id": str(digest_id_uuid)}


# ---------- 工具定义(供 Agent 调用) ----------


@tool
async def ingest_sources_tool() -> str:
    """采集最新文章:抓取所有配置的 RSS 源(URL 去重)写入文章库。

    在简报生成开始时调用;若候选文章不足,可以再次调用尝试获取更多。
    返回本次新增条数与文章库总数。
    """
    async with SessionLocal() as session:
        new_count = await ingest_all(session)
        total = (await session.execute(select(func.count()).select_from(Article))).scalar()
    return f"采集完成:本次新增 {new_count} 篇,文章库共 {total} 篇。新增为 0 说明各源暂无新文章,直接使用库中候选即可。"


@tool
async def add_topic_term_tool(topic: str, term: str) -> str:
    """给某个主题的检索词表补充一个新词(你从候选文章中新发现的代表性词)。

    补充后词表持久化保存,本次及以后运行的主题匹配都会使用该词。
    例如发现新模型名「Kimi」在候选里反复出现且尚未被匹配到,可执行
    add_topic_term_tool(topic="大模型", term="Kimi")。

    Args:
        topic: 主题名,必须是订阅里已有的主题(如 大模型/智能体/开源/芯片/机器人/数据安全/产品/政策监管)。
        term: 要补充的检索词(一个词或短语,不要带标点)。

    Returns:
        补充结果说明。
    """
    if topic not in KNOWN_TOPICS:
        return f"主题 {topic} 不在已知主题中,可选: {'、'.join(KNOWN_TOPICS)}"
    term = term.strip()
    if not term:
        return "检索词不能为空"
    async with SessionLocal() as session:
        existing = (
            await session.execute(
                select(TopicTerm).where(TopicTerm.topic == topic, TopicTerm.term == term)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return f"词「{term}」已在主题 {topic} 的词表中"
        session.add(TopicTerm(topic=topic, term=term, created_by="llm"))
        await session.commit()
    return f"已把「{term}」加入主题 {topic} 的词表,后续匹配生效"


@tool
async def fetch_candidates_tool(user_id: str) -> str:
    """获取某用户的候选文章(已按订阅粗筛并确定性打分,最多 30 条)。

    主题匹配基于词表(主题→一组检索词,标题命中加权):匹配不到时,
    可先调用 add_topic_term_tool 补充新词再重新获取。

    Args:
        user_id: 用户 UUID 字符串。

    Returns:
        候选文章 JSON 数组,每项含 id/title/summary/source/score/matched_topics/matched_keywords。
    """
    async with SessionLocal() as session:
        user = await session.get(User, uuid.UUID(user_id))
        if user is None:
            return "错误:用户不存在"
        sub = (
            await session.execute(select(Subscription).where(Subscription.user_id == user.id))
        ).scalar_one_or_none()
        if sub is None:
            return "该用户还没有订阅偏好,请基于默认偏好(全部主题)继续"
        candidates = await fetch_candidates(session, user, sub, limit=30)
    fields = ("id", "url", "title", "summary", "source", "score", "matched_topics", "matched_keywords")
    return json.dumps(
        [{k: c.get(k) for k in fields} for c in candidates],
        ensure_ascii=False,
        default=str,
    )


@tool
async def get_profile_tool(user_id: str) -> str:
    """获取用户画像:显式订阅偏好(L1)+ 派生画像(L3)。

    Args:
        user_id: 用户 UUID 字符串。

    Returns:
        JSON:{subscription: {topics, keywords, exclude_keywords, sources, tone, max_items},
              profile: {topic_affinity, persona, ...}, name}
    """
    async with SessionLocal() as session:
        user = await session.get(User, uuid.UUID(user_id))
        if user is None:
            return "错误:用户不存在"
        sub = (
            await session.execute(select(Subscription).where(Subscription.user_id == user.id))
        ).scalar_one_or_none()
        data = {
            "name": user.name,
            "subscription": {
                "topics": (sub.topics if sub else []),
                "keywords": (sub.keywords if sub else []),
                "exclude_keywords": (sub.exclude_keywords if sub else []),
                "sources": (sub.sources if sub else []),
                "tone": (sub.tone if sub else "concise"),
                "max_items": (sub.max_items if sub else 10),
            },
            "profile": user.profile or {},
        }
    return json.dumps(data, ensure_ascii=False, default=str)


@tool
async def save_digest_tool(user_id: str, digest_date: str, title: str, content_md: str, items_json: str) -> str:
    """把写好的简报保存到数据库(同一天重复保存会覆盖旧内容)。

    Args:
        user_id: 用户 UUID 字符串。
        digest_date: 简报日期,格式 YYYY-MM-DD。
        title: 简报标题。
        content_md: 简报正文 markdown(全文中文)。
        items_json: 入选文章的 JSON 数组字符串,每项含
            {article_id: int, title: str, url: str, score: float, reason: str}。

    Returns:
        保存后的 digest_id(UUID 字符串)。
    """
    async with SessionLocal() as session:
        items = json.loads(items_json)
        d = date.fromisoformat(digest_date)
        uid = uuid.UUID(user_id)
        existing = (
            await session.execute(select(Digest).where(Digest.user_id == uid, Digest.date == d))
        ).scalar_one_or_none()
        if existing is not None:
            existing.title = title
            existing.content_md = content_md
            existing.items = items
            existing.status = "composed"
            existing.model_used = get_settings().llm_model
            await session.commit()
            return str(existing.id)
        digest = Digest(
            id=uuid.uuid4(),
            user_id=uid,
            date=d,
            title=title,
            content_md=content_md,
            items=items,
            status="composed",
            model_used=get_settings().llm_model,
        )
        session.add(digest)
        await session.commit()
        return str(digest.id)


@tool
async def read_digest_tool(digest_id: str) -> str:
    """读取某简报的落库状态、投递记录与内容预览,用于核对保存/推送结果。

    Args:
        digest_id: 简报 UUID 字符串。

    Returns:
        JSON:{digest_id, date, title, status, model_used, item_count,
              deliveries: [{channel, status, sent_at}], content_preview}
    """
    async with SessionLocal() as session:
        digest = await session.get(Digest, uuid.UUID(digest_id))
        if digest is None:
            return "简报不存在(可能尚未保存)"
        deliveries = (
            await session.execute(
                select(Delivery).where(Delivery.digest_id == digest.id).order_by(Delivery.id.desc())
            )
        ).scalars().all()
        data = {
            "digest_id": str(digest.id),
            "date": str(digest.date),
            "title": digest.title,
            "status": digest.status,
            "model_used": digest.model_used,
            "item_count": len(digest.items or []),
            "deliveries": [
                {"channel": d.channel, "status": d.status, "sent_at": str(d.sent_at)} for d in deliveries
            ],
            "content_preview": (digest.content_md or "")[:400],
        }
    return json.dumps(data, ensure_ascii=False, default=str)


