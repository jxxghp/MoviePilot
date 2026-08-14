from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from fastapi import HTTPException

from app.runtime.extensions.plugin_manager import PluginManager
from app.foundation.singleton import Singleton


@pytest.fixture
def plugin_manager() -> Iterator[PluginManager]:
    """构造隔离的插件管理器实例，避免单例状态污染其它用例。"""
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()
    yield manager
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


def _plugin_with_dashboard(dashboard: Any) -> SimpleNamespace:
    """构造仅包含仪表板接口的插件实例。"""
    return SimpleNamespace(
        plugin_name="演示插件",
        get_render_mode=lambda: ("vue", "dist/assets"),
        get_dashboard=lambda key=None, user_agent=None: dashboard,
    )


def test_plugin_dashboard_keeps_vue_elements_none(plugin_manager: PluginManager) -> None:
    """Vue 仪表板的 elements=None 应原样返回给前端渲染远程组件。"""
    plugin_manager.running_plugins["DemoPlugin"] = _plugin_with_dashboard(
        (
            {"cols": 12},
            {"title": "演示插件", "border": True},
            None,
        )
    )

    dashboard = plugin_manager.get_plugin_dashboard("DemoPlugin", "usage")

    assert dashboard.id == "DemoPlugin"
    assert dashboard.render_mode == "vue"
    assert dashboard.cols == {"cols": 12}
    assert dashboard.attrs == {"title": "演示插件", "border": True}
    assert dashboard.elements is None


def test_plugin_dashboard_returns_none_when_plugin_has_no_dashboard(plugin_manager: PluginManager) -> None:
    """插件声明当前无仪表板时应返回 None，而不是触发解包异常。"""
    plugin_manager.running_plugins["DemoPlugin"] = _plugin_with_dashboard(None)

    assert plugin_manager.get_plugin_dashboard("DemoPlugin", "missing") is None


def test_plugin_dashboard_rejects_invalid_dashboard_shape(plugin_manager: PluginManager) -> None:
    """非空但不符合三元组契约的仪表板数据应返回服务端错误。"""
    plugin_manager.running_plugins["DemoPlugin"] = _plugin_with_dashboard(
        {"cols": {}, "attrs": {}, "elements": []}
    )

    with pytest.raises(HTTPException) as exc_info:
        plugin_manager.get_plugin_dashboard("DemoPlugin", "broken")

    assert exc_info.value.status_code == 500
    assert "仪表盘数据格式错误" in exc_info.value.detail
