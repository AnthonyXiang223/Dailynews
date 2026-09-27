"""主题词表:主题 → 一组检索词。

匹配逻辑:主题命中 = 词表内任一词命中文章文本(标题命中权重高,正文命中权重低)。
词表存库(topic_terms),LLM 可通过 add_topic_term_tool 自行补充新词,
下次运行自动生效;下方 SEED_TERMS 为初始种子。
"""
from sqlalchemy import select

from ..models import TopicTerm

KNOWN_TOPICS = ["大模型", "智能体", "开源", "芯片", "机器人", "数据安全", "产品", "政策监管", "其他"]

# 初始种子词表(保守:避开过于宽泛的词,如"AI")
SEED_TERMS: dict[str, list[str]] = {
    "大模型": [
        "大模型", "大语言模型", "LLM", "GPT", "Claude", "Gemini", "DeepSeek", "Qwen", "通义",
        "Kimi", "推理模型", "多模态模型", "开源模型", "模型发布", "上下文窗口", "token",
    ],
    "智能体": [
        "智能体", "Agent", "多智能体", "AI 助手", "智能助理", "数字员工", "Agentic", "MCP",
    ],
    "开源": ["开源", "开源社区", "GitHub", "Apache", "开源模型", "开源项目"],
    "芯片": [
        "芯片", "算力", "GPU", "CPU", "英伟达", "NVIDIA", "AMD", "英特尔", "Intel", "高通",
        "骁龙", "制程", "半导体", "HBM", "光刻机",
    ],
    "机器人": ["机器人", "具身智能", "人形机器人", "机器狗", "机械臂"],
    "数据安全": ["数据安全", "隐私", "数据泄露", "合规", "加密", "网络安全", "漏洞", "攻击"],
    "产品": ["发布", "上线", "公测", "内测", "更新"],
    "政策监管": ["监管", "政策", "法案", "条例", "审查", "备案", "反垄断"],
    "其他": [],
}


async def load_topic_terms(session, topics: list[str]) -> dict[str, list[str]]:
    """取指定主题的完整词表 = 库中已补充词 ∪ 种子词(不重复)。"""
    terms: dict[str, set[str]] = {t: set() for t in topics}
    rows = (
        await session.execute(select(TopicTerm).where(TopicTerm.topic.in_(topics)))
    ).scalars().all()
    for row in rows:
        if row.topic in terms:
            terms[row.topic].add(row.term)
    for topic in topics:
        terms[topic].update(t.lower() for t in SEED_TERMS.get(topic, []))
    return {t: sorted(s) for t, s in terms.items()}
