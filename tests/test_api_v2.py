from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from app.api.apiv2_utils import (
    OPENAPI_V2_PATH,
    V2ResponseMiddleware,
    configure_v2_openapi,
)
from app.schemas.response import Response


pytestmark = pytest.mark.anyio


@pytest.fixture()
def anyio_backend():
    """使用 asyncio 运行异步接口测试。"""
    return "asyncio"


@pytest.fixture()
def api_app() -> FastAPI:
    """构造同时包含 v1 和 v2 示例接口的测试应用。"""
    app = FastAPI()
    app.add_middleware(V2ResponseMiddleware)

    @app.get("/api/v1/items")
    async def get_v1_items() -> list[dict]:
        """返回未封装的 v1 列表。"""
        return [{"id": 1}]

    @app.get("/api/v2/items")
    async def get_v2_items() -> list[dict]:
        """返回供 v2 适配器封装的列表。"""
        return [{"id": 1}]

    @app.get("/api/v2/wrapped", response_model=Response)
    async def get_wrapped_response() -> Response:
        """返回已经使用通用结构封装的响应。"""
        return Response(success=True, message="操作成功", data={"id": 1})

    @app.get("/api/v2/error")
    async def get_error() -> None:
        """返回供 v2 适配器转换的 HTTP 错误。"""
        raise HTTPException(status_code=400, detail="请求参数错误")

    @app.get("/api/v2/validated/{item_id}")
    async def get_validated_item(item_id: int) -> dict:
        """返回带路径参数校验的示例数据。"""
        return {"id": item_id}

    @app.get("/api/v2/openai/v1/models")
    async def get_openai_models() -> dict:
        """返回需要保持原始协议结构的 OpenAI 模型列表。"""
        return {"object": "list", "data": []}

    @app.get("/api/v2/events")
    async def get_events() -> None:
        """返回不应封装的 SSE 流。"""
        async def event_source():
            """生成一条测试事件。"""
            yield "data: ok\n\n"

        return StreamingResponse(event_source(), media_type="text/event-stream")

    @app.get(OPENAPI_V2_PATH, include_in_schema=False)
    async def get_v2_openapi_schema() -> dict:
        """返回不应被 v2 中间件封装的 OpenAPI 文档。"""
        return {"openapi": "3.1.0", "info": {"title": "Test", "version": "1.0.0"}}

    configure_v2_openapi(app)
    return app


def make_client(app: FastAPI) -> httpx.AsyncClient:
    """创建不访问真实网络的 ASGI 测试客户端。"""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


async def test_v2_wraps_raw_json_without_changing_v1(api_app: FastAPI):
    """v2 应封装原始 JSON 数据，同时保持 v1 返回结构不变。"""
    async with make_client(api_app) as client:
        v1_response = await client.get("/api/v1/items")
        v2_response = await client.get("/api/v2/items")

    assert v1_response.json() == [{"id": 1}]
    assert v2_response.json() == {
        "success": True,
        "message": "",
        "data": [{"id": 1}],
    }


async def test_v2_keeps_existing_response_payload(api_app: FastAPI):
    """已经使用 Response 的接口不应被重复封装。"""
    async with make_client(api_app) as client:
        response = await client.get("/api/v2/wrapped")

    payload = response.json()
    assert payload["success"] is True
    assert payload["message"] == "操作成功"
    assert payload["data"] == {"id": 1}
    assert "success" not in payload["data"]


async def test_v2_moves_http_error_detail_to_message(api_app: FastAPI):
    """v2 HTTP 错误应保留状态码并把 detail 转换到 message。"""
    async with make_client(api_app) as client:
        response = await client.get("/api/v2/error")

    assert response.status_code == 400
    assert response.json() == {
        "success": False,
        "message": "请求参数错误",
        "data": {},
    }


async def test_v2_moves_validation_error_to_message(api_app: FastAPI):
    """v2 参数校验错误也应返回可直接展示的 message。"""
    async with make_client(api_app) as client:
        response = await client.get("/api/v2/validated/not-an-integer")

    payload = response.json()
    assert response.status_code == 422
    assert payload["success"] is False
    assert payload["message"]
    assert payload["data"] == {}


