"""应用配置:全部走环境变量 / .env 文件。"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 数据库
    database_url: str = "postgresql+asyncpg://dailynews:dailynews@localhost:5432/dailynews"

    # LLM(OpenAI 兼容,默认 DeepSeek;LLM_API_KEY 留空 = 确定性离线模式)
    llm_provider: str = "openai_compatible"  # openai_compatible | anthropic
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"
    llm_api_key: str = ""

    # 可选:向量嵌入
    embed_api_key: str = ""
    embed_base_url: str = ""
    embed_model: str = ""

    # 内容源与调度
    news_window_hours: int = 24  # 候选时间窗口:每日简报只看最近 24 小时
    news_sources: str = ""  # RSS 源,逗号分隔;留空仅用内置样例
    scheduler_enabled: bool = True

    # 邮件推送(SMTP;未配置 SMTP_PASSWORD 时降级为控制台打印)
    smtp_host: str = "smtp.qq.com"
    smtp_port: int = 465
    smtp_user: str = ""  # 发件邮箱(即 SMTP 登录账号)
    smtp_password: str = ""  # 邮箱授权码(QQ 邮箱:设置→账户→开启 SMTP 后生成),不是登录密码
    smtp_from: str = ""  # 发件人地址,默认同 smtp_user

    # Agent 检查点(需要 checkpointer 扩展,默认关)
    agent_checkpoint: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
