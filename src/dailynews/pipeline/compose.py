"""确定性简报生成(离线 fallback)与正文链接修正。

LLM 模式下简报正文由 writer 子代理撰写,但 writer 的 markdown 链接常写残
(空括号、漏写),系统在投递前统一用 rewrite_item_links 修正为原文直链。
"""
import re
import uuid
from datetime import date

from ..models import Digest


def compose_digest(
    user_id: str,
    user_name: str,
    subscription,
    candidates: list[dict],
    digest_id: uuid.UUID,
    digest_date: date,
) -> tuple[str, str, list]:
    """确定性模板:top-N 按主题分组,标题直连原文,生成中文 markdown。"""
    max_items = subscription.max_items or 10
    top = candidates[:max_items]

    lines = [
        f"您好,这是今天为您挑选的 AI 资讯精选,共 {len(top)} 条。",
        "",
    ]

    if not top:
        lines.append("今日暂无与您订阅匹配的新内容,明天再来看看吧。")
    else:
        # 平铺列表,不分栏目:用户要的只是新闻本身
        summary_len = 90
        for a in top:
            lines.append(f"- **[{a['title']}]({a['url']})** —— {a['source']}")
            if a.get("summary"):
                lines.append(f"  > {(a['summary'] or '')[:summary_len]}")
            lines.append("")

    content_md = "\n".join(lines)
    title = f"每日 AI 新闻简报 · {digest_date:%m-%d} · {len(top)} 条精选"
    items = [
        {
            "article_id": a["id"],
            "title": a["title"],
            "url": a["url"],
            "score": a["score"],
            "reason": f"命中关键词 {', '.join(a.get('matched_keywords') or []) or '主题 ' + (a.get('matched_topics') or [''])[0]}",
        }
        for a in top
    ]
    return title, content_md, items


def sanitize_digest(digest: Digest, topics: list[str] | None = None) -> None:
    """投递/生成后清洗 writer 的常见违规输出(提示词约束不住,数据层兜底)。

    处理:
    - 正文开头与简报标题重复的大标题行(界面与邮件主题已显示标题)
    - 独立成行的裸 URL(与条目 url 重复)
    - 「入选理由」等策展内部字段行
    - 栏目/分组标题行(用户要求平铺列表,writer 却总写栏目名):
      ① 仅由订阅主题名组合的行(如「大模型」「大模型 × 智能体」)
      ② 短且无标点的独立行(如「AI 安全与治理」)——正文条目/摘要都带标点或以 - 开头
    - 标题里的 markdown # 前缀
    """
    content = digest.content_md or ""
    item_urls = {item.get("url") for item in (digest.items or []) if item.get("url")}
    topics = [t for t in (topics or []) if t]

    def _is_topic_combo(t: str) -> bool:
        """仅由主题名(以 ×·、/ 与& 连接)组成的行 → 栏目标题。"""
        if not topics or not t:
            return False
        import re as _re

        tokens = [x for x in _re.split(r"[×·、/\s与&]+", t) if x]
        return bool(tokens) and all(x in topics for x in tokens)

    def _is_bare_short_line(t: str) -> bool:
        """短行 + 无标点 + 非列表/链接/引用 → 栏目标题(含自拟栏目名)。"""
        if not t or len(t) > 14:
            return False
        if t.startswith(("- ", "> ", "#", "http", "**")) or any(c in t for c in "。!?;,:"):
            return False
        if "]" in t or "[" in t:
            return False
        return True

    kept: list[str] = []
    for line in content.split("\n"):
        t = line.strip()
        if not kept and t.startswith("# ") and "每日 AI 新闻简报" in t:
            continue  # 开头重复的大标题
        if t.startswith("http") and t.split() and t.split()[0] in item_urls:
            continue  # 裸 URL 行
        if t.startswith("入选理由"):
            continue  # 内部字段
        if _is_topic_combo(t):
            continue  # 栏目名(主题组合)
        if _is_bare_short_line(t):
            continue  # 栏目名(自拟短标题)
        # writer 常把整行列表写成 **- [...] —— 来源**,去掉包裹整行的加粗星号
        if t.startswith("**- "):
            line = line.replace("**- ", "- ", 1)
            line = line.rstrip()
            if line.endswith("**"):
                line = line[:-2].rstrip()
        kept.append(line)

    digest.content_md = "\n".join(kept).strip()
    if digest.title and digest.title.startswith("#"):
        digest.title = digest.title.lstrip("# ").strip()


def rewrite_item_links(digest: Digest) -> None:
    """把简报正文中的文章标题链接统一修正为原文直链。

    幂等;兼容 writer 的多种写法(LLM 常把 markdown 链接写残):
    - [标题](任意url,含空括号 [标题]())→ 整体替换为原文直链
    - **标题** 纯加粗没有链接 → 补上原文直链
    - 裸标题 → 直接包裹为加粗链接
    在投递前调用,保证读者点标题就能打开原文。
    """
    content = digest.content_md or ""
    for item in digest.items or []:
        title = item.get("title")
        url = item.get("url")
        if not title or not url:
            continue
        if f"]({url})" in content:
            continue  # 已是原文直链,幂等跳过
        # 情况 A:标题已带链接(任意 url,含空括号)→ 整体替换
        m = re.search(r"\[([^\]]*" + re.escape(title) + r"[^\]]*)\]\(([^)]*)\)", content)
        if m:
            content = content[: m.start()] + f"[{m.group(1)}]({url})" + content[m.end():]
            continue
        # 情况 B:纯加粗标题 → 补链接
        bold = f"**{title}**"
        if bold in content:
            content = content.replace(bold, f"**[{title}]({url})**")
            continue
        # 情况 C:裸标题 → 直接包裹
        pos = content.find(title)
        if pos >= 0:
            content = content[:pos] + f"**[{title}]({url})**" + content[pos + len(title):]
        # 清理残次写法遗留的孤立右括号,如 **[标题](url)]**
        content = content.replace(f"]({url})]", f"]({url})")
    digest.content_md = content
