"""插件版本查询与实例版本绑定切换接口测试。"""

from __future__ import annotations

import inspect

from app.api.dependencies.auth import get_current_active_superuser
from app.api.endpoints import pluginversion as pluginversion_endpoint
from app.api.endpoints.pluginversion import (
    plugin_version_overview,
    recycle_plugin_versions,
    set_plugin_instance_version,
)
from app.schemas.exception import PluginMutationRejectedError
from app.schemas.plugin import PluginInstanceVersionUpdateRequest


def _depends_default(func, parameter_name: str):
    """取出端点函数指定参数的 FastAPI Depends 默认值。"""
    return inspect.signature(func).parameters[parameter_name].default


def test_all_endpoints_require_superuser_dependency():
    """三个端点都要求超级管理员，不能被低权限用户直接调用。"""
    for func in (plugin_version_overview, set_plugin_instance_version, recycle_plugin_versions):
        depends = _depends_default(func, "_")
        assert depends.dependency is get_current_active_superuser


def test_plugin_version_overview_returns_manager_result(monkeypatch):
    """接口把 Manager 组装好的总览原样透传给调用方。"""
    overview = {
        "plugin_id": "DemoPlugin",
        "current_version": "2.0.0",
        "installed_versions": [],
        "instances": [],
    }
    manager = type(
        "Manager",
        (),
        {"get_plugin_version_overview": lambda self, _plugin_id: overview},
    )()
    monkeypatch.setattr(pluginversion_endpoint, "get_plugin_manager", lambda: manager)

    result = plugin_version_overview("DemoPlugin", None)

    assert result.success is True
    assert result.data == overview


def test_plugin_version_overview_reports_missing_plugin(monkeypatch):
    """插件不存在时返回失败响应，而不是让异常穿透接口。"""

    def _raise(_plugin_id):
        raise LookupError("插件 Missing 不存在")

    manager = type("Manager", (), {"get_plugin_version_overview": lambda self, plugin_id: _raise(plugin_id)})()
    monkeypatch.setattr(pluginversion_endpoint, "get_plugin_manager", lambda: manager)

    result = plugin_version_overview("Missing", None)

    assert result.success is False
    assert "不存在" in result.message


def test_set_plugin_instance_version_rejects_instance_outside_plugin(monkeypatch):
    """目标实例不在该插件的绑定列表中时拒绝切换，不下发到 Manager 层。"""
    overview = {
        "plugin_id": "DemoPlugin",
        "current_version": "1.0.0",
        "installed_versions": [],
        "instances": [{"instance_id": "OtherWork", "plugin_version": None, "follow_current_version": True, "running": False}],
    }
    calls: list = []
    manager = type(
        "Manager",
        (),
        {
            "get_plugin_version_overview": lambda self, _plugin_id: overview,
            "set_plugin_instance_version": lambda self, *a, **kw: calls.append((a, kw)),
        },
    )()
    monkeypatch.setattr(pluginversion_endpoint, "get_plugin_manager", lambda: manager)

    result = set_plugin_instance_version(
        "DemoPlugin",
        "DemoPluginWork",
        PluginInstanceVersionUpdateRequest(follow_current_version=True),
        None,
    )

    assert result.success is False
    assert "不存在" in result.message
    assert calls == []


def test_set_plugin_instance_version_delegates_to_manager_and_reports_success(monkeypatch):
    """已知实例的切换请求原样转交给 Manager，并按其结果返回成功响应。"""
    overview = {
        "plugin_id": "DemoPlugin",
        "current_version": "1.0.0",
        "installed_versions": [],
        "instances": [{"instance_id": "DemoPluginWork", "plugin_version": "1.0.0", "follow_current_version": False, "running": True}],
    }
    calls: list = []

    def _set_version(self, instance_id, *, follow_current_version, plugin_version=None):
        calls.append((instance_id, follow_current_version, plugin_version))
        return True, instance_id

    manager = type(
        "Manager",
        (),
        {
            "get_plugin_version_overview": lambda self, _plugin_id: overview,
            "set_plugin_instance_version": _set_version,
        },
    )()
    monkeypatch.setattr(pluginversion_endpoint, "get_plugin_manager", lambda: manager)

    result = set_plugin_instance_version(
        "DemoPlugin",
        "DemoPluginWork",
        PluginInstanceVersionUpdateRequest(follow_current_version=False, plugin_version="2.0.0"),
        None,
    )

    assert result.success is True
    assert result.message == "版本切换成功"
    assert calls == [("DemoPluginWork", False, "2.0.0")]


