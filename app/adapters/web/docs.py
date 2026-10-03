"""Swagger UI、ReDoc 与 OpenAPI 文档路由，按运行时开关逐请求决定是否开放。

FastAPI 自带的文档路由只能在创建应用时开启或关闭；这里始终注册路由、每次请求读取开关，
修改设置后无需重启。文档首次生成会常驻数十 MB 内存，关闭后的首个文档请求会丢弃已缓存的文档。
"""

from collections.abc import Callable

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.routing import APIRoute

DOCS_PATH = "/docs"
DOCS_OAUTH2_REDIRECT_PATH = "/docs/oauth2-redirect"
REDOC_PATH = "/redoc"


def install_api_docs_routes(
    app: FastAPI,
    *,
    openapi_path: str,
    enabled: Callable[[], bool],
) -> None:
    """注册文档路由；创建应用时需关闭 FastAPI 自带的同名路由。

    :param app: 需要挂载文档的 FastAPI 应用
    :param openapi_path: OpenAPI JSON 的路径，不含反向代理的 root_path
    :param enabled: 每次请求调用，返回当前是否开放文档
    """

    def ensure_enabled() -> None:
        """未开放时返回 404，并释放开放期间缓存的文档。"""
        if enabled():
            return
        app.openapi_schema = None
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    def root_path(request: Request) -> str:
        """与 FastAPI 自带文档一致，文档页引用的地址带上反向代理前缀。"""
        return str(request.scope.get("root_path", "")).rstrip("/")

    def openapi(_: Request) -> JSONResponse:
        """返回 OpenAPI 文档；首次生成约需数秒 CPU，同步端点由 FastAPI 放进线程池，不阻塞事件循环。"""
        ensure_enabled()
        return JSONResponse(app.openapi())

    async def swagger_ui(request: Request) -> HTMLResponse:
        """返回 Swagger UI 页面。"""
        ensure_enabled()
        prefix = root_path(request)
        return get_swagger_ui_html(
            openapi_url=f"{prefix}{openapi_path}",
            title=f"{app.title} - Swagger UI",
            oauth2_redirect_url=f"{prefix}{DOCS_OAUTH2_REDIRECT_PATH}",
            init_oauth=app.swagger_ui_init_oauth,
            swagger_ui_parameters=app.swagger_ui_parameters,
        )

    async def swagger_ui_redirect(_: Request) -> HTMLResponse:
        """返回 Swagger UI 的 OAuth2 回调页面。"""
        ensure_enabled()
        return get_swagger_ui_oauth2_redirect_html()

    async def redoc(request: Request) -> HTMLResponse:
        """返回 ReDoc 页面。"""
        ensure_enabled()
        return get_redoc_html(
            openapi_url=f"{root_path(request)}{openapi_path}",
            title=f"{app.title} - ReDoc",
        )

    for path, endpoint, response_class in (
        (openapi_path, openapi, JSONResponse),
        (DOCS_PATH, swagger_ui, HTMLResponse),
        (DOCS_OAUTH2_REDIRECT_PATH, swagger_ui_redirect, HTMLResponse),
        (REDOC_PATH, redoc, HTMLResponse),
    ):
        app.router.add_api_route(
            path,
            endpoint,
            methods=["GET"],
            response_class=response_class,
            include_in_schema=False,
            route_class_override=APIRoute,
        )