async def test_v2_keeps_protocol_response_unwrapped(api_app: FastAPI):
    """OpenAI 等标准协议接口应保持原始响应结构。"""
    async with make_client(api_app) as client:
        response = await client.get("/api/v2/openai/v1/models")

    assert response.json() == {"object": "list", "data": []}


async def test_v2_keeps_streaming_response_unwrapped(api_app: FastAPI):
    """v2 SSE 等非 JSON 响应应保持原始内容。"""
    async with make_client(api_app) as client:
        response = await client.get("/api/v2/events")

    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text == "data: ok\n\n"


async def test_v2_keeps_openapi_schema_unwrapped(api_app: FastAPI):
    """v2 OpenAPI 文档应保持 Swagger UI 可读取的原始结构。"""
    async with make_client(api_app) as client:
        response = await client.get(OPENAPI_V2_PATH)

    assert response.json() == {
        "openapi": "3.1.0",
        "info": {"title": "Test", "version": "1.0.0"},
    }


def test_v2_openapi_uses_response_schema(api_app: FastAPI):
    """v2 普通 JSON 接口的 OpenAPI 应声明通用 Response 模型。"""
    schema = api_app.openapi()

    raw_schema = schema["paths"]["/api/v2/items"]["get"]["responses"]["200"]
    protocol_schema = schema["paths"]["/api/v2/openai/v1/models"]["get"]
    stream_schema = schema["paths"]["/api/v2/events"]["get"]

    assert raw_schema["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Response"
    }
    assert protocol_schema["responses"]["200"]["content"]["application/json"][
        "schema"
    ] != {"$ref": "#/components/schemas/Response"}
    assert stream_schema["responses"]["200"]["content"]["application/json"].get(
        "schema"
    ) != {"$ref": "#/components/schemas/Response"}
    data_schema = schema["components"]["schemas"]["Response"]["properties"]["data"]
    assert data_schema["title"] == "Data"
    assert {} in data_schema["anyOf"]


def test_response_accepts_scalar_data():
    """通用 Response 的 data 应支持标量接口数据。"""
    response = Response(success=True, data=1)

    assert response.data == 1


def test_v2_router_reuses_v1_route_endpoints():
    """v2 路由应直接复用 v1 的端点函数，避免复制业务实现。"""
    from fastapi.routing import APIRoute

    from app.api.apiv1 import api_router
    from app.api.apiv2 import api_router_v2

    v1_routes = [route for route in api_router.routes if isinstance(route, APIRoute)]
    v2_routes = [route for route in api_router_v2.routes if isinstance(route, APIRoute)]

    assert len(v2_routes) == len(v1_routes)
    assert all(
        v2_route.path == v1_route.path
        and v2_route.methods == v1_route.methods
        and v2_route.endpoint is v1_route.endpoint
        for v1_route, v2_route in zip(v1_routes, v2_routes)
    )


def test_plugin_routes_are_mirrored_to_v2(monkeypatch):
    """插件动态路由注册和移除时应同步维护 v2 路径。"""
    from app.api.endpoints import plugin as plugin_endpoint

    class FakeApp:
        """记录动态注册路径的应用桩。"""

        def __init__(self):
            self.routes = []
            self.openapi_schema = None

        def add_api_route(self, **kwargs):
            """记录新增的路由路径。"""
            self.routes.append(SimpleNamespace(path=kwargs["path"]))

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
    monkeypatch.setattr(plugin_endpoint, "app", fake_app)
    monkeypatch.setattr(plugin_endpoint, "PluginManager", FakePluginManager)

    plugin_endpoint._update_plugin_api_routes("DemoPlugin", action="add")

    assert [route.path for route in fake_app.routes] == [
        "/api/v1/plugin/DemoPlugin/health",
        "/api/v2/plugin/DemoPlugin/health",
    ]

    plugin_endpoint._update_plugin_api_routes("DemoPlugin", action="remove")

    assert fake_app.routes == []
