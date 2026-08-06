import json
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse

from app.schemas.response import Response


API_V2_STR = "/api/v2"
OPENAPI_V2_PATH = f"{API_V2_STR}/openapi.json"
_PROTOCOL_PREFIXES = ("/openai", "/anthropic", "/mcp")
_JSON_CONTENT_TYPES = ("application/json", "+json")


def _is_protocol_path(path: str) -> bool:
    """判断路径是否属于需要保留原始协议响应的接口。"""
    relative_path = path.removeprefix(API_V2_STR)
    return any(
        relative_path == prefix or relative_path.startswith(f"{prefix}/")
        for prefix in _PROTOCOL_PREFIXES
    )


def _is_json_response(response: StarletteResponse) -> bool:
    """判断响应是否为可安全解析的 JSON 响应。"""
    content_type = response.headers.get("content-type", "").split(";", 1)[0]
    return any(
        content_type == accepted_type or content_type.endswith(accepted_type)
        for accepted_type in _JSON_CONTENT_TYPES
    )


def _is_response_payload(payload: Any) -> bool:
    """判断响应内容是否已经符合通用 Response 结构。"""
    return isinstance(payload, dict) and {
        "success",
        "message",
        "data",
    }.issubset(payload)


def _get_error_message(payload: Any) -> str:
    """从旧版错误响应中提取统一的错误消息。"""
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail:
            return detail
        if isinstance(detail, list):
            messages = [
                item.get("msg")
                for item in detail
                if isinstance(item, dict) and isinstance(item.get("msg"), str)
            ]
            if messages:
                return "; ".join(messages)
        if detail is not None:
            return json.dumps(detail, ensure_ascii=False)
        message = payload.get("message")
        if isinstance(message, str) and message:
            return message
    if isinstance(payload, str) and payload:
        return payload
    return "请求失败"


def _copy_response_headers(source: StarletteResponse, target: StarletteResponse) -> None:
    """复制适配前响应中仍然有效的头信息。"""
    for key, value in source.raw_headers:
        if key.lower() not in {b"content-length", b"content-type"}:
            target.raw_headers.append((key, value))


def _restore_response_body(
    source: StarletteResponse,
    body: bytes,
) -> StarletteResponse:
    """在检查响应体后恢复原始响应内容和头信息。"""
    restored_response = StarletteResponse(
        content=body,
        status_code=source.status_code,
        background=source.background,
    )
    restored_response.raw_headers = list(source.raw_headers)
    return restored_response


class V2ResponseMiddleware(BaseHTTPMiddleware):
    """
    为 v2 REST 接口适配统一的 Response 响应结构。

    已经返回项目 Response 模型的成功响应保持原样，避免改变既有接口语义；
    OpenAI、Anthropic 和 MCP 协议接口也保持原始协议响应。
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[StarletteResponse]],
    ) -> StarletteResponse:
        """处理 v2 请求并在必要时封装 JSON 响应。"""
        response = await call_next(request)
        if not request.url.path.startswith(f"{API_V2_STR}/"):
            return response
        if request.url.path == OPENAPI_V2_PATH:
            return response
        if _is_protocol_path(request.url.path):
            return response
        if response.status_code in {204, 304} or not _is_json_response(response):
            return response
        if response.headers.get("content-encoding"):
            return response

        route = request.scope.get("route")
        route_response_model = getattr(route, "response_model", None)
        if response.status_code < 400 and route_response_model is Response:
            return response

        body = b"".join([chunk async for chunk in response.body_iterator])
        if not body:
            return _restore_response_body(response, body)
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            return _restore_response_body(response, body)

        if _is_response_payload(payload):
            return _restore_response_body(response, body)

        if response.status_code >= 400:
            content = {
                "success": False,
                "message": _get_error_message(payload),
                "data": {},
            }
            if isinstance(payload, dict) and isinstance(payload.get("detail_i18n"), str):
                content["message_i18n"] = payload["detail_i18n"]
        else:
            content = {
                "success": True,
                "message": "",
                "data": payload,
            }

        wrapped_response = JSONResponse(
            content=content,
            status_code=response.status_code,
            background=response.background,
        )
        _copy_response_headers(response, wrapped_response)
        return wrapped_response


def configure_v2_openapi(app: FastAPI) -> None:
    """
    将 v2 普通 JSON 接口的 OpenAPI 响应模型改为通用 Response。

    :param app: 已完成 v1/v2 路由注册的 FastAPI 应用
    """
    if getattr(app, "_v2_openapi_configured", False):
        return

    original_openapi = app.openapi

    def custom_openapi() -> dict[str, Any]:
        """生成包含 v2 通用响应模型的 OpenAPI 文档。"""
        schema = original_openapi()
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        components["Response"] = Response.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )

        route_map = {
            (route.path, method.lower()): route
            for route in app.routes
            if isinstance(route, APIRoute)
            for method in route.methods
        }
        response_ref = {"$ref": "#/components/schemas/Response"}
        for path, path_item in schema.get("paths", {}).items():
            if not path.startswith(f"{API_V2_STR}/"):
                continue
            for method, operation in path_item.items():
                if method not in {
                    "get",
                    "post",
                    "put",
                    "patch",
                    "delete",
                    "options",
                    "head",
                }:
                    continue
                route = route_map.get((path, method))
                if (
                    route is None
                    or route.response_model is None
                    or route.response_model is Any
                    or route.response_model is Response
                    or _is_protocol_path(path)
                ):
                    continue
                if route.status_code in {204, 304}:
                    continue
                content_type = getattr(route.response_class, "media_type", None)
                if content_type and not (
                    content_type == "application/json" or content_type.endswith("+json")
                ):
                    continue
                status_code = str(route.status_code or 200)
                response = operation.get("responses", {}).get(status_code)
                if response and "content" in response:
                    json_content = response["content"].get("application/json")
                    if json_content is not None:
                        json_content["schema"] = response_ref

        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi
    app._v2_openapi_configured = True
