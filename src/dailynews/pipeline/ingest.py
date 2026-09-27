"""内容采集:RSS 源 + 内置样例(stub),URL 归一化去重后入 articles 表。

设计:采集是确定性步骤,不经过 Agent;去重幂等,多次运行安全。
"""
import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse

import feedparser
import httpx
from sqlalchemy.dialects.postgresql import insert

from ..config import get_settings
from ..models import Article

STUB_SOURCE = "内置样例"


def normalize_url(url: str) -> str:
    """去掉 fragment、跟踪参数、末尾斜杠,统一 scheme/host 大小写。"""
    parts = urlparse(url.strip())
    clean = urlunparse(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", "", "", "")
    )
    return clean


def url_hash(url: str) -> str:
    return hashlib.md5(normalize_url(url).encode()).hexdigest()


def stub_articles() -> list[dict]:
    """内置样例数据:无网络/无 RSS 源时保证流水线可离线演示。"""
    now = datetime.now(timezone.utc)
    samples = [
        ("OpenAI 发布新一代大模型,推理成本再降 40%",
         "OpenAI 今日发布新一代大模型,官方称推理成本较上代下降 40%,API 价格同步下调。"),
        ("国产大模型 DeepSeek 更新:数学推理能力显著提升",
         "DeepSeek 发布新版本,数学与代码推理评测得分刷新纪录,开源社区关注度高涨。"),
        ("智能体框架竞争加剧:LangGraph 与 OpenAI Agents SDK 正面交锋",
         "多智能体编排框架成为新战场,两大框架在工具调用与持久化能力上展开竞争。"),
        ("开源大模型社区爆发:多款轻量模型可本地部署",
         "多家机构发布开源轻量大模型,消费级显卡即可运行,开源生态持续繁荣。"),
        ("芯片巨头发布新一代 AI 训练芯片,算力翻倍",
         "新一代 AI 芯片正式发布,单卡算力较上代提升一倍,面向大规模训练场景。"),
        ("人形机器人公司获新一轮融资,具身智能加速落地",
         "国内人形机器人厂商完成新一轮融资,具身智能在工业场景的应用进入验证期。"),
        ("数据安全新规落地,AI 训练数据合规要求明确",
         "监管部门发布数据安全新规,对 AI 训练数据的采集与使用提出明确合规要求。"),
        ("AI 编程工具内卷:多家产品宣布支持智能体协作",
         "AI 编程工具纷纷上线智能体协作能力,开发者工作流正在被重塑。"),
    ]
    return [
        {
            "url": f"https://example.com/news/2026-09-{i + 1:02d}",
            "title": title,
            "summary": summary,
            "content": f"{title}。{summary}",
            "source": STUB_SOURCE,
            "published_at": now - timedelta(hours=2 + i * 3),  # 错开时间,模拟时效衰减
            "meta": {"stub": True},
        }
        for i, (title, summary) in enumerate(samples)
    ]


async def fetch_rss(source: str, timeout: float = 15.0) -> list[dict]:
    """抓取单个 RSS 源,解析为文章字典列表。"""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        # 部分站点(InfoQ/开源中国)对非浏览器 UA 直接 403/451
        resp = await client.get(
            source,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 DailyNewsBot/0.1"
                )
            },
        )
        resp.raise_for_status()
    feed = feedparser.parse(resp.text)
    items: list[dict] = []
    for entry in feed.entries[:50]:
        link = entry.get("link", "")
        if not link:
            continue
        pub = entry.get("published_parsed") or entry.get("updated_parsed")
        published_at = datetime(*pub[:6], tzinfo=timezone.utc) if pub else None
        items.append(
            {
                "url": link,
                "title": entry.get("title", ""),
                "summary": entry.get("summary", ""),
                "content": "",
                "source": feed.feed.get("title", source),
                "published_at": published_at,
                "meta": {"tags": [getattr(t, "term", None) for t in entry.get("tags", [])]},
            }
        )
    return items


async def ingest_all(session) -> int:
    """抓取所有配置源 + 内置样例,去重写入。返回新增条数。"""
    settings = get_settings()
    sources = [s.strip() for s in settings.news_sources.split(",") if s.strip()]
    items: list[dict] = []
    for source in sources:
        try:
            items += await fetch_rss(source)
        except Exception as exc:  # noqa: BLE001 单源失败不影响整体
            print(f"[ingest] 抓取失败 {source}: {exc}")
    if not sources:
        items += stub_articles()  # 未配置真实源时才用内置样例兜底

    new_count = 0
    for it in items:
        stmt = (
            insert(Article)
            .values(
                url_hash=url_hash(it["url"]),
                url=normalize_url(it["url"]),
                title=it["title"],
                summary=it.get("summary"),
                content=it.get("content"),
                source=it["source"],
                published_at=it.get("published_at"),
                meta=it.get("meta", {}),
            )
            .on_conflict_do_nothing(index_elements=["url_hash"])
            .returning(Article.id)
        )
        new_count += len((await session.execute(stmt)).scalars().all())
    await session.commit()
    return new_count
