"""测试夹具:关闭调度器与 LLM,保证离线、无副作用。"""
import os

# 必须在导入 dailynews 之前设置环境(覆盖 .env 文件)
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://dailynews:dailynews@localhost:5432/dailynews")
os.environ.setdefault("LLM_API_KEY", "")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from dailynews.app import create_app  # noqa: E402


@pytest.fixture()
def client():
    app = create_app(start_scheduler=False)
    with TestClient(app) as c:
        yield c
