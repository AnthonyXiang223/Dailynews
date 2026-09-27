"""FastAPI 应用工厂:挂路由、静态页、调度器生命周期。"""
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from . import api
from .config import get_settings
from .scheduler import install_scheduler, shutdown_scheduler

STATIC_DIR = Path(__file__).parent / "static"


def create_app(start_scheduler: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_scheduler and get_settings().scheduler_enabled:
            await install_scheduler()
        yield
        if start_scheduler:
            await shutdown_scheduler()

    app = FastAPI(title="每日 AI 新闻简报", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def log_slow_requests(request: Request, call_next):
        """慢请求日志:超过 800ms 的 API 请求落日志,用于定位偶发卡顿。"""
        start = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - start) * 1000
        if elapsed_ms > 800 and request.url.path.startswith("/api"):
            from datetime import datetime

            print(
                f"[slow-request] {datetime.now():%H:%M:%S} {request.method} "
                f"{request.url.path} 耗时 {elapsed_ms:.0f}ms"
            )
        return response

    @app.get("/healthz")
    async def healthz():
        """存活探针:不触数据库,永远 200。"""
        return {"status": "ok"}

    app.include_router(api.router, prefix="/api")
    # 静态前端挂在根路径(必须最后挂,避免吞掉 /api)
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


app = create_app()
