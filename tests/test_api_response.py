from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ValidationError
from starlette.responses import Response as StarletteResponse
from starlette.responses import StreamingResponse

from app.api.response import (
    RAW_RESPONSE_OPENAPI_KEY,
    ResponseAPIRoute,
    ResponseAPIRouter,
)
from app.factory import (
    localized_http_exception_handler,
    localized_unhandled_exception_handler,
    localized_validation_exception_handler,
)
from app.runtime.localization import LocaleHelper
from app.schemas.common import JsonData
from app.schemas.response import Response


pytestmark = pytest.mark.anyio


class Item(BaseModel):
    """统一响应测试使用的业务数据模型。"""

    id: int


@pytest.fixture()
def anyio_backend():
    """使用 asyncio 运行异步接口测试。"""
    return "asyncio"


@pytest.fixture()
def api_app() -> FastAPI:
    """构造使用统一响应路由的最小测试应用。"""
    app = FastAPI()
    app.router.route_class = ResponseAPIRoute
    app.add_exception_handler(HTTPException, localized_http_exception_handler)
    from fastapi.exceptions import RequestValidationError

    app.add_exception_handler(
        RequestValidationError,
        localized_validation_exception_handler,
    )
    app.add_exception_handler(Exception, localized_unhandled_exception_handler)

    @app.middleware("http")
    async def locale_middleware(request, call_next):
        """在测试应用中模拟生产环境的请求语言上下文。"""
        token = LocaleHelper.set_current_locale(
            LocaleHelper.get_locale_from_request(request)
        )
        try:
            return await call_next(request)
        finally:
            LocaleHelper.reset_current_locale(token)

    @app.get("/items", response_model=list[Item])
    async def get_items() -> list[Item]:
        """返回需要自动封装的业务数据。"""
        return [Item(id=1)]

    @app.get("/wrapped", response_model=Response[Item])
    async def get_wrapped_response() -> Response[Item]:
        """返回已经封装的响应。"""
        return Response(success=True, message="模块不支持测试", data=Item(id=2))

    @app.get(
        "/oauth-token",
        response_model=Item,
        openapi_extra={RAW_RESPONSE_OPENAPI_KEY: True},
    )
    async def get_oauth_token() -> Item:
        """模拟必须保持顶层字段的标准协议响应。"""
        return Item(id=3)

    @app.get("/error")
    async def get_error() -> None:
        """抛出需要统一处理的 HTTP 错误。"""
        raise HTTPException(status_code=400, detail="用户名或密码错误")

    @app.get("/validated/{item_id}", response_model=Item)
    async def get_validated_item(item_id: int) -> Item:
        """返回带路径参数校验的业务数据。"""
        return Item(id=item_id)

    @app.get("/crash", response_model=Item)
    async def get_crash() -> Item:
        """抛出需要隐藏内部细节的未捕获异常。"""
        raise RuntimeError("private failure detail")

    @app.get("/native", response_model=None)
    async def get_native_response() -> dict[str, bool]:
        """返回显式旁路的原生 JSON 协议。"""
        return {"native": True}

    @app.get("/events", response_model=None)
    async def get_events() -> StreamingResponse:
        """返回不应封装的事件流。"""

        async def event_source():
            """生成一条测试事件。"""
            yield "data: ok\n\n"

        return StreamingResponse(event_source(), media_type="text/event-stream")

    return app


def make_client(app: FastAPI) -> httpx.AsyncClient:
    """创建不访问真实网络的 ASGI 测试客户端。"""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


async def test_route_wraps_data_and_keeps_existing_response(api_app: FastAPI):
    """普通数据应自动封装，已经封装的响应不应重复套壳。"""
    async with make_client(api_app) as client:
        items_response = await client.get("/items")
        wrapped_response = await client.get("/wrapped")

    assert items_response.json() == {
        "success": True,
        "message": "",
        "data": [{"id": 1}],
    }
    assert wrapped_response.json() == {
        "success": True,
        "message": "模块不支持测试",
        "data": {"id": 2},
    }


async def test_explicit_none_and_stream_keep_native_protocol(api_app: FastAPI):
    """显式无响应模型和流式响应应保持原生协议。"""
    async with make_client(api_app) as client:
        native_response = await client.get("/native")
        stream_response = await client.get("/events")

    assert native_response.json() == {"native": True}
    assert stream_response.headers["content-type"].startswith("text/event-stream")
    assert stream_response.text == "data: ok\n\n"


