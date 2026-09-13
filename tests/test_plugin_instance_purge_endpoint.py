"""插件实例彻底清理端点与其运行时端口测试。"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

from app.api.dependencies.auth import get_current_active_superuser
from app.api.endpoints import plugininstance as plugininstance_endpoint
from app.api.endpoints.plugininstance import purge_plugin_instance
from app.api.routers import API_V1_ROUTER_SPECS
from app.application.plugin.config import PluginPurgeResult
from app.schemas.plugin import PluginInstance, PluginInstancePurgeRequest


class _Command:
    """记录调用参数并回放既定结果的清理用例替身。"""

    def __init__(self, result: PluginPurgeResult) -> None:
        """保存要回放的结果。"""
        self.result = result
        self.calls: list[tuple] = []

    def purge(self, instance_id, scope):
        """记录一次清理请求并回放既定结果。"""
        self.calls.append((instance_id, scope))
        return self.result


def test_endpoint_requires_superuser_dependency() -> None:
    """清理端点要求超级管理员，与卸载、重置同档。"""
    depends = inspect.signature(purge_plugin_instance).parameters["_"].default
    assert depends.dependency is get_current_active_superuser


def test_router_registers_the_purge_path_under_the_plugin_prefix() -> None:
    """路由独立成文件但仍挂在插件前缀下，路径与单文件时期一致。"""
    spec = next(
        item
        for item in API_V1_ROUTER_SPECS
        if item.router is plugininstance_endpoint.router
    )

    assert spec.prefix == "/plugin"
    assert {route.path for route in plugininstance_endpoint.router.routes} == {
        "/instance/{instance_id}/purge",
    }


def test_endpoint_passes_the_requested_scope_through_unchanged() -> None:
    """勾选范围原样转交，服务端不替调用方补默认值。"""
    command = _Command(PluginPurgeResult(True, purged=("config", "plugin_data")))

    response = purge_plugin_instance(
        "DemoPluginWork",
        PluginInstancePurgeRequest(config=True, plugin_data=True),
        None,
        command,
    )

    instance_id, scope = command.calls[0]
    assert instance_id == "DemoPluginWork"
    assert (scope.config, scope.plugin_data) == (True, True)
    assert (scope.own_database, scope.data_directory) == (False, False)
    assert response.success is True
    assert response.data.purged == ["config", "plugin_data"]


def test_endpoint_defaults_every_scope_item_to_false() -> None:
    """请求体不带任何字段时四项全假，落到用例层被拒。"""
    request = PluginInstancePurgeRequest()

    assert (
        request.config,
        request.plugin_data,
        request.own_database,
        request.data_directory,
    ) == (False, False, False, False)


def test_endpoint_reports_a_refused_purge_as_a_failed_response() -> None:
    """用例层拒绝时如实回报失败，不把 HTTP 200 当成清理成功。"""
    command = _Command(PluginPurgeResult(False, "请至少选择一项要清理的内容"))

    response = purge_plugin_instance(
        "DemoPluginWork", PluginInstancePurgeRequest(), None, command
    )

    assert response.success is False
    assert response.message == "请至少选择一项要清理的内容"


def test_endpoint_reports_whether_the_instance_row_was_removed() -> None:
    """分身行随清理消失，端点要把这个事实透出去。"""
    command = _Command(
        PluginPurgeResult(True, purged=("config",), instance_removed=True)
    )

    response = purge_plugin_instance(
        "DemoPluginWork", PluginInstancePurgeRequest(config=True), None, command
    )

    assert response.data.instance_removed is True


# --------------------------------------------------------------------------- #
# 运行时端口：本体的行不得被清理删掉
# --------------------------------------------------------------------------- #


class _Store:
    """按 ID 记录分身行的实例存储替身，本体行不从读取口返回。"""

    def __init__(self) -> None:
        """预置一个分身与一个本体。"""
        self.rows = {
            "DemoPluginWork": PluginInstance(
                instance_id="DemoPluginWork", source_plugin_id="DemoPlugin"
            ),
        }
        self.deleted: list[str] = []

    def get(self, instance_id: str):
        """只返回分身行，本体一律为空。"""
        return self.rows.get(instance_id)

    def delete(self, instance_id: str) -> bool:
        """删除分身行；本体行读不到因而删不掉。"""
        if instance_id not in self.rows:
            return False
        self.deleted.append(instance_id)
        del self.rows[instance_id]
        return True


def _manager_with(store: _Store):
    """拼出一个只装了实例存储与配置存储的 Manager 替身。"""
    from app.runtime.extensions.plugin.manager import PluginManager

    manager = object.__new__(PluginManager)
    manager._plugin_instance_store = store  # noqa: SLF001
    manager._plugin_config_store = SimpleNamespace(  # noqa: SLF001
        delete_data_rows=lambda plugin_id: store.deleted.append(f"rows:{plugin_id}"),
        destroy_database=lambda plugin_id: store.deleted.append(f"db:{plugin_id}"),
    )
    return manager


def test_manager_purges_a_clone_row_but_leaves_the_host_row_alone() -> None:
    """本体那一行承载着插件的启用状态与展示覆盖，清理不得把它删掉。"""
    store = _Store()
    manager = _manager_with(store)

    assert manager.is_plugin_clone("DemoPluginWork") is True
    assert manager.is_plugin_clone("DemoPlugin") is False
    assert manager.purge_plugin_instance("DemoPluginWork") is True
    assert manager.purge_plugin_instance("DemoPlugin") is False
    assert store.deleted == ["DemoPluginWork"]


def test_manager_separates_data_rows_from_the_own_database() -> None:
    """两件事必须能分开做，否则用户没法只删其中一项。"""
    store = _Store()
    manager = _manager_with(store)

    manager.delete_plugin_data_rows("DemoPluginWork")
    manager.destroy_plugin_own_database("DemoPluginWork")

    assert store.deleted == ["rows:DemoPluginWork", "db:DemoPluginWork"]
