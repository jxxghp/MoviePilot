import json
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.adapters.observability.otel import build_observation_port
from app.adapters.web.correlation import CorrelationIdMiddleware
from app.adapters.web.health import install_health_routes
from app.adapters.web.metrics import HttpMetricsMiddleware
from app.adapters.web.plugin.routes import FastAPIDynamicRouteRegistry
from app.adapters.web.security.access import (
    configure_token_codec,
    verify_apikey,
    verify_token,
)
from app.api.response import ResponseAPIRoute
from app.application.outbox import PostCommitEffectError
from app.application.plugin.routes import configure_plugin_routes
from app.application.plugin.runtime import get_plugin_manager
from app.application.security.token import create_access_token, decode_access_token
from app.runtime.correlation import get_correlation_id
from app.runtime.errors import public_error_message
from app.runtime.localization import LocaleHelper
from app.runtime.log import configure_correlation_id_provider, logger
from app.runtime.loop import main_loop_registry
from app.runtime.observability import configure_observation
from app.runtime.settings import get_runtime_setting
from app.runtime.version import get_app_version
from app.schemas.exception import (
    PersistenceUnavailableError,
)
from app.schemas.mcp import McpJsonRpcError, McpJsonRpcErrorDetail
from app.schemas.openai import (
    AnthropicErrorDetail,
    AnthropicErrorResponse,
    OpenAIErrorDetail,
    OpenAIErrorResponse,
)
from app.schemas.response import Response as ApiResponse
from app.schemas.response import ValidationIssue
from app.startup.lifecycle import lifespan


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


def _localize_exception_message(request: Request, message: str) -> str:
    """直接按异常所属请求的语言翻译消息，避免中间件上下文已被恢复。"""
    return LocaleHelper.translate_text(
        message,
        locale=LocaleHelper.get_locale_from_request(request),
    )


def _is_mcp_jsonrpc_request(request: Request) -> bool:
    """判断请求是否指向保持原生响应的 MCP JSON-RPC 根端点。"""
    request_path = getattr(getattr(request, "url", None), "path", "")
    return request_path.rstrip("/") == f"{get_runtime_setting('API_V1_STR')}/mcp"


def _get_native_ai_protocol(request: Request) -> str | None:
    """识别需要保持原生错误体的 OpenAI 或 Anthropic 兼容请求。"""
    request_path = getattr(getattr(request, "url", None), "path", "")
    if request_path.startswith(f"{get_runtime_setting('API_V1_STR')}/openai/v1/"):
        return "openai"
    if request_path.startswith(f"{get_runtime_setting('API_V1_STR')}/anthropic/v1/"):
        return "anthropic"
    return None


def _native_ai_error_response(
        protocol: str,
        status_code: int,
        message: str,
        headers: dict[str, str] | None = None,
) -> JSONResponse:
    """按 OpenAI 或 Anthropic 兼容协议构造原生错误响应。"""
    if protocol == "openai":
        error_type = (
            "authentication_error"
            if status_code in {401, 403}
            else "server_error"
            if status_code >= 500
            else "invalid_request_error"
        )
        return JSONResponse(
            status_code=status_code,
            content=OpenAIErrorResponse(
                error=OpenAIErrorDetail(
                    message=message,
                    type=error_type,
                    code=error_type,
                )
            ).model_dump(mode="json"),
            headers=headers,
        )

    error_type = (
        "authentication_error"
        if status_code in {401, 403}
        else "api_error"
        if status_code >= 500
        else "invalid_request_error"
    )
    return JSONResponse(
        status_code=status_code,
        content=AnthropicErrorResponse(
            error=AnthropicErrorDetail(type=error_type, message=message)
        ).model_dump(mode="json"),
        headers=headers,
    )


