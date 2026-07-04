import asyncio
import inspect
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from app.api.endpoints import workflow as workflow_endpoint
from app.core.security import verify_token
from app.db.user_oper import (
    get_current_active_manage_user,
    get_current_active_manage_user_async,
    get_current_active_user,
    get_current_active_user_async,
)


def _declared_dependencies(func):
    """读取接口函数签名中直接声明的 FastAPI 依赖函数。"""
    dependencies = []
    for parameter in inspect.signature(func).parameters.values():
        default = parameter.default
        dependency = getattr(default, "dependency", None)
        if dependency:
            dependencies.append(dependency)
    return dependencies


def _workflow_routes():
    """返回 Workflow API 当前注册的所有路由。"""
    return [
        route
        for route in workflow_endpoint.router.routes
        if isinstance(route, APIRoute)
    ]


@pytest.mark.parametrize(
    "user",
    [
        SimpleNamespace(is_superuser=True, permissions={}),
        SimpleNamespace(is_superuser=False, permissions={"manage": True}),
    ],
)
def test_workflow_manage_dependency_allows_superuser_or_manage_user(user):
    """Workflow 管理边界允许超级管理员或拥有 manage 权限的用户。"""
    assert get_current_active_manage_user(current_user=user) is user
    assert asyncio.run(get_current_active_manage_user_async(current_user=user)) is user


@pytest.mark.parametrize("permissions", [None, {}, {"manage": False}])
def test_workflow_manage_dependency_rejects_regular_user(permissions):
    """Workflow 管理边界拒绝不具备 manage 权限的普通用户。"""
    user = SimpleNamespace(is_superuser=False, permissions=permissions)

    with pytest.raises(HTTPException) as sync_exc_info:
        get_current_active_manage_user(current_user=user)
    assert sync_exc_info.value.status_code == 400
    assert sync_exc_info.value.detail == "用户权限不足"

    with pytest.raises(HTTPException) as async_exc_info:
        asyncio.run(get_current_active_manage_user_async(current_user=user))
    assert async_exc_info.value.status_code == 400
    assert async_exc_info.value.detail == "用户权限不足"


def test_workflow_manage_dependencies_reuse_active_user_resolution():
    """Workflow 管理依赖复用激活用户解析，保留未激活用户拒绝策略。"""
    assert _declared_dependencies(get_current_active_manage_user) == [
        get_current_active_user
    ]
    assert _declared_dependencies(get_current_active_manage_user_async) == [
        get_current_active_user_async
    ]


def test_workflow_routes_require_manage_dependency_not_bare_verify_token():
    """Workflow 路由必须使用管理权限依赖，不能直接裸用 verify_token。"""
    routes = _workflow_routes()
    assert routes

    for route in routes:
        dependencies = _declared_dependencies(route.endpoint)
        assert verify_token not in dependencies, route.path
        if inspect.iscoroutinefunction(route.endpoint):
            assert get_current_active_manage_user_async in dependencies, route.path
            assert get_current_active_manage_user not in dependencies, route.path
        else:
            assert get_current_active_manage_user in dependencies, route.path
            assert get_current_active_manage_user_async not in dependencies, route.path
