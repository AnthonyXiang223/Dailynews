"""LLM 工厂:Agent 编排与工具内部(如逐条摘要)共用的模型构建。"""
from ..config import get_settings


def build_chat_model():
    """按配置构建 LLM。anthropic 分支适配 DeepSeek 的 Anthropic 兼容端点。

    不设 max_tokens:输出长度交给模型按任务自决,避免长简报被截断。
    """
    settings = get_settings()
    if settings.llm_provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=settings.llm_model,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            temperature=0.3,
        )
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        temperature=0.3,
    )