def _mcp_jsonrpc_error_response(
        status_code: int,
        code: int,
        message: str,
        headers: dict[str, str] | None = None,
) -> JSONResponse:
    """构造带 HTTP 状态码的 MCP JSON-RPC 原生错误响应。"""
    return JSONResponse(
        status_code=status_code,
        content=McpJsonRpcError(
            jsonrpc="2.0",
            id=None,
            error=McpJsonRpcErrorDetail(code=code, message=message),
        ).model_dump(mode="json"),
        headers=headers,
    )


def _protocol_validation_error_response(
        request: Request,
        exc: RequestValidationError,
) -> JSONResponse | None:
    """为 OpenAI 与 Anthropic 兼容端点生成协议原生的参数错误响应。"""
    errors = exc.errors()
    first_error = errors[0] if errors else {}
    location = ".".join(
        str(item)
        for item in first_error.get("loc", ())
        if item not in {"body"}
    )
    message = str(first_error.get("msg") or "Invalid request parameters.")
    native_ai_protocol = _get_native_ai_protocol(request)

    if native_ai_protocol == "openai":
        return JSONResponse(
            status_code=422,
            content=OpenAIErrorResponse(
                error=OpenAIErrorDetail(
                    message=message,
                    type="invalid_request_error",
                    param=location or None,
                    code="invalid_request_error",
                )
            ).model_dump(mode="json"),
        )
    if native_ai_protocol == "anthropic":
        if location:
            message = f"{location}: {message}"
        return JSONResponse(
            status_code=422,
            content=AnthropicErrorResponse(
                error=AnthropicErrorDetail(
                    type="invalid_request_error",
                    message=message,
                )
            ).model_dump(mode="json"),
        )
    if _is_mcp_jsonrpc_request(request):
        if location:
            message = f"{location}: {message}"
        return _mcp_jsonrpc_error_response(
            status_code=422,
            code=-32602,
            message=message,
        )
    return None


async def localized_http_exception_handler(
        request: Request,
        exc: HTTPException,
) -> JSONResponse:
    """
    将 HTTPException 响应统一封装为 Response 结构并隐藏内部实现细节。

    :param request: 当前 HTTP 请求
    :param exc: FastAPI HTTP 异常
    :return: 统一 JSON 错误响应
    """
    message = _localize_exception_message(
        request,
        public_error_message(_get_http_exception_message(exc.detail)),
    )
    native_ai_protocol = _get_native_ai_protocol(request)
    if native_ai_protocol:
        return _native_ai_error_response(
            protocol=native_ai_protocol,
            status_code=exc.status_code,
            message=message,
            headers=exc.headers,
        )
    if _is_mcp_jsonrpc_request(request):
        error_codes = {
            400: -32600,
            401: -32001,
            403: -32001,
            404: -32601,
            409: -32009,
        }
        return _mcp_jsonrpc_error_response(
            status_code=exc.status_code,
            code=error_codes.get(exc.status_code, -32000),
            message=message,
            headers=exc.headers,
        )
    return JSONResponse(
        status_code=exc.status_code,
        content=ApiResponse[None](success=False, message=message).model_dump(mode="json"),
        headers=exc.headers,
    )


async def persistence_unavailable_handler(
        request: Request,
        _exc: PersistenceUnavailableError,
) -> JSONResponse:
    """将持久化能力暂不可用映射为可重试的 503 响应。"""
    return await localized_http_exception_handler(
        request,
        HTTPException(
            status_code=503,
            detail="服务当前繁忙，请稍后重试",
            headers={"Retry-After": "1"},
        ),
    )


async def localized_validation_exception_handler(
        request: Request,
        exc: RequestValidationError,
) -> JSONResponse:
    """
    将请求参数校验错误转换为统一响应并保留结构化错误数据。

    :param request: 当前 HTTP 请求
    :param exc: FastAPI 请求参数校验异常
    :return: 统一 JSON 错误响应
    """
    protocol_response = _protocol_validation_error_response(request, exc)
    if protocol_response is not None:
        return protocol_response

    errors = [
        ValidationIssue(
            location=list(error.get("loc", ())),
            message=str(error.get("msg") or "请求参数错误"),
            error_type=str(error.get("type") or "validation_error"),
        )
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content=ApiResponse[list[ValidationIssue]](
            success=False,
            message=_localize_exception_message(request, "请求参数不正确"),
            data=errors,
        ).model_dump(mode="json"),
    )


