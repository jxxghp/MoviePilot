from typing import Any

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute

from app.api.deps import get_current_active_user_async
from app.runtime.config import settings


def _v1_routes(app: FastAPI) -> list[Any]:
    """返回最终应用中可执行的 v1 API 路由上下文。"""
    routes: list[Any] = []
    for route in app.routes:
        effective_route_contexts = getattr(route, "effective_route_contexts", None)
        if callable(effective_route_contexts):
            routes.extend(effective_route_contexts())
        elif isinstance(route, APIRoute):
            routes.append(route)
    return [
        route
        for route in routes
        if route.path.startswith(f"{settings.API_V1_STR}/")
    ]


def _route_contract(route: APIRoute) -> tuple[Any, ...]:
    """提取直接聚合前后必须保持一致的公开路由合同。"""
    return (
        type(route),
        route.path,
        tuple(sorted(route.methods or ())),
        route.name,
        route.endpoint,
        tuple(route.tags),
        route.status_code,
        route.response_model,
        route.response_class,
        route.responses,
        tuple(
            (
                dependency.dependency,
                dependency.use_cache,
                tuple(getattr(dependency, "scopes", ()) or ()),
                getattr(dependency, "scope", None),
            )
            for dependency in route.dependencies
        ),
        route.operation_id,
        route.unique_id,
        route.include_in_schema,
        route.deprecated,
    )


def test_init_routers_directly_includes_endpoint_router_specs(monkeypatch):
    """启动聚合应直接 include 原始端点路由器并一次性附加完整 v1 前缀。"""
    from app.api.routers import API_V1_ROUTER_SPECS
    from app.startup.initializers.routers import init_routers

    app = FastAPI()
    include_calls = []
    original_include_router = app.include_router

    def record_include_router(router, **kwargs):
        """记录启动聚合参数后继续执行 FastAPI 的公开 include 接口。"""
        include_calls.append((router, kwargs.get("prefix"), kwargs.get("tags")))
        return original_include_router(router, **kwargs)

    monkeypatch.setattr(app, "include_router", record_include_router)

    init_routers(app)

    v1_calls = include_calls[: len(API_V1_ROUTER_SPECS)]
    assert [router for router, _, _ in v1_calls] == [
        spec.router for spec in API_V1_ROUTER_SPECS
    ]
    assert [prefix for _, prefix, _ in v1_calls] == [
        f"{settings.API_V1_STR}{spec.prefix}" for spec in API_V1_ROUTER_SPECS
    ]
    assert [tuple(tags or ()) for _, _, tags in v1_calls] == [
        spec.tags for spec in API_V1_ROUTER_SPECS
    ]
    assert [prefix for _, prefix, _ in include_calls[-2:]] == [
        "/api/v3",
        "/cookiecloud",
    ]


def test_direct_v1_routes_and_openapi_match_compatibility_router():
    """最终应用的 v1 路由合同与 OpenAPI 应和兼容聚合结果完全一致。"""
    from app.api.apiv1 import api_router
    from app.startup.initializers.routers import init_routers

    compatibility_app = FastAPI()
    compatibility_app.include_router(api_router, prefix=settings.API_V1_STR)
    direct_app = FastAPI()
    init_routers(direct_app)

    compatibility_routes = _v1_routes(compatibility_app)
    direct_routes = _v1_routes(direct_app)

    assert compatibility_routes
    assert direct_routes
    assert [_route_contract(route) for route in direct_routes] == [
        _route_contract(route) for route in compatibility_routes
    ]
    assert all(
        route.dependency_overrides_provider is direct_app for route in direct_routes
    )
    assert all(
        route.dependency_overrides_provider is compatibility_app
        for route in compatibility_routes
    )
    direct_v1_paths = {
        path: item
        for path, item in direct_app.openapi()["paths"].items()
        if path.startswith(f"{settings.API_V1_STR}/")
    }
    assert direct_v1_paths == compatibility_app.openapi()["paths"]


@pytest.mark.anyio
async def test_direct_routes_honor_application_dependency_overrides():
    """直接聚合后的路由仍应由最终 FastAPI 应用解析依赖覆盖。"""
    from app.startup.initializers.routers import init_routers

    app = FastAPI()
    init_routers(app)
    app.dependency_overrides[get_current_active_user_async] = lambda: object()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get(f"{settings.API_V1_STR}/system/ping")

    assert response.status_code == 200, response.text
    assert response.json() == {"success": True, "message": "", "data": None}


def test_compatibility_api_router_keeps_public_contract():
    """历史导出应继续提供无 v1 根前缀的标准 APIRouter 与固定路由集合。"""
    from app.api.apiv1 import api_router
    from app.api.routers import API_V1_ROUTER_SPECS

    assert type(api_router) is APIRouter
    app = FastAPI()
    app.include_router(api_router, prefix=settings.API_V1_STR)
    paths = set(app.openapi()["paths"])
    expected_paths = {
        f"{settings.API_V1_STR}{spec.prefix}{route.path}"
        for spec in API_V1_ROUTER_SPECS
        for route in spec.router.routes
        if (
            isinstance(route, APIRoute)
            and route.include_in_schema
            and ":path}" not in route.path
        )
    }
    assert expected_paths <= paths


def test_split_endpoint_routes_are_flattened_into_parent_routers():
    """拆分端点合并后父路由只应暴露可执行路由，避免测试和工具遇到包装节点。"""
    from app.api.endpoints import plugin, subscribe, system

    for endpoint in (plugin, subscribe, system):
        assert endpoint.router.routes
        assert all(isinstance(route, APIRoute) for route in endpoint.router.routes)


def test_init_routers_accepts_composition_root_api_prefix():
    """路由初始化应使用组合根传入的 API 前缀。"""
    from app.startup.initializers.routers import init_routers

    app = FastAPI()
    init_routers(app, "/custom/v1")

    assert "/custom/v1/system/ping" in app.openapi()["paths"]
