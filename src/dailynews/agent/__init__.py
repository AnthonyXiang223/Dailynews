"""每日简报 Agent 入口:deepagents 编排(LLM 模式)+ 确定性 fallback。"""
from .llm import build_chat_model
from .run import run_daily_digest

__all__ = ["build_chat_model", "run_daily_digest"]
