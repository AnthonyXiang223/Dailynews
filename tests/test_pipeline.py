"""离线全链路测试:URL 去重 → 粗筛打分 → 简报生成 → 正文链接修正。

零 LLM、零 PostgreSQL,只依赖纯函数与未持久化的 ORM 对象。
"""
import uuid
from datetime import date, datetime, timedelta, timezone

from dailynews.models import Subscription
from dailynews.pipeline.compose import compose_digest, rewrite_item_links, sanitize_digest
from dailynews.pipeline.filter import select_candidates
from dailynews.pipeline.ingest import normalize_url, stub_articles, url_hash
from dailynews.profile.deriver import compute_affinity


def _art(**kw) -> dict:
    base = {
        "id": 1,
        "url": "https://example.com/a",
        "title": "",
        "summary": "",
        "content": "",
        "source": "内置样例",
        "published_at": datetime.now(timezone.utc),
    }
    base.update(kw)
    return base


def _sub(**kw) -> Subscription:
    defaults = dict(
        topics=["大模型"],
        keywords=["OpenAI"],
        exclude_keywords=[],
        sources=[],
        tone="concise",
        max_items=10,
    )
    defaults.update(kw)
    return Subscription(**defaults)


def test_normalize_url_and_hash():
    assert normalize_url("HTTPS://Example.com/a/?utm_source=x#sec") == "https://example.com/a"
    # 去重键只看归一化后的 URL:尾部斜杠、大小写、跟踪参数都不影响
    assert url_hash("https://example.com/a") == url_hash("https://example.com/a/")


def test_stub_articles_have_unique_hashes():
    arts = stub_articles()
    assert len(arts) >= 5
    assert len({url_hash(a["url"]) for a in arts}) == len(arts)


def test_select_candidates_filters_and_scores():
    sub = _sub(exclude_keywords=["广告"])
    arts = [
        _art(id=1, title="OpenAI 发布新大模型,推理成本大降", summary="详细介绍新一代大模型"),
        _art(id=2, title="某广告商发布新大模型", summary="含有排除词广告"),          # 排除词命中 → 剔除
        _art(id=3, title="智能体框架更新", summary="与订阅无关"),                    # 不命中 → 剔除
        _art(
            id=4,
            title="OpenAI 开源大模型工具链",
            published_at=datetime.now(timezone.utc) - timedelta(hours=48),  # 旧闻 → 衰减但保留
        ),
    ]
    scored = select_candidates(arts, sub, {})
    ids = [a["id"] for a in scored]
    assert 2 not in ids and 3 not in ids
    assert ids[0] == 1  # 同时命中关键词+主题,得分最高
    assert 4 in ids
    assert scored[0]["score"] > [a["score"] for a in scored if a["id"] == 4][0]


def test_source_whitelist_and_profile_boost():
    sub = _sub(sources=["机器之心"])
    arts = [
        _art(id=1, title="OpenAI 发布新大模型", source="机器之心"),
        _art(id=2, title="OpenAI 的另一条新闻", source="其他来源"),  # 不在白名单 → 剔除
    ]
    scored = select_candidates(arts, sub, {})
    assert [a["id"] for a in scored] == [1]

    # L3 画像加成:大模型主题亲和度高 → 得分提升
    base = select_candidates(arts, _sub(sources=[]), {})[0]["score"]
    boosted = select_candidates(arts, _sub(sources=[]), {"topic_affinity": {"大模型": {"score": 2.0}}})[0]["score"]
    assert boosted > base


def test_compose_links_titles_to_original():
    sub = _sub(max_items=5)
    arts = [
        _art(id=1, title="OpenAI 发布新大模型", url="https://example.com/news/1", summary="新一代模型发布"),
        _art(id=2, title="OpenAI 开源新工具", url="https://example.com/news/2", summary="开源社区关注"),
    ]
    scored = select_candidates(arts, sub, {})
    title, content_md, items = compose_digest("user-1", "张三", sub, scored, uuid.uuid4(), date(2026, 9, 23))

    # 正文以大标题开头会与界面标题/邮件主题重复,故只保留问候语;标题在返回值里
    assert "您好" in content_md
    assert "每日 AI 新闻简报" not in content_md
    assert "每日 AI 新闻简报" in title
    # 标题直链原文(不再经过反馈埋点)
    assert "**[OpenAI 发布新大模型](https://example.com/news/1)**" in content_md
    assert "/api/feedback/" not in content_md
    assert len(items) == 2


def test_compose_empty_candidates():
    sub = _sub()
    title, content_md, items = compose_digest("user-1", "张三", sub, [], uuid.uuid4(), date(2026, 9, 23))
    assert "暂无" in content_md
    assert items == []


def test_sanitize_digest():
    class FakeDigest:
        pass

    fd = FakeDigest()
    fd.items = [{"url": "https://example.com/a", "title": "示例标题"}]
    fd.title = "# 每日 AI 新闻简报 · 2026-09-24"
    fd.content_md = (
        "# 每日 AI 新闻简报 · 2026-09-24\n\n"
        "您好,今日 1 条。\n\n"
        "大模型\n\n"                      # 栏目名(单一主题)→ 删除
        "大模型 × 智能体\n\n"              # 栏目名(主题组合)→ 删除
        "AI 安全与治理\n\n"               # 栏目名(自拟短标题)→ 删除
        "## 1. [示例标题](https://example.com/a)\n\n"
        "https://example.com/a\n\n"
        "摘要内容。\n\n"
        "入选理由:命中大模型主题\n"
        "**- [**另一标题**](https://example.com/a) —— 来源**\n"
    )
    sanitize_digest(fd, topics=["大模型", "智能体"])
    # 正文不再有大标题/栏目名/裸 URL 行/内部字段
    assert "每日 AI 新闻简报 · 2026-09-24" not in fd.content_md
    assert "\n大模型\n" not in fd.content_md
    assert "\n大模型 × 智能体\n" not in fd.content_md
    assert "AI 安全与治理" not in fd.content_md
    assert "\nhttps://example.com/a\n" not in fd.content_md
    assert "入选理由" not in fd.content_md
    # 整行加粗的列表行被还原为普通列表行
    assert "**- [" not in fd.content_md
    assert "\n- [**另一标题**](https://example.com/a) —— 来源" in fd.content_md
    # 标题去掉 markdown # 前缀
    assert fd.title == "每日 AI 新闻简报 · 2026-09-24"
    # 正常内容保留
    assert "您好" in fd.content_md
    assert "摘要内容" in fd.content_md


