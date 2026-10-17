"""
面向编排器的最小公开健康探针。
"""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from app.runtime.health import get_application_health


async def liveness() -> JSONResponse:
    """
    确认进程和当前事件循环能够处理请求，不访问任何外部依赖。

    :return: HTTP 200 响应，状态固定为 alive
    """
    return JSONResponse(content={"status": "alive"})


async def readiness(request: Request) -> JSONResponse:
    """
    仅公开是否可以接收流量，不泄露数据库、插件或启动异常细节。

    :param request: 当前请求，用于取得所属应用的就绪状态
    :return: 就绪时返回 HTTP 200 与 ready，否则返回 HTTP 503 与 not_ready
    """
    ready = get_application_health(request.app).is_ready
    return JSONResponse(
        status_code=(
            status.HTTP_200_OK
            if ready
            else status.HTTP_503_SERVICE_UNAVAILABLE
        ),
        content={"status": "ready" if ready else "not_ready"},
    )


def install_health_routes(app: FastAPI) -> None:
    """
    把公开探针装到 API 版本前缀之外，供容器和反向代理使用。

    :param app: 尚未注册这两个探针路由的 FastAPI 应用
    """
    app.router.add_api_route(
        "/health/live",
        liveness,
        methods=["GET"],
        response_class=JSONResponse,
        include_in_schema=False,
        route_class_override=APIRoute,
    )
    app.router.add_api_route(
        "/health/ready",
        readiness,
        methods=["GET"],
        response_class=JSONResponse,
        include_in_schema=False,
        route_class_override=APIRoute,
    )
