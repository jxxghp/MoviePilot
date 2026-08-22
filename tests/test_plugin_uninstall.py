"""插件整体卸载路径的行为契约测试。

覆盖分身端点已下线、卸载端点与 Agent 工具共用同一份管理器实现、卸载不存在的
插件返回恰当错误，以及真实卸载流程的软卸载语义：只回收运行态、注册表与模块
缓存，配置、业务数据、持久化数据目录与源码目录一律保留，重新安装可以读回
原有配置。
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.runtime.extensions.lifecycle import paths as plugin_paths_module
from app.api.endpoints import plugin as plugin_endpoint
from app.db.models.plugindata import PluginData
from app.db.models.pluginconfig import PluginConfig
from app.db.oper.plugindata import PluginDataOper
from app.db.oper.pluginconfig import PluginConfigOper
from app.foundation.singleton import Singleton
from app.sdk.extension import _PluginBase
from app.runtime.extensions import plugin_manager as plugin_manager_module
from app.runtime.extensions.contract.instance import DEFAULT_INSTANCE_ID
from app.runtime.extensions.lifecycle.storage import (
    PluginStorage,
    configure_plugin_storage,
    get_plugin_storage,
)
from app.runtime.extensions.plugin_manager import PluginManager
from app.schemas.types import SystemConfigKey
from app.startup.plugins_initializer import (
    _delete_plugin_instance_config,
    _list_plugin_instance_ids,
    _read_plugin_instance_config,
    _write_plugin_instance_config,
)

PLUGIN_ID = "_UninstallDemoPlugin"


class _UninstallDemoPlugin(_PluginBase):
    """驱动整体卸载测试的最小插件。"""

    plugin_name = "卸载演示"
    plugin_version = "1.0.0"

    def init_plugin(self, config: dict = None):
        """生效配置信息，测试桩不做额外处理。"""

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return bool((self.get_config() or {}).get("enable"))

    def get_api(self) -> List[Dict[str, Any]]:
        """返回空 API 声明。"""
        return []

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回插件配置表单。"""
        return None, {}

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页。"""
        return None

    def stop_service(self):
        """停止插件服务，测试桩无后台服务。"""


@pytest.fixture
def plugin_manager() -> Iterator[PluginManager]:
    """构造隔离的插件管理器实例，避免单例状态污染其它用例。"""
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()
    yield manager
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


@pytest.fixture(autouse=True)
def _restore_plugin_database_ports() -> Iterator[None]:
    """快照并复原插件数据库生命周期端口，避免用例间相互污染。"""
    saved_ensure = plugin_manager_module._plugin_database_ensure
    saved_release = plugin_manager_module._plugin_database_release
    saved_destroy = plugin_manager_module._plugin_database_destroy
    yield
    plugin_manager_module._plugin_database_ensure = saved_ensure
    plugin_manager_module._plugin_database_release = saved_release
    plugin_manager_module._plugin_database_destroy = saved_destroy


@pytest.fixture
def production_plugin_config_storage() -> Iterator[None]:
    """按启动组合根同款接线，把插件配置与数据端口接到真实数据库操作器。"""
    original = get_plugin_storage()
    configure_plugin_storage(PluginStorage(
        read_config=_read_plugin_instance_config,
        write_config=_write_plugin_instance_config,
        delete_config=_delete_plugin_instance_config,
        delete_data=lambda plugin_id: PluginDataOper().del_data(plugin_id),
        list_instances=_list_plugin_instance_ids,
    ))
    yield
    configure_plugin_storage(original)


@pytest.fixture
def plugin_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把插件持久化数据根目录指向隔离的临时目录。"""
    root = tmp_path / "plugin_data"
    root.mkdir()
    monkeypatch.setattr(plugin_paths_module, "settings", SimpleNamespace(PLUGIN_DATA_PATH=root))
    return root


@pytest.fixture
def plugin_source_dir(tmp_path: Path) -> Path:
    """落一份插件源码目录，用于验证卸载后仍然保留在磁盘上。"""
    source_dir = tmp_path / "app" / "plugins" / PLUGIN_ID.lower()
    source_dir.mkdir(parents=True)
    (source_dir / "__init__.py").write_text(
        "class _UninstallDemoPlugin:\n    pass\n", encoding="utf-8"
    )
    return source_dir


