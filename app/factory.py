from typing import Awaitable, Callable

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.helper.locale import LocaleHelper
from app.startup.lifecycle import lifespan


async def localized_http_exception_handler(
        _request: Request,
        exc: HTTPException,
) -> JSONResponse:
    """
    为 HTTPException 响应补充多语言错误详情。

    :param _request: 当前 HTTP 请求
    :param exc: FastAPI HTTP 异常
    :return: 带 detail_i18n 的 JSON 错误响应
    """
    content = {"detail": exc.detail}
    if isinstance(exc.detail, str):
        content["detail_i18n"] = LocaleHelper.translate_text(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=content,
        headers=exc.headers,
    )


def create_app() -> FastAPI:
    """
    创建并配置 FastAPI 应用实例。
    """
    _app = FastAPI(
        title=settings.PROJECT_NAME,
        openapi_url=f"{settings.API_V1_STR}/openapi.json",
        lifespan=lifespan
    )

    _app.add_exception_handler(HTTPException, localized_http_exception_handler)

    # 配置 CORS 中间件
    _app.add_middleware(
        CORSMiddleware,  # noqa
        allow_origins=settings.ALLOWED_HOSTS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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
