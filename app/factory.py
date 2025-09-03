from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse

from app.core.config import settings
from app.monitoring import setup_prometheus_metrics
from app.startup.lifecycle import lifespan


def create_app() -> FastAPI:
    """
    创建并配置 FastAPI 应用实例。
    """
    _app = FastAPI(
        title=settings.PROJECT_NAME,
        openapi_url=f"{settings.API_V1_STR}/openapi.json",
        lifespan=lifespan,
        default_response_class=ORJSONResponse
    )

    # 配置 CORS 中间件
    _app.add_middleware(
        CORSMiddleware,  # noqa
        allow_origins=settings.ALLOWED_HOSTS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 设置性能监控
    setup_prometheus_metrics(_app)

    return _app


# 创建 FastAPI 应用实例
app = create_app()