def _install_plugin(monkeypatch: pytest.MonkeyPatch, manager: PluginManager) -> None:
    """把测试插件接入管理器的加载路径，数据库钩子替换为安全空操作。"""
    monkeypatch.setattr(
        manager,
        "_load_selective_plugins",
        lambda pid, installed, check: [_UninstallDemoPlugin],
    )
    plugin_manager_module._plugin_database_ensure = lambda _pid, _iid: None
    plugin_manager_module._plugin_database_release = lambda _pid: None
    plugin_manager_module._plugin_database_destroy = lambda _pid, _iid: None


@pytest.fixture
def ready_plugin(
    monkeypatch, plugin_manager, db, plugin_data_root, production_plugin_config_storage,
):
    """安装测试插件并完成一次默认实例启动，返回可直接使用的管理器。"""
    db.watermark(PluginConfig)
    db.watermark(PluginData)
    _install_plugin(monkeypatch, plugin_manager)
    plugin_manager.start(pid=PLUGIN_ID)
    return plugin_manager


def test_uninstall_plugin_preserves_config_data_and_directories(
    ready_plugin, plugin_data_root, plugin_source_dir,
):
    """
    卸载是软卸载：停止运行态、把插件类从注册表回收，并清除其模块缓存，避免
    同名插件重装后复用旧模块；但配置行、数据行、持久化数据目录与源码目录
    一律保留，不做任何删除。
    """
    PluginConfigOper().upsert(PLUGIN_ID, DEFAULT_INSTANCE_ID, {"config_data": {"enable": True}})
    PluginDataOper().save(PLUGIN_ID, "cursor", 1, instance_id=DEFAULT_INSTANCE_ID)
    data_path = ready_plugin._running_plugins[PLUGIN_ID].get_data_path()
    plugin_data_dir = plugin_data_root / PLUGIN_ID
    assert data_path.exists()
    assert plugin_data_dir.exists()
    module_name = f"app.plugins.{PLUGIN_ID.lower()}"
    sys.modules[module_name] = MagicMock()
    sys.modules[f"{module_name}.helper"] = MagicMock()

    ready_plugin.uninstall_plugin(PLUGIN_ID)

    config_row = PluginConfigOper().get(PLUGIN_ID, DEFAULT_INSTANCE_ID)
    assert config_row is not None
    assert config_row.config_data == {"enable": True}
    assert PluginDataOper().get_data(PLUGIN_ID, "cursor", instance_id=DEFAULT_INSTANCE_ID) == 1
    assert plugin_data_dir.exists()
    assert plugin_source_dir.exists()
    assert module_name not in sys.modules
    assert f"{module_name}.helper" not in sys.modules
    assert PLUGIN_ID not in ready_plugin._plugins
    assert PLUGIN_ID not in ready_plugin._running_plugins


def test_uninstall_plugin_then_reinstall_reads_back_original_config(ready_plugin):
    """
    卸载后重新安装（重新触发启动加载）同一插件，能读回卸载前的配置——软卸载
    不清空配置，这正是常见的「卸载重装修问题」操作依赖的语义。
    """
    original_config = {"enable": True, "token": "secret"}
    PluginConfigOper().upsert(PLUGIN_ID, DEFAULT_INSTANCE_ID, {"config_data": original_config})

    ready_plugin.uninstall_plugin(PLUGIN_ID)
    assert ready_plugin.get_plugin_config(PLUGIN_ID) == {}  # 插件已从注册表移除，读取门槛未过

    # 重新安装：等价于把插件重新加入已安装清单后再次触发启动加载
    ready_plugin.start(pid=PLUGIN_ID)

    assert ready_plugin.get_plugin_config(PLUGIN_ID) == original_config


def test_uninstall_plugin_rejects_unknown_plugin(plugin_manager):
    """对不存在的插件执行整体卸载时抛出 LookupError。"""
    with pytest.raises(LookupError):
        plugin_manager.uninstall_plugin("_NoSuchPlugin")


def test_clone_endpoint_route_removed():
    """插件分身创建端点已下线，路由表中不再存在对应路径，处理函数也已删除。"""
    paths = [route.path for route in plugin_endpoint.router.routes]
    assert "/clone/{plugin_id}" not in paths
    assert not hasattr(plugin_endpoint, "clone_plugin")
    assert not hasattr(plugin_endpoint, "_add_clone_to_plugin_folder")