async def test_accept_language_localizes_success_and_http_error(api_app: FastAPI):
    """Accept-Language 应直接决定成功与 HTTP 错误响应的 message。"""
    async with make_client(api_app) as client:
        wrapped_response = await client.get(
            "/wrapped", headers={"Accept-Language": "en-US"}
        )
        error_response = await client.get(
            "/error", headers={"Accept-Language": "en-US"}
        )
        zh_error_response = await client.get(
            "/error", headers={"Accept-Language": "zh-CN"}
        )

    assert wrapped_response.json()["message"] == "Module does not support testing"
    assert error_response.status_code == 400
    assert error_response.json() == {
        "success": False,
        "message": "Incorrect username or password",
        "data": None,
    }
    assert zh_error_response.json()["message"] == "用户名或密码错误"


async def test_validation_error_uses_unified_model(api_app: FastAPI):
    """请求参数校验失败应返回统一协议和明确的错误项结构。"""
    async with make_client(api_app) as client:
        response = await client.get(
            "/validated/not-an-integer",
            headers={"Accept-Language": "en-US"},
        )
        zh_response = await client.get(
            "/validated/not-an-integer",
            headers={"Accept-Language": "zh-CN"},
        )

    payload = response.json()
    assert response.status_code == 422
    assert payload["success"] is False
    assert payload["message"] == "Request parameters are incorrect"
    assert payload["data"] == [
        {
            "location": ["path", "item_id"],
            "message": "Input should be a valid integer, unable to parse string as an integer",
            "error_type": "int_parsing",
        }
    ]
    assert zh_response.json()["message"] == "请求参数不正确"


