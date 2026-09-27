"""每日简报运行入口。

两种模式:
- Agent 模式(配置了 LLM key):主 Agent 通过 6 个工具自主完成
  采集 → 策展 → 撰写 → 核对 → 推送 全流程,代码不预设顺序;
  系统只做兜底:没落库则确定性重生成,没投递则系统补投。
- 确定性模式(无 key 或 Agent 失败):直接顺序调用与工具相同的
  业务函数,零 LLM 可离线运行。两种模式共用 deliver_digest,行为一致。
"""
import uuid
from datetime import date, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ..config import get_settings
from ..db import SessionLocal
from ..models import Digest, Subscription, User
from ..pipeline.compose import compose_digest, rewrite_item_links, sanitize_digest
from ..pipeline.filter import fetch_candidates
from ..pipeline.ingest import ingest_all
from .llm import build_chat_model
from .prompts import CURATOR_PROMPT, MAIN_PROMPT, WRITER_PROMPT, Selection
from .tools import (
    add_topic_term_tool,
    deliver_digest,
    fetch_candidates_tool,
    get_profile_tool,
    ingest_sources_tool,
    read_digest_tool,
    save_digest_tool,
    summarize_article_tool,
)


@lru_cache
def _build_agent():
    """编译 deepagents 图(进程内缓存一份)。

    planning 在 v0.7+ 需显式开启 TodoListMiddleware;
    主 Agent 持全部 6 个工具,子代理按最小权限只给必要的工具。
    """
    from deepagents import create_deep_agent
    from langchain.agents.middleware import TodoListMiddleware

    return create_deep_agent(
        model=build_chat_model(),
        tools=[
            ingest_sources_tool,
            fetch_candidates_tool,
            get_profile_tool,
            add_topic_term_tool,
            summarize_article_tool,
            save_digest_tool,
            read_digest_tool,
        ],
        system_prompt=MAIN_PROMPT,
        middleware=[TodoListMiddleware()],
        subagents=[
            {
                "name": "curator",
                "description": "内容策展:按用户画像从候选文章中筛选打分,可补充主题词表,输出结构化选文 JSON。",
                "system_prompt": CURATOR_PROMPT,
                "tools": [fetch_candidates_tool, get_profile_tool, add_topic_term_tool],
                "response_format": Selection,
            },
            {
                "name": "writer",
                "description": "简报撰稿:为每条选文调用 summarize_article_tool 生成摘要,组装成中文简报 markdown 并保存。",
                "system_prompt": WRITER_PROMPT,
                "tools": [get_profile_tool, summarize_article_tool, save_digest_tool],
            },
        ],
    )


async def _get_digest(session, user_id: uuid.UUID, d: date) -> Digest | None:
    """populate_existing 绕过会话缓存——Agent 工具在独立会话里写库,这里必须读到最新值。"""
    return (
        await session.execute(
            select(Digest)
            .where(Digest.user_id == user_id, Digest.date == d)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _try_agent_run(user_id: uuid.UUID, d: date) -> None:
    """Agent 模式:全流程交给主 Agent(工具驱动,无预设顺序);异常只记录,由兜底接管。"""
    try:
        agent = _build_agent()
        await agent.ainvoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": f"请为用户 {user_id} 生成 {d:%Y-%m-%d} 的每日 AI 新闻简报,并完成推送。",
                    }
                ]
            },
            config={"configurable": {"thread_id": f"user:{user_id}:{d}"}},
        )
    except Exception as exc:  # noqa: BLE001 降级路径,不允许中断当天简报
        print(f"[agent] LLM 模式执行失败,降级到确定性模式: {exc!r}")


async def _run_deterministic(session, user: User, sub: Subscription, user_id: uuid.UUID, d: date) -> Digest:
    """确定性 fallback:顺序执行与 Agent 工具相同的业务函数,离线可用。"""
    await ingest_all(session)
    candidates = await fetch_candidates(session, user, sub, limit=30)
    digest_row = await _get_digest(session, user_id, d)
    digest_id = digest_row.id if digest_row else uuid.uuid4()
    title, content_md, items = compose_digest(str(user_id), user.name, sub, candidates, digest_id, d)
    if digest_row is None:
        digest_row = Digest(
            id=digest_id, user_id=user_id, date=d, title=title,
            content_md=content_md, items=items, status="composed", model_used=None,
        )
        session.add(digest_row)
    else:
        digest_row.title = title
        digest_row.content_md = content_md
        digest_row.items = items
        digest_row.model_used = None
        digest_row.status = "composed"
    await session.commit()
    return digest_row


async def run_daily_digest(user_id: uuid.UUID, digest_date: date | None = None, auto_deliver: bool = True) -> dict:
    """单用户每日简报:Agent 自主编排(或确定性兜底)+ 按场景投递。

    auto_deliver=True(定时任务):生成后自动发邮箱;
    auto_deliver=False(界面手动生成):只生成不投递,用户满意后点「发送到邮箱」。
    """
    async with SessionLocal() as session:
        user = await session.get(User, user_id)
        if user is None:
            return {"ok": False, "error": f"用户 {user_id} 不存在"}

        try:
            tz = ZoneInfo(user.timezone or "Asia/Shanghai")
        except Exception:  # noqa: BLE001 非法时区兜底
            tz = ZoneInfo("Asia/Shanghai")
        d = digest_date or datetime.now(tz).date()

        sub = (
            await session.execute(select(Subscription).where(Subscription.user_id == user_id))
        ).scalar_one_or_none()
        if sub is None:
            sub = Subscription(user_id=user_id)  # 默认订阅:全部主题、简洁、10 条
            session.add(sub)
            await session.commit()

        # 1. 生成:Agent 模式自主完成;无 key / Agent 失败 / 未落库 → 确定性兜底
        mode = "agent" if get_settings().llm_api_key else "deterministic"
        if mode == "agent":
            await _try_agent_run(user_id, d)
        digest_row = await _get_digest(session, user_id, d)
        if digest_row is None or not digest_row.content_md:
            mode = "deterministic"
            digest_row = await _run_deterministic(session, user, sub, user_id, d)

        # 2. 生成后统一清洗(栏目标题/裸URL/内部字段)+ 修正链接——手动生成不投递也要干净
        sanitize_digest(digest_row, sub.topics or [])
        rewrite_item_links(digest_row)
        await session.commit()

        # 3. 投递:定时任务自动发邮箱;手动生成不投递(用户看满意了点「发送到邮箱」)
        if auto_deliver:
            result = await deliver_digest(digest_row.id)
        else:
            result = {"status": "skipped"}

        return {
            "ok": True,
            "digest_id": str(digest_row.id),
            "mode": mode,
            "delivery": result.get("status"),
        }
