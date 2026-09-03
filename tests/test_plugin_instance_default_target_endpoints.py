"""插件实例默认调用目标设置与清除接口测试。"""

from __future__ import annotations

import inspect

import pytest
from fastapi import HTTPException

from app.api.dependencies.auth import get_current_active_superuser
from app.api.endpoints import pluginversion as pluginversion_endpoint
from app.api.endpoints.pluginversion import (
    clear_plugin_instance_default_target,
    set_plugin_instance_default_target,
)


def _depends_default(func, parameter_name: str):
    """取出端点函数指定参数的 FastAPI Depends 默认值。"""
    return inspect.signature(func).parameters[parameter_name].default


def _manager(**methods):
    """按方法名快速拼装一个鸭子类型的 Manager 替身。"""
    return type("Manager", (), methods)()


def test_both_endpoints_require_superuser_dependency():
    """设为默认与清除默认两个端点都要求超级管理员。"""
    for func in (set_plugin_instance_default_target, clear_plugin_instance_default_target):
        depends = _depends_default(func, "_")
        assert depends.dependency is get_current_active_superuser


# --------------------------------------------------------------------------- #
# PUT /instances/{plugin_id}/{instance_id}/default_target
# --------------------------------------------------------------------------- #


def test_put_delegates_to_manager_and_reports_success(monkeypatch):
    """设置请求原样转交给 Manager，命中时返回成功。"""
    calls: list = []
    manager = _manager(
        set_plugin_instance_default_target=lambda self, *a: (calls.append(a), True)[1]
    )
    monkeypatch.setattr(pluginversion_endpoint, "get_plugin_manager", lambda: manager)

    result = set_plugin_instance_default_target("DemoPlugin", "DemoPluginWork", None)

    assert result.success is True
    assert calls == [("DemoPlugin", "DemoPluginWork")]


def test_put_reports_missing_instance_as_404(monkeypatch):
    """目标实例不归属该插件时，Manager 返回 False，接口须映射为 404。"""
    manager = _manager(set_plugin_instance_default_target=lambda self, *a: False)
    monkeypatch.setattr(pluginversion_endpoint, "get_plugin_manager", lambda: manager)

    with pytest.raises(HTTPException) as excinfo:
        set_plugin_instance_default_target("DemoPlugin", "Missing", None)

    assert excinfo.value.status_code == 404


def test_put_reports_missing_plugin_as_404(monkeypatch):
    """插件本身不存在时返回 404。"""

    def _raise(*_a):
        raise LookupError("插件 Missing 不存在")

    manager = _manager(set_plugin_instance_default_target=_raise)
    monkeypatch.setattr(pluginversion_endpoint, "get_plugin_manager", lambda: manager)

    with pytest.raises(HTTPException) as excinfo:
        set_plugin_instance_default_target("Missing", "Missing", None)

    assert excinfo.value.status_code == 404


# --------------------------------------------------------------------------- #
# DELETE /instances/{plugin_id}/{instance_id}/default_target
# --------------------------------------------------------------------------- #


def test_delete_delegates_to_manager_and_is_idempotent(monkeypatch):
    """清除请求原样转交给 Manager，重复调用同样返回成功。"""
    calls: list = []
    manager = _manager(
        clear_plugin_instance_default_target=lambda self, *a: calls.append(a)
    )
    monkeypatch.setattr(pluginversion_endpoint, "get_plugin_manager", lambda: manager)

    first = clear_plugin_instance_default_target("DemoPlugin", "DemoPluginWork", None)
    second = clear_plugin_instance_default_target("DemoPlugin", "DemoPluginWork", None)

    assert first.success is True
    assert second.success is True
    assert calls == [("DemoPlugin", "DemoPluginWork"), ("DemoPlugin", "DemoPluginWork")]


def test_delete_reports_missing_plugin_as_404(monkeypatch):
    """插件本身不存在时返回 404。"""

    def _raise(*_a):
        raise LookupError("插件 Missing 不存在")

    manager = _manager(clear_plugin_instance_default_target=_raise)
    monkeypatch.setattr(pluginversion_endpoint, "get_plugin_manager", lambda: manager)

    with pytest.raises(HTTPException) as excinfo:
        clear_plugin_instance_default_target("Missing", "Missing", None)

    assert excinfo.value.status_code == 404


def test_router_registers_default_target_paths():
    """路由器暴露设为默认与清除默认两个路径，注册在插件前缀下。"""
    paths = {route.path for route in pluginversion_endpoint.router.routes}
    assert "/instances/{plugin_id}/{instance_id}/default_target" in paths