async def test_unhandled_exception_uses_localized_unified_response(api_app: FastAPI):
    """未捕获异常应返回本地化统一响应且不泄露内部错误。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=api_app,
            raise_app_exceptions=False,
        ),
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            "/crash",
            headers={"Accept-Language": "en-US"},
        )

    assert response.status_code == 500
    assert response.json() == {
        "success": False,
        "message": "Unknown error",
        "data": None,
    }
    assert "private failure detail" not in response.text


def test_openapi_declares_generic_success_and_error_models(api_app: FastAPI):
    """OpenAPI 应展示业务数据类型及统一的 HTTP/422 错误响应结构。"""
    schema = api_app.openapi()
    operation = schema["paths"]["/items"]["get"]
    success_ref = operation["responses"]["200"]["content"]["application/json"][
        "schema"
    ]["$ref"]
    validation_ref = operation["responses"]["422"]["content"][
        "application/json"
    ]["schema"]["$ref"]

    assert success_ref.endswith("/Response_list_Item__")
    assert validation_ref.endswith("/Response_list_ValidationIssue__")
    assert "HTTPValidationError" not in schema["components"]["schemas"]
    success_schema = schema["components"]["schemas"][success_ref.rsplit("/", 1)[-1]]
    assert success_schema["required"] == ["success", "message", "data"]


async def test_openapi_marker_keeps_oauth_payload_at_top_level(api_app: FastAPI):
    """显式原生标记应保留 OAuth 等标准协议的顶层字段及模型。"""
    async with make_client(api_app) as client:
        response = await client.get("/oauth-token")

    operation = api_app.openapi()["paths"]["/oauth-token"]["get"]
    schema_ref = operation["responses"]["200"]["content"]["application/json"][
        "schema"
    ]["$ref"]

    assert response.json() == {"id": 3}
    assert schema_ref.endswith("/Item")
    assert operation[RAW_RESPONSE_OPENAPI_KEY] is True
    validation_ref = operation["responses"]["422"]["content"][
        "application/json"
    ]["schema"]["$ref"]
    assert validation_ref.endswith("/Response_list_ValidationIssue__")


def test_response_localizes_zh_en_and_falls_back_to_source():
    """Response 应支持中英文上下文，未知文案按原文回退。"""
    zh_token = LocaleHelper.set_current_locale("zh-CN")
    try:
        zh_response = Response[None](success=False, message="用户名或密码错误")
    finally:
        LocaleHelper.reset_current_locale(zh_token)

    en_token = LocaleHelper.set_current_locale("en-US")
    try:
        en_response = Response[None](success=False, message="用户名或密码错误")
        fallback_response = Response[None](success=False, message="未登记的新错误文案")
    finally:
        LocaleHelper.reset_current_locale(en_token)

    assert zh_response.message == "用户名或密码错误"
    assert en_response.message == "Incorrect username or password"
    assert fallback_response.message == "未登记的新错误文案"


def test_response_defaults_are_serialized_despite_required_openapi_fields():
    """省略默认值构造仍应在序列化结果中完整输出三块结构。"""
    response = Response[None](success=True)

    assert response.model_dump() == {
        "success": True,
        "message": "",
        "data": None,
    }
    assert Response[None].model_json_schema()["required"] == [
        "success",
        "message",
        "data",
    ]


def test_response_rejects_fields_outside_unified_protocol():
    """统一响应顶层只允许 success、message、data 三个字段。"""
    with pytest.raises(ValidationError):
        Response[None](success=True, message_i18n="unexpected")


def test_v1_routes_use_response_route_except_native_protocols():
    """v1 普通接口应使用统一路由，标准协议路由保持原生实现。"""
    from fastapi.routing import APIRoute

    from app.api.apiv1 import api_router

    api_routes = [
        route for route in api_router.routes if isinstance(route, APIRoute)
    ]
    native_paths = {
        "/openai/v1/models",
        "/openai/v1/chat/completions",
        "/openai/v1/responses",
        "/anthropic/v1/messages",
    }

    assert all(
        isinstance(route, ResponseAPIRoute) or route.path in native_paths
        for route in api_routes
    )


def test_v1_json_routes_have_concrete_data_models():
    """普通 v1 JSON 路由禁止未参数化、Any 或通用 JSON 顶层输出模型。"""
    from fastapi.routing import APIRoute

    from app.api.apiv1 import api_router

    weak_routes = []
    for route in api_router.routes:
        if not isinstance(route, APIRoute):
            continue
        response_model = route.response_model
        try:
            is_response_model = issubclass(response_model, Response)
        except TypeError:
            is_response_model = False
        if not is_response_model:
            continue
        generic_args = response_model.__pydantic_generic_metadata__.get("args")
        if not generic_args or generic_args in ((Any,), (JsonData,)):
            weak_routes.append((route.path, route.name, generic_args))

    assert weak_routes == []


def test_v1_model_free_routes_match_audited_native_allowlist():
    """无响应模型仅允许固定的协议、流、文件、图片、HTML 与 204 路由。"""
    from app.api.apiv1 import api_router

    expected_routes = {
        ("/message/", "incoming_verify"),
        ("/message/agent/file/{file_id}", "download_web_agent_file"),
        ("/message/agent/stream", "web_agent_stream"),
        ("/search/media/{media_id}/stream", "search_by_id_stream"),
        ("/search/title/stream", "search_by_title_stream"),
        ("/search/subtitle/title/stream", "search_subtitle_by_title_stream"),
        (
            "/search/subtitle/media/{media_id}/stream",
            "search_subtitle_by_id_stream",
        ),
        ("/system/img/{proxy}", "proxy_img"),
        ("/system/cache/image", "cache_img"),
        ("/system/progress/{process_type}", "get_progress"),
        ("/system/message", "get_message"),
        ("/system/logging", "get_logging"),
        ("/system/logging/download/{name}", "download_logging"),
        (
            "/llm/provider-auth/callback/{provider_id}",
            "llm_provider_auth_callback",
        ),
        ("/plugin/file/{plugin_id}/{filepath:path}", "plugin_static_file"),
        ("/storage/download", "download"),
        ("/storage/image", "image"),
        ("/mcp", "delete_mcp_session"),
    }
    actual_routes = {
        (route.path, route.name)
        for route in api_router.routes
        if isinstance(route, ResponseAPIRoute) and route.response_model is None
    }

    assert actual_routes == expected_routes


def test_native_protocol_openapi_has_explicit_response_schemas():
    """OpenAI、Anthropic 与 MCP 原生协议响应必须在 OpenAPI 中明确建模。"""
    from app.factory import create_app
    from app.startup.routers_initializer import init_routers

    app = create_app()
    init_routers(app)
    schema = app.openapi()
    operations = {
        ("/api/v1/openai/v1/models", "get"): "OpenAIErrorResponse",
        ("/api/v1/openai/v1/chat/completions", "post"): "OpenAIErrorResponse",
        ("/api/v1/openai/v1/responses", "post"): "OpenAIErrorResponse",
        ("/api/v1/anthropic/v1/messages", "post"): "AnthropicErrorResponse",
    }

    for (path, method), error_model in operations.items():
        success_content = schema["paths"][path][method]["responses"]["200"][
            "content"
        ]
        response_schema = success_content["application/json"]["schema"]
        assert response_schema
        if path.endswith(("/chat/completions", "/messages")):
            assert success_content["text/event-stream"]["schema"] == {
                "type": "string"
            }
        for status_code in ("400", "401", "422", "500", "503"):
            error_schema = schema["paths"][path][method]["responses"][status_code][
                "content"
            ]["application/json"]["schema"]
            assert error_schema["$ref"].endswith(f"/{error_model}")

    mcp_schema = schema["paths"]["/api/v1/mcp"]["post"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]
    assert len(mcp_schema["anyOf"]) == 2
    post_responses = schema["paths"]["/api/v1/mcp"]["post"]["responses"]
    delete_responses = schema["paths"]["/api/v1/mcp"]["delete"]["responses"]
    assert delete_responses["204"] == {"description": "MCP 会话已终止"}
    for status_code in ("400", "401", "403", "404", "409", "422", "500"):
        for responses in (post_responses, delete_responses):
            error_ref = responses[status_code]["content"]["application/json"][
                "schema"
            ]["$ref"]
            assert error_ref.endswith("/McpJsonRpcError")


async def test_native_protocol_validation_errors_keep_native_shapes():
    """OpenAI 与 Anthropic 的请求校验错误应保持各自协议的错误结构。"""
    from app.factory import create_app
    from app.startup.routers_initializer import init_routers

    app = create_app()
    init_routers(app)
    async with make_client(app) as client:
        openai_response = await client.post(
            "/api/v1/openai/v1/chat/completions",
            json={"messages": "invalid"},
        )
        openai_responses_response = await client.post(
            "/api/v1/openai/v1/responses",
            json={},
        )
        anthropic_response = await client.post(
            "/api/v1/anthropic/v1/messages",
            json={"messages": "invalid"},
        )

    assert openai_response.status_code == 422
    assert openai_response.json() == {
        "error": {
            "message": "Input should be a valid list",
            "type": "invalid_request_error",
            "param": "messages",
            "code": "invalid_request_error",
        }
    }
    assert openai_responses_response.status_code == 422
    assert openai_responses_response.json()["error"]["type"] == (
        "invalid_request_error"
    )
    assert openai_responses_response.json()["error"]["param"] == "input"
    assert anthropic_response.status_code == 422
    assert anthropic_response.json() == {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "messages: Input should be a valid list",
        },
    }


async def test_mcp_root_auth_error_keeps_jsonrpc_shape():
    """MCP 根端点的依赖异常应保持 JSON-RPC，REST 子端点仍由统一协议处理。"""
    from app.factory import create_app
    from app.startup.routers_initializer import init_routers

    app = create_app()
    init_routers(app)
    async with make_client(app) as client:
        response = await client.post(
            "/api/v1/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )

    payload = response.json()
    assert response.status_code == 401
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] is None
    assert payload["error"]["code"] == -32001
    assert "success" not in payload


async def test_native_ai_http_and_unhandled_errors_keep_protocol_shapes():
    """兼容协议的依赖异常与未捕获异常都应返回原生错误体。"""
    from starlette.requests import Request

    def request_for(path: str) -> Request:
        """构造直接调用异常处理器所需的最小请求对象。"""
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": path,
                "headers": [],
                "query_string": b"",
                "server": ("testserver", 80),
                "client": ("testclient", 123),
                "scheme": "http",
            }
        )

    openai_http = await localized_http_exception_handler(
        request_for("/api/v1/openai/v1/chat/completions"),
        HTTPException(status_code=401, detail="Invalid bearer token."),
    )
    anthropic_http = await localized_http_exception_handler(
        request_for("/api/v1/anthropic/v1/messages"),
        HTTPException(status_code=403, detail="invalid x-api-key"),
    )
    openai_crash = await localized_unhandled_exception_handler(
        request_for("/api/v1/openai/v1/responses"),
        RuntimeError("private openai failure"),
    )
    anthropic_crash = await localized_unhandled_exception_handler(
        request_for("/api/v1/anthropic/v1/messages"),
        RuntimeError("private anthropic failure"),
    )

    openai_http_payload = openai_http.body.decode()
    anthropic_http_payload = anthropic_http.body.decode()
    openai_crash_payload = openai_crash.body.decode()
    anthropic_crash_payload = anthropic_crash.body.decode()
    assert openai_http.status_code == 401
    assert '"type":"authentication_error"' in openai_http_payload
    assert '"success"' not in openai_http_payload
    assert anthropic_http.status_code == 403
    assert '"type":"authentication_error"' in anthropic_http_payload
    assert '"success"' not in anthropic_http_payload
    assert openai_crash.status_code == 500
    assert '"type":"server_error"' in openai_crash_payload
    assert "private openai failure" not in openai_crash_payload
    assert anthropic_crash.status_code == 500
    assert '"type":"api_error"' in anthropic_crash_payload
    assert "private anthropic failure" not in anthropic_crash_payload


def test_servarr_and_cookiecloud_openapi_has_explicit_models():
    """兼容协议成功响应必须显式建模，错误响应必须声明统一结构。"""
    from app.factory import create_app
    from app.startup.routers_initializer import init_routers

    app = create_app()
    init_routers(app)
    schema = app.openapi()
    compatible_paths = [
        path
        for path in schema["paths"]
        if path.startswith(("/api/v3", "/cookiecloud"))
    ]

    assert compatible_paths
    for path in compatible_paths:
        for operation in schema["paths"][path].values():
            success_response = operation["responses"].get("200")
            if success_response:
                success_schemas = [
                    content["schema"]
                    for content in success_response.get("content", {}).values()
                ]
                assert success_schemas and all(success_schemas)
            for status_code in ("400", "401", "403", "404", "409", "422", "500"):
                error_response = operation["responses"][status_code]
                error_schema = next(iter(error_response["content"].values()))[
                    "schema"
                ]
                assert error_schema["$ref"].startswith(
                    "#/components/schemas/Response_"
                )


def test_all_openapi_error_responses_use_json_schemas():
    """所有普通与原生协议错误响应都应在文档中声明 JSON 媒体类型和结构。"""
    from app.factory import create_app
    from app.startup.routers_initializer import init_routers

    app = create_app()
    init_routers(app)
    schema = app.openapi()
    methods = {"get", "post", "put", "patch", "delete", "options", "head"}

    invalid_responses = []
    for path, path_item in schema["paths"].items():
        for method, operation in path_item.items():
            if method not in methods:
                continue
            for status_code, response in operation["responses"].items():
                if not str(status_code).startswith(("4", "5")):
                    continue
                json_schema = response.get("content", {}).get(
                    "application/json", {}
                ).get("schema")
                if not json_schema:
                    invalid_responses.append((path, method, status_code))

    assert invalid_responses == []


def test_openapi_success_models_have_no_implicit_empty_nested_schemas():
    """2xx 响应可达模型不得包含裸 Any、裸数组或未声明值类型的开放映射。"""
    from app.factory import create_app
    from app.startup.routers_initializer import init_routers

    app = create_app()
    init_routers(app)
    schema = app.openapi()
    components = schema["components"]["schemas"]
    allowed_open_components = {
        # 三个外部兼容协议允许规范声明之外的请求扩展字段。
        "AnthropicMessage",
        "AnthropicMessagesRequest",
        "OpenAIChatCompletionsRequest",
        "OpenAIChatMessage",
        "OpenAIResponsesRequest",
        # 分类规则与 CookieCloud 解密载荷按设计接受扩展键。
        "CategoryRule",
        "CookieDecryptedPayload",
        # 通用管理请求的 params 按设计透传模块个性化参数。
        "ManageRequest",
        # 通用管理响应的 data 为模块自定义结构，按设计不固定字段。
        "Response_Dict_str__Any__",
        # LLM 提供商管理响应的 data 目录查询为列表、其余动作为映射。
        "Response_Union_List_Dict_str__Any____Dict_str__Any___",
    }
    allowed_empty_components = {"McpJsonRpcEmptyResult"}
    violations = []

    def visit(node: Any, component: str, location: str) -> None:
        """递归检查单个组件节点中的隐式弱类型。"""
        if not isinstance(node, dict):
            return
        if node == {} and component not in allowed_empty_components:
            violations.append((component, location, "empty"))
        if (
            node.get("type") == "array"
            and "items" not in node
            and "prefixItems" not in node
        ):
            violations.append((component, location, "untyped-array"))
        if (
            node.get("additionalProperties") is True
            and component not in allowed_open_components
        ):
            violations.append((component, location, "open-object"))
        if (
            node.get("type") == "object"
            and not node.get("properties")
            and "additionalProperties" not in node
            and component not in allowed_empty_components
        ):
            violations.append((component, location, "untyped-object"))
        for key, value in node.items():
            if key in {"default", "example", "examples"}:
                continue
            if isinstance(value, dict):
                visit(value, component, f"{location}.{key}")
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    visit(item, component, f"{location}.{key}[{index}]")

    for component_name, component_schema in components.items():
        visit(component_schema, component_name, component_name)

    assert violations == []


def test_plugin_routes_only_register_v1(monkeypatch):
    """插件动态路由只注册 v1 地址，并显式绕过主程序响应路由。"""
    from app.application import plugins

    class FakeApp:
        """记录动态注册路径的应用桩。"""

        def __init__(self):
            self.routes = []
            self.route_options = []
            self.openapi_schema = None
            self.router = self

        def add_api_route(self, **kwargs):
            """记录新增的路由路径。"""
            self.routes.append(SimpleNamespace(path=kwargs["path"]))
            self.route_options.append(kwargs)

        def setup(self):
            """模拟 FastAPI 路由重建。"""

    class FakePluginManager:
        """返回单个测试插件 API 的管理器桩。"""

        def get_plugin_apis(self, plugin_id):
            """返回测试插件 API。"""
            assert plugin_id == "DemoPlugin"
            return [
                {
                    "path": "/DemoPlugin/health",
                    "endpoint": lambda: {"ok": True},
                    "methods": ["GET"],
                }
            ]

    fake_app = FakeApp()
    monkeypatch.setattr(plugins, "_api_app", fake_app)
    monkeypatch.setattr(plugins, "PluginManager", FakePluginManager)

    plugins._update_plugin_api_routes("DemoPlugin", action="add")
    assert [route.path for route in fake_app.routes] == [
        "/api/v1/plugin/DemoPlugin/health"
    ]
    assert fake_app.route_options[0]["route_class_override"] is APIRoute

    plugins._update_plugin_api_routes("DemoPlugin", action="remove")
    assert fake_app.routes == []


def test_response_router_uses_response_route_class():
    """统一路由器应默认创建统一响应路由。"""
    router = ResponseAPIRouter()

    @router.get("/health", response_model=bool)
    def health() -> bool:
        """返回测试健康状态。"""
        return True

    assert isinstance(router.routes[0], ResponseAPIRoute)


def test_dynamic_host_route_without_annotation_uses_recursive_json_model():
    """主应用动态路由未声明模型时仍应使用统一响应模型。"""
    app = FastAPI()
    app.router.route_class = ResponseAPIRoute

    def plugin_endpoint():
        """模拟未声明返回注解的插件动态接口。"""
        return {"ok": True}

    app.add_api_route("/plugin", plugin_endpoint, methods=["GET"])
    route = app.routes[-1]
    generic_args = route.response_model.__pydantic_generic_metadata__["args"]

    assert generic_args == (JsonData,)


def build_plugin_api_app(monkeypatch) -> FastAPI:
    """构造覆盖插件自由返回类型的动态路由测试应用。"""
    from app.application import plugins

    class PluginPayload(BaseModel):
        """插件自行声明的响应模型。"""

        ok: bool

    def dict_endpoint() -> dict[str, bool]:
        """返回插件自定义字典。"""
        return {"ok": True}

    def model_endpoint() -> PluginPayload:
        """返回插件自定义模型。"""
        return PluginPayload(ok=True)

    def response_endpoint() -> JSONResponse:
        """返回插件自定义状态码和响应头。"""
        return JSONResponse(
            {"accepted": True},
            status_code=202,
            headers={"X-Plugin-Response": "yes"},
        )

    async def stream_endpoint() -> StreamingResponse:
        """返回插件自定义事件流。"""
        async def stream_source():
            """生成插件测试事件。"""
            yield "data: plugin\n\n"

        return StreamingResponse(stream_source(), media_type="text/event-stream")

    def empty_endpoint() -> StarletteResponse:
        """返回插件自定义空响应。"""
        return StarletteResponse(status_code=204)

    class FakePluginManager:
        """返回覆盖插件响应边界的路由声明。"""

        def get_plugin_apis(self, plugin_id):
            """返回测试插件 API。"""
            assert plugin_id == "DemoPlugin"
            common = {"methods": ["GET"], "allow_anonymous": True}
            return [
                {
                    **common,
                    "path": "/DemoPlugin/dict",
                    "endpoint": dict_endpoint,
                },
                {
                    **common,
                    "path": "/DemoPlugin/model",
                    "endpoint": model_endpoint,
                    "response_model": PluginPayload,
                },
                {
                    **common,
                    "path": "/DemoPlugin/response",
                    "endpoint": response_endpoint,
                },
                {
                    **common,
                    "path": "/DemoPlugin/stream",
                    "endpoint": stream_endpoint,
                    "response_model": None,
                },
                {
                    **common,
                    "path": "/DemoPlugin/empty",
                    "endpoint": empty_endpoint,
                    "status_code": 204,
                    "response_model": None,
                },
            ]

    app = FastAPI()
    app.router.route_class = ResponseAPIRoute
    monkeypatch.setattr(plugins, "_api_app", app)
    monkeypatch.setattr(plugins, "PluginManager", FakePluginManager)
    plugins._update_plugin_api_routes("DemoPlugin", action="add")
    return app


async def test_plugin_dynamic_routes_preserve_raw_runtime_responses(monkeypatch):
    """插件动态 API 应完整保留自行选择的响应体、状态码和流。"""
    app = build_plugin_api_app(monkeypatch)

    async with make_client(app) as client:
        dict_response = await client.get("/api/v1/plugin/DemoPlugin/dict")
        model_response = await client.get("/api/v1/plugin/DemoPlugin/model")
        native_response = await client.get("/api/v1/plugin/DemoPlugin/response")
        stream_response = await client.get("/api/v1/plugin/DemoPlugin/stream")
        empty_response = await client.get("/api/v1/plugin/DemoPlugin/empty")

    assert dict_response.json() == {"ok": True}
    assert model_response.json() == {"ok": True}
    assert native_response.status_code == 202
    assert native_response.json() == {"accepted": True}
    assert native_response.headers["X-Plugin-Response"] == "yes"
    assert stream_response.text == "data: plugin\n\n"
    assert empty_response.status_code == 204
    assert empty_response.content == b""


def test_plugin_dynamic_routes_keep_plugin_openapi_model_raw(monkeypatch):
    """插件声明的模型应直接进入 OpenAPI，不得套入主程序 Response。"""
    app = build_plugin_api_app(monkeypatch)

    operation = app.openapi()["paths"]["/api/v1/plugin/DemoPlugin/model"]["get"]
    response_schema = operation["responses"]["200"]["content"][
        "application/json"
    ]["schema"]

    assert response_schema == {
        "$ref": "#/components/schemas/PluginPayload",
    }


def test_dynamic_bare_response_uses_recursive_json_without_double_wrapping():
    """动态插件声明裸 Response 时应补齐递归 JSON 类型且不重复封装。"""
    router = ResponseAPIRouter()

    @router.get("/plugin", response_model=Response)
    def plugin_endpoint() -> Response:
        """模拟返回统一响应但未参数化 data 的插件接口。"""
        return Response(success=True, data={"ok": True})

    route = router.routes[0]
    generic_args = route.response_model.__pydantic_generic_metadata__["args"]
    result = route.endpoint()

    assert generic_args == (JsonData,)
    assert isinstance(result, Response)
    assert result.data == {"ok": True}