async def localized_unhandled_exception_handler(
        request: Request,
        exc: Exception,
) -> JSONResponse:
    """
    将未捕获异常隐藏为统一的服务器错误响应，避免泄露内部细节。

    :param request: 当前 HTTP 请求
    :param exc: 未捕获异常
    :return: 统一 JSON 错误响应
    """
    logger.error(
        f"API 请求发生未捕获异常: {exc}",
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    native_ai_protocol = _get_native_ai_protocol(request)
    if native_ai_protocol:
        return _native_ai_error_response(
            protocol=native_ai_protocol,
            status_code=500,
            message="Internal server error.",
        )
    if _is_mcp_jsonrpc_request(request):
        return _mcp_jsonrpc_error_response(
            status_code=500,
            code=-32603,
            message="Internal error",
        )
    message = (
        str(exc)
        if isinstance(exc, PostCommitEffectError)
        else "未知错误"
    )
    return JSONResponse(
        status_code=500,
        content=ApiResponse[None](
            success=False,
            message=_localize_exception_message(request, message),
        ).model_dump(mode="json"),
    )


def create_app() -> FastAPI:
    """
    创建并配置 FastAPI 应用实例。
    """
    configure_correlation_id_provider(get_correlation_id)
    configure_observation(build_observation_port())
    _app = FastAPI(
        title=get_runtime_setting('PROJECT_NAME'),
        version=get_app_version(),
        openapi_url=f"{get_runtime_setting('API_V1_STR')}/openapi.json",
        lifespan=lifespan
    )

    _app.add_exception_handler(HTTPException, localized_http_exception_handler)
    _app.add_exception_handler(
        PersistenceUnavailableError,
        persistence_unavailable_handler,
    )
    _app.add_exception_handler(
        RequestValidationError,
        localized_validation_exception_handler,
    )
    _app.add_exception_handler(Exception, localized_unhandled_exception_handler)
    # 主程序静态路由统一使用 ResponseAPIRoute；动态插件注册时会显式覆盖为原生 APIRoute。
    _app.router.route_class = ResponseAPIRoute
    # 编排器探针使用原生 APIRoute 和最小响应，不进入业务响应包络或版本前缀。
    install_health_routes(_app)

    # 配置 CORS 中间件
    _app.add_middleware(
        CORSMiddleware,  # noqa
        allow_origins=get_runtime_setting('ALLOWED_HOSTS'),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    _app.add_middleware(CorrelationIdMiddleware)
    _app.add_middleware(HttpMetricsMiddleware)

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

    # HTTP 适配器只持有令牌编解码端口，具体实现由组合根在创建应用时连接。
    configure_token_codec(create_access_token, decode_access_token)

    # 向 application 层插件路由服务注入应用实例，插件 API 的动态注册/移除
    # 统一经服务完成，避免 api.endpoints 反向依赖本模块。
    configure_plugin_routes(FastAPIDynamicRouteRegistry(
        app=_app,
        plugin_ids=lambda: get_plugin_manager().get_running_plugin_ids(),
        plugin_apis=lambda plugin_id: get_plugin_manager().get_plugin_apis(plugin_id),
        verify_token=verify_token,
        verify_apikey=verify_apikey,
        prefix=f"{get_runtime_setting('API_V1_STR')}/plugin",
        protected_routes={
            f"{get_runtime_setting('API_V1_STR')}/openapi.json",
            "/docs",
            "/docs/oauth2-redirect",
            "/redoc",
        },
        log=logger,
        event_loop=lambda: main_loop_registry.current,
    ))

    return _app


# 创建 FastAPI 应用实例；所有组合根装配副作用都在 create_app() 内部完成
app = create_app()