def test_sanitize_keeps_real_content():
    """短行启发式不能误删正文:带标点的短句、列表行、标题链接都保留。"""
    class FakeDigest:
        pass

    fd = FakeDigest()
    fd.title = "每日 AI 新闻简报 · 2026-09-24"
    fd.items = [{"url": "https://example.com/a", "title": "示例标题"}]
    fd.content_md = (
        "您好,今日为您精选 2 条 AI 要闻:\n\n"
        "- [**示例标题**](https://example.com/a) —— 来源\n"
        "这是摘要。\n"
        "以上就是今日精选的内容。\n"
    )
    sanitize_digest(fd, topics=["大模型"])
    assert "您好" in fd.content_md
    assert "- [**示例标题**]" in fd.content_md
    assert "这是摘要。" in fd.content_md
    assert "以上就是今日精选的内容。" in fd.content_md


def test_deriver_vote_cancel_undoes():
    """点赞→取消点赞:画像亲和度先涨后回落(取消事件以负权重抵消)。"""
    from types import SimpleNamespace

    now = datetime.now(timezone.utc)

    def ev(event_type: str):
        # compute_affinity 只读三个属性,鸭子类型即可
        return SimpleNamespace(topic="大模型", event_type=event_type, created_at=now)

    assert compute_affinity([ev("upvote")])["大模型"]["score"] == 1.0
    both = compute_affinity([ev("upvote"), ev("upvote_cancel")])
    assert both["大模型"]["score"] == 0.0  # 取消后完全回落
    d = compute_affinity([ev("downvote"), ev("downvote_cancel")])
    assert d["大模型"]["score"] == 0.0
    # 切换:upvote(+1) 后 downvote(-1) → 净 0
    s = compute_affinity([ev("upvote"), ev("downvote")])
    assert s["大模型"]["score"] == 0.0


def test_topic_terms_matching():
    """词表匹配:组内词命中标题 > 仅命中正文 > 不命中;压制营销噪音。"""
    sub = _sub(topics=["大模型", "智能体"], keywords=[])
    terms = {
        "大模型": ["大模型", "GPT", "Claude"],
        "智能体": ["智能体", "Agent"],
    }
    arts = [
        _art(id=1, title="GPT-6 发布,推理能力大幅提升"),                      # 词表词命中标题 → 2.0
        _art(id=2, title="某车企新车上市", summary="搭载大模型语音助手"),      # 仅正文命中 → 0.5
        _art(id=3, title="某车企另一款车", summary="智能体座舱全新升级"),      # 仅正文命中 → 0.5
        _art(id=4, title="完全无关的新闻"),                                   # 不命中 → 剔除
    ]
    scored = select_candidates(arts, sub, {}, None, terms)
    ids = [a["id"] for a in scored]
    assert 4 not in ids
    assert scored[0]["id"] == 1  # 标题命中排最前
    assert scored[0]["score"] > scored[1]["score"]

    # 未传词表 → 退化为主题词字面匹配(向后兼容)
    legacy = select_candidates(arts, _sub(topics=["大模型"], keywords=[]), {})
    assert [a["id"] for a in legacy] == [2]  # 只有正文含"大模型"的命中


def test_rewrite_item_links():
    items = [
        {"article_id": 1, "title": "OpenAI 发布新大模型", "url": "https://example.com/a"},
        {"article_id": 2, "title": "DeepSeek 开源新工具", "url": "https://example.com/b"},
    ]

    class FakeDigest:
        pass

    fd = FakeDigest()
    fd.user_id = "user-1"
    fd.items = items
    fd.content_md = ""

    # 情况 1:writer 用了原文 url → 幂等跳过

    fd.content_md = "## 大模型\n\n- **[OpenAI 发布新大模型](https://example.com/a)** —— 来源\n"
    rewrite_item_links(fd)
    assert fd.content_md.count("https://example.com/a") == 1

    # 情况 2:LLM 常写残的空括号链接 [标题]() → 修正为原文直链并清理孤立右括号
    fd.content_md = "## 大模型\n\n**[OpenAI 发布新大模型]()]** —— 来源\n"
    rewrite_item_links(fd)
    assert f"]({items[0]['url']})" in fd.content_md
    assert "]()]" not in fd.content_md

    # 情况 3:只有加粗没有链接 → 补链接
    fd.content_md = "## 开源\n\n**DeepSeek 开源新工具** —— 来源\n"
    rewrite_item_links(fd)
    assert f"]({items[1]['url']})" in fd.content_md

    # 情况 4:裸标题(无加粗无链接)→ 直接包裹
    fd.content_md = "## 开源\n\nDeepSeek 开源新工具 —— 来源\n"
    rewrite_item_links(fd)
    assert f"]({items[1]['url']})" in fd.content_md