def test_uninstall_plugin_endpoint_maps_unknown_plugin_to_404():
    """插件不存在时卸载端点返回 404，且不触发后续注销步骤。"""
    manager = MagicMock()
    manager.uninstall_plugin.side_effect = LookupError("插件 DemoPlugin 不存在")

    with (
        patch("app.api.endpoints.plugin.PluginManager", return_value=manager),
        patch("app.api.endpoints.plugin.remove_plugin_api") as remove_api,
        patch("app.api.endpoints.plugin.remove_plugin_job") as remove_job,
        patch("app.api.endpoints.plugin.remove_plugin_from_folders") as remove_folders,
    ):
        with pytest.raises(HTTPException) as exc_info:
            plugin_endpoint.uninstall_plugin("DemoPlugin", None)

    assert exc_info.value.status_code == 404
    manager.uninstall_plugin.assert_called_once_with("DemoPlugin")
    remove_api.assert_not_called()
    remove_job.assert_not_called()
    remove_folders.assert_not_called()


def test_uninstall_plugin_endpoint_deregisters_after_manager_succeeds():
    """卸载成功后端点注销已安装登记、动态 API、定时任务与文件夹归属。"""
    manager = MagicMock()
    config_oper = MagicMock()
    config_oper.get.return_value = ["DemoPlugin", "OtherPlugin"]

    with (
        patch("app.api.endpoints.plugin.PluginManager", return_value=manager),
        patch("app.api.endpoints.plugin.SystemConfigOper", return_value=config_oper),
        patch("app.api.endpoints.plugin.remove_plugin_api") as remove_api,
        patch("app.api.endpoints.plugin.remove_plugin_job") as remove_job,
        patch("app.api.endpoints.plugin.remove_plugin_from_folders") as remove_folders,
    ):
        result = plugin_endpoint.uninstall_plugin("DemoPlugin", None)

    assert result.success is True
    manager.uninstall_plugin.assert_called_once_with("DemoPlugin")
    config_oper.set.assert_called_once_with(
        SystemConfigKey.UserInstalledPlugins, ["OtherPlugin"]
    )
    remove_api.assert_called_once_with("DemoPlugin")
    remove_job.assert_called_once_with("DemoPlugin")
    remove_folders.assert_called_once_with("DemoPlugin")


def test_uninstall_plugin_runtime_propagates_unknown_plugin_without_deregistering():
    """Agent 工具路径对不存在的插件同样直接抛错，不触发注销步骤。"""
    from app.agent.tools.impl._plugin_tool_utils import uninstall_plugin_runtime

    manager = MagicMock()
    manager.uninstall_plugin.side_effect = LookupError("插件 DemoPlugin 不存在")

    with (
        patch("app.agent.tools.impl._plugin_tool_utils.PluginManager", return_value=manager),
        patch("app.application.plugin.routes.remove_plugin_api") as remove_api,
        patch("app.application.plugin.folders.remove_plugin_from_folders") as remove_folders,
        patch("app.application.scheduling.remove_plugin_job") as remove_job,
    ):
        with pytest.raises(LookupError):
            asyncio.run(uninstall_plugin_runtime("DemoPlugin"))

    remove_api.assert_not_called()
    remove_job.assert_not_called()
    remove_folders.assert_not_called()


def test_uninstall_plugin_endpoint_and_agent_tool_share_manager_call():
    """
    端点与 Agent 工具的卸载路径都委托给同一个 `PluginManager.uninstall_plugin`
    调用，验证卸载实现已合并、行为一致。
    """
    from app.agent.tools.impl._plugin_tool_utils import uninstall_plugin_runtime

    manager = MagicMock()
    config_oper = MagicMock()
    config_oper.get.return_value = []
    config_oper.async_set = AsyncMock()

    with (
        patch("app.api.endpoints.plugin.PluginManager", return_value=manager),
        patch("app.api.endpoints.plugin.SystemConfigOper", return_value=config_oper),
        patch("app.api.endpoints.plugin.remove_plugin_api"),
        patch("app.api.endpoints.plugin.remove_plugin_job"),
        patch("app.api.endpoints.plugin.remove_plugin_from_folders"),
    ):
        plugin_endpoint.uninstall_plugin("DemoPlugin", None)

    with (
        patch("app.agent.tools.impl._plugin_tool_utils.PluginManager", return_value=manager),
        patch("app.agent.tools.impl._plugin_tool_utils.SystemConfigOper", return_value=config_oper),
        patch("app.application.plugin.routes.remove_plugin_api"),
        patch("app.application.plugin.folders.remove_plugin_from_folders"),
        patch("app.application.scheduling.remove_plugin_job"),
    ):
        asyncio.run(uninstall_plugin_runtime("DemoPlugin"))

    assert manager.uninstall_plugin.call_args_list == [
        (("DemoPlugin",), {}),
        (("DemoPlugin",), {}),
    ]
