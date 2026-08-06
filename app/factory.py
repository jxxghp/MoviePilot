import json
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.apiv2_utils import OPENAPI_V2_PATH, V2ResponseMiddleware
from app.core.config import settings
from app.helper.locale import LocaleHelper
from app.startup.lifecycle import lifespan
from version import APP_VERSION


def _get_http_exception_message(detail: Any) -> str:
    """将 HTTPException 的 detail 转换为统一消息文本。"""
    if isinstance(detail, str) and detail:
        return detail
    if detail is None:
        return "请求失败"
    try:
        return json.dumps(detail, ensure_ascii=False)
    except TypeError:
        return str(detail)


async def localized_http_exception_handler(
        _request: Request,
        exc: HTTPException,
) -> JSONResponse:
    """
    将 HTTPException 响应统一封装为 Response 结构并保留原始错误消息。

    :param _request: 当前 HTTP 请求
    :param exc: FastAPI HTTP 异常
    :return: 统一 JSON 错误响应
    """
    message = _get_http_exception_message(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "message": message,
            "data": {},
        },
        headers=exc.headers,
    )


def create_app() -> FastAPI:
    """
    创建并配置 FastAPI 应用实例。
    """
    _app = FastAPI(
        title=settings.PROJECT_NAME,
        version=APP_VERSION,
        openapi_url=OPENAPI_V2_PATH,
        lifespan=lifespan
    )

    @_app.get(f"{settings.API_V1_STR}/openapi.json", include_in_schema=False)
    def get_v1_openapi_schema() -> dict[str, Any]:
        """保留旧版 OpenAPI 地址并返回当前完整接口文档。"""
        return _app.openapi()

    _app.add_exception_handler(HTTPException, localized_http_exception_handler)

    # 配置 CORS 中间件
    _app.add_middleware(
        CORSMiddleware,  # noqa
        allow_origins=settings.ALLOWED_HOSTS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    _app.add_middleware(V2ResponseMiddleware)

    @_app.middleware("http")
    async def locale_context_middleware(
            request: Request,
            call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """
        为每个请求设置后端多语言上下文。
        """
        token = LocaleHelper.set_current_locale(
            LocaleHelper.get_locale_from_request(request)
        )
        try:
            return await call_next(request)
        finally:
            LocaleHelper.reset_current_locale(token)

    return _app


# 创建 FastAPI 应用实例
app = create_app()
