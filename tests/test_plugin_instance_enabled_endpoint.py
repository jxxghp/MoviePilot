"""插件实例启停接口与依赖分类装载过滤测试。"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

from app.api.dependencies.auth import get_current_active_superuser
from app.api.endpoints import plugintarget as plugintarget_endpoint
from app.api.endpoints.plugintarget import set_plugin_instance_enabled
from app.runtime.extensions.plugin.dependency import PluginDependencyService
from app.schemas.exception import PluginMutationRejectedError
from app.schemas.plugin import PluginInstance, PluginInstanceEnabledRequest


def _manager(**methods):
    """按方法名快速拼装一个鸭子类型的 Manager 替身。"""
    return type("Manager", (), methods)()


def test_endpoint_requires_superuser_dependency():
    """启停端点要求超级管理员。"""
    depends = inspect.signature(set_plugin_instance_enabled).parameters["_"].default
    assert depends.dependency is get_current_active_superuser


def test_endpoint_delegates_to_manager_and_reports_success(monkeypatch):
    """请求原样转交给 Manager，状态确实变化时返回成功。"""
    calls: list = []
    manager = _manager(
        set_plugin_instance_enabled=lambda self, *a: (calls.append(a), True)[1]
    )
    monkeypatch.setattr(plugintarget_endpoint, "get_plugin_manager", lambda: manager)

    result = set_plugin_instance_enabled(
        "DemoPluginWork", PluginInstanceEnabledRequest(enabled=False), None
    )

    assert result.success is True
    assert calls == [("DemoPluginWork", False)]


def test_endpoint_reports_unchanged_state_without_pretending_success(monkeypatch):
    """实例不存在或已处于目标状态时如实回报失败，并给出完整句子而非中文片段。"""
    manager = _manager(set_plugin_instance_enabled=lambda self, *a: False)
    monkeypatch.setattr(plugintarget_endpoint, "get_plugin_manager", lambda: manager)

    disabled = set_plugin_instance_enabled(
        "Missing", PluginInstanceEnabledRequest(enabled=False), None
    )
    enabled = set_plugin_instance_enabled(
        "Missing", PluginInstanceEnabledRequest(enabled=True), None
    )

    assert disabled.success is False
    assert disabled.message == "实例 Missing 不存在或已处于停用状态"
    assert enabled.message == "实例 Missing 不存在或已处于启用状态"


def test_endpoint_reports_mutation_rejection_as_a_failed_response(monkeypatch):
    """处于停机准入窗口时返回失败响应而不是抛出。"""

    def _raise(*_a):
        raise PluginMutationRejectedError("插件正在停机")

    manager = _manager(set_plugin_instance_enabled=_raise)
    monkeypatch.setattr(plugintarget_endpoint, "get_plugin_manager", lambda: manager)

    result = set_plugin_instance_enabled(
        "DemoPluginWork", PluginInstanceEnabledRequest(enabled=True), None
    )

    assert result.success is False
    assert "插件正在停机" in result.message


def test_router_registers_the_enabled_path():
    """路由器暴露启停路径，注册在插件前缀下。"""
    paths = {route.path for route in plugintarget_endpoint.router.routes}
    assert "/instance/{instance_id}/enabled" in paths


# --------------------------------------------------------------------------- #
# 依赖分类必须按装载判据过滤
# --------------------------------------------------------------------------- #


def test_classification_drops_hosts_that_are_not_loadable():
    """分类结果会被逐个 start()，停用的本体必须在这里就被剔除。

    物理插件那一层按安装清单划分，而安装清单只回答「包在不在磁盘上」；不过滤的话
    停用的插件会在开机与配置热重载时被重新拉起来，启用位形同虚设。
    """
    system = SimpleNamespace(
        dependency=SimpleNamespace(
            classify_plugins=lambda: (
                ["PluginA", "PluginB"],
                ["PluginC"],
                ["PluginD"],
            )
        )
    )
    service = PluginDependencyService(
        system=lambda: system,
        instances=lambda: {
            "PluginAx2": PluginInstance(
                instance_id="PluginAx2", source_plugin_id="PluginA"
            )
        },
        loadable_hosts=lambda: {"PluginA"},
        log=SimpleNamespace(info=lambda *_a: None, error=lambda *_a: None),
    )

    classification = service.classify_plugins()

    assert classification.ready == ("PluginA", "PluginAx2")
    assert classification.missing_dependencies == ()
    assert classification.missing_source == ()


def test_classification_without_a_loadable_port_keeps_every_plugin():
    """未装配装载判据端口时保持原分类，测试与旧调用方不因此丢掉插件。"""
    system = SimpleNamespace(
        dependency=SimpleNamespace(
            classify_plugins=lambda: (["PluginA"], ["PluginC"], ["PluginD"])
        )
    )
    service = PluginDependencyService(
        system=lambda: system,
        log=SimpleNamespace(info=lambda *_a: None, error=lambda *_a: None),
    )

    classification = service.classify_plugins()

    assert classification.ready == ("PluginA",)
    assert classification.missing_dependencies == ("PluginC",)
    assert classification.missing_source == ("PluginD",)