def test_set_plugin_instance_version_propagates_manager_failure_message(monkeypatch):
    """Manager 拒绝切换时把可读原因原样返回给调用方。"""
    overview = {
        "plugin_id": "DemoPlugin",
        "current_version": "1.0.0",
        "installed_versions": [],
        "instances": [{"instance_id": "DemoPluginWork", "plugin_version": "1.0.0", "follow_current_version": False, "running": True}],
    }
    manager = type(
        "Manager",
        (),
        {
            "get_plugin_version_overview": lambda self, _plugin_id: overview,
            "set_plugin_instance_version": lambda self, *a, **kw: (
                False,
                "插件 DemoPlugin 未安装版本 9.9.9",
            ),
        },
    )()
    monkeypatch.setattr(pluginversion_endpoint, "get_plugin_manager", lambda: manager)

    result = set_plugin_instance_version(
        "DemoPlugin",
        "DemoPluginWork",
        PluginInstanceVersionUpdateRequest(follow_current_version=False, plugin_version="9.9.9"),
        None,
    )

    assert result.success is False
    assert result.message == "插件 DemoPlugin 未安装版本 9.9.9"


def test_set_plugin_instance_version_reports_missing_plugin(monkeypatch):
    """插件本身不存在时同样返回失败响应，不下发到实例切换逻辑。"""

    def _raise(_plugin_id):
        raise LookupError("插件 Missing 不存在")

    manager = type("Manager", (), {"get_plugin_version_overview": lambda self, plugin_id: _raise(plugin_id)})()
    monkeypatch.setattr(pluginversion_endpoint, "get_plugin_manager", lambda: manager)

    result = set_plugin_instance_version(
        "Missing",
        "MissingWork",
        PluginInstanceVersionUpdateRequest(follow_current_version=True),
        None,
    )

    assert result.success is False
    assert "不存在" in result.message


def test_recycle_plugin_versions_returns_manager_outcome(monkeypatch):
    """接口把 Manager 回收结果原样透传给调用方。"""
    outcome = {"removed": ["1.0.0"], "kept": {"2.0.0": "当前安装版本"}}
    manager = type(
        "Manager",
        (),
        {"recycle_plugin_versions": lambda self, _plugin_id: outcome},
    )()
    monkeypatch.setattr(pluginversion_endpoint, "get_plugin_manager", lambda: manager)

    result = recycle_plugin_versions("DemoPlugin", None)

    assert result.success is True
    assert result.data == outcome


def test_recycle_plugin_versions_reports_missing_plugin(monkeypatch):
    """插件不存在时返回失败响应，而不是让异常穿透接口。"""

    def _raise(_plugin_id):
        raise LookupError("插件 Missing 不存在")

    manager = type("Manager", (), {"recycle_plugin_versions": lambda self, plugin_id: _raise(plugin_id)})()
    monkeypatch.setattr(pluginversion_endpoint, "get_plugin_manager", lambda: manager)

    result = recycle_plugin_versions("Missing", None)

    assert result.success is False
    assert "不存在" in result.message


def test_recycle_plugin_versions_reports_mutation_rejection(monkeypatch):
    """并发窗口拒绝本次回收时返回失败响应，而不是让异常穿透接口。"""

    def _raise(_plugin_id):
        raise PluginMutationRejectedError("插件正在结算，暂不接受回收")

    manager = type("Manager", (), {"recycle_plugin_versions": lambda self, plugin_id: _raise(plugin_id)})()
    monkeypatch.setattr(pluginversion_endpoint, "get_plugin_manager", lambda: manager)

    result = recycle_plugin_versions("DemoPlugin", None)

    assert result.success is False
    assert "暂不接受回收" in result.message


def test_router_registers_all_paths():
    """路由器暴露版本总览、实例切换与回收三个路径，且注册在插件前缀下。"""
    paths = {route.path for route in pluginversion_endpoint.router.routes}
    assert "/versions/{plugin_id}" in paths
    assert "/versions/{plugin_id}/{instance_id}" in paths
    assert "/versions/{plugin_id}/recycle" in paths
