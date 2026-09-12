"""插件实例日志等级查询与设置接口测试。"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.api.dependencies.auth import get_current_active_superuser
from app.api.endpoints import pluginversion as pluginversion_endpoint
from app.api.endpoints.pluginversion import (
    clear_plugin_instance_log_level,
    plugin_instance_log_levels,
    set_plugin_instance_log_level,
)
from app.schemas.plugin import PluginInstanceLogLevelUpdateRequest


def _depends_default(func, parameter_name: str):
    """取出端点函数指定参数的 FastAPI Depends 默认值。"""
    return inspect.signature(func).parameters[parameter_name].default


def _manager(**methods):
    """按方法名快速拼装一个鸭子类型的 Manager 替身。"""
    return type("Manager", (), methods)()


def test_all_endpoints_require_superuser_dependency():
    """三个日志等级端点都要求超级管理员，不能被低权限用户直接调用。"""
    for func in (
        plugin_instance_log_levels,
        set_plugin_instance_log_level,
        clear_plugin_instance_log_level,
    ):
        depends = _depends_default(func, "_")
        assert depends.dependency is get_current_active_superuser


def test_get_returns_manager_levels_wrapped_in_plugin_overview(monkeypatch):
    """查询接口把 Manager 组装好的等级列表包进插件级总览对象，而不是裸列表。

    响应模型直接裸露列表会被宿主分页契约门禁判定为集合接口、要求声明分页参数；
    插件实例数量始终很小，参照 plugin.versions.get 包一层 plugin_id + instances。
    """
    levels = [
        {
            "instance_id": "DemoPlugin",
            "configured_level": None,
            "expires_at": None,
            "effective_level": "INFO",
        },
        {
            "instance_id": "DemoPluginWork",
            "configured_level": "DEBUG",
            "expires_at": None,
            "effective_level": "DEBUG",
        },
    ]
    manager = _manager(get_plugin_instance_log_levels=lambda self, _plugin_id: levels)
    monkeypatch.setattr(pluginversion_endpoint, "get_plugin_manager", lambda: manager)

    result = plugin_instance_log_levels("DemoPlugin", None)

    assert result.success is True
    assert result.data == {"plugin_id": "DemoPlugin", "instances": levels}


def test_get_reports_missing_plugin_as_404(monkeypatch):
    """插件不存在时查询接口返回 404，而不是让异常穿透或吞掉。"""

    def _raise(_plugin_id):
        raise LookupError("插件 Missing 不存在")

    manager = _manager(
        get_plugin_instance_log_levels=lambda self, plugin_id: _raise(plugin_id)
    )
    monkeypatch.setattr(pluginversion_endpoint, "get_plugin_manager", lambda: manager)

    with pytest.raises(HTTPException) as excinfo:
        plugin_instance_log_levels("Missing", None)

    assert excinfo.value.status_code == 404


def test_put_delegates_to_manager_and_reports_success(monkeypatch):
    """设置请求原样转交给 Manager，携带等级与失效时间。"""
    calls: list = []
    manager = _manager(
        set_plugin_instance_log_level=lambda self, *a, **kw: calls.append((a, kw))
    )
    monkeypatch.setattr(pluginversion_endpoint, "get_plugin_manager", lambda: manager)
    expires_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

    result = set_plugin_instance_log_level(
        "DemoPlugin",
        "DemoPluginWork",
        PluginInstanceLogLevelUpdateRequest(level="DEBUG", expires_at=expires_at),
        None,
    )

    assert result.success is True
    assert calls == [(("DemoPlugin", "DemoPluginWork", "DEBUG", expires_at), {})]


def test_put_reports_missing_plugin_or_instance_as_404(monkeypatch):
    """未知插件或实例时设置接口返回 404。"""

    def _raise(*_a, **_kw):
        raise LookupError("插件实例 Missing 不存在")

    manager = _manager(set_plugin_instance_log_level=_raise)
    monkeypatch.setattr(pluginversion_endpoint, "get_plugin_manager", lambda: manager)

    with pytest.raises(HTTPException) as excinfo:
        set_plugin_instance_log_level(
            "DemoPlugin",
            "Missing",
            PluginInstanceLogLevelUpdateRequest(level="DEBUG"),
            None,
        )

    assert excinfo.value.status_code == 404


def test_put_reports_invalid_level_as_400(monkeypatch):
    """非法等级时设置接口返回 400。"""

    def _raise(*_a, **_kw):
        raise ValueError("不支持的日志等级：LOUD")

    manager = _manager(set_plugin_instance_log_level=_raise)
    monkeypatch.setattr(pluginversion_endpoint, "get_plugin_manager", lambda: manager)

    with pytest.raises(HTTPException) as excinfo:
        set_plugin_instance_log_level(
            "DemoPlugin",
            "DemoPlugin",
            PluginInstanceLogLevelUpdateRequest(level="LOUD"),
            None,
        )

    assert excinfo.value.status_code == 400


def test_delete_delegates_to_manager_and_is_idempotent(monkeypatch):
    """清除请求原样转交给 Manager，重复调用同样返回成功。"""
    calls: list = []
    manager = _manager(
        clear_plugin_instance_log_level=lambda self, *a: calls.append(a)
    )
    monkeypatch.setattr(pluginversion_endpoint, "get_plugin_manager", lambda: manager)

    first = clear_plugin_instance_log_level("DemoPlugin", "DemoPluginWork", None)
    second = clear_plugin_instance_log_level("DemoPlugin", "DemoPluginWork", None)

    assert first.success is True
    assert second.success is True
    assert calls == [("DemoPlugin", "DemoPluginWork"), ("DemoPlugin", "DemoPluginWork")]


def test_delete_reports_missing_plugin_or_instance_as_404(monkeypatch):
    """未知插件或实例时清除接口返回 404。"""

    def _raise(*_a):
        raise LookupError("插件实例 Missing 不存在")

    manager = _manager(clear_plugin_instance_log_level=_raise)
    monkeypatch.setattr(pluginversion_endpoint, "get_plugin_manager", lambda: manager)

    with pytest.raises(HTTPException) as excinfo:
        clear_plugin_instance_log_level("DemoPlugin", "Missing", None)

    assert excinfo.value.status_code == 404
