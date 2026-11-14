"""
补充插件停止拒绝和本体启用位恢复的真实入口边界。
"""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.application import scheduling
from app.application.plugin import folders, management, routes
from app.runtime.extensions.plugin.manager import PluginManager
from tests.test_plugin_instance_enablement import _host, _store


@pytest.mark.asyncio
async def test_async_uninstall_preserves_membership_and_data_when_stop_is_refused(monkeypatch):
    """
    异步卸载遇到真实停止失败回执时保留成员、配置、实例和数据，允许稍后重试。
    """
    manager = Mock()
    manager.mutation.side_effect = lambda _operation: nullcontext()
    manager.get_plugin_instance.return_value = SimpleNamespace(instance_id="ReviewClone")
    manager.get_plugin_source_instances.return_value = []
    manager.plugins = {}
    manager.stop.return_value = False
    configuration = SimpleNamespace(async_update_atomically=AsyncMock())
    monkeypatch.setattr(management, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(management, "get_configured_system_config", lambda: configuration)
    monkeypatch.setattr(routes, "remove_plugin_api", Mock())
    monkeypatch.setattr(scheduling, "remove_plugin_job", Mock())
    remove_folder = Mock()
    monkeypatch.setattr(folders, "remove_plugin_from_folders", remove_folder)
    with pytest.raises(RuntimeError, match="尚未停止"):
        await management.uninstall_plugin_runtime("ReviewClone")
    manager.stop.assert_called_once_with("ReviewClone")
    configuration.async_update_atomically.assert_not_awaited()
    manager.delete_plugin_config.assert_not_called()
    manager.delete_plugin_data.assert_not_called()
    manager.delete_plugin_instance.assert_not_called()
    manager.remove_plugin.assert_not_called()
    remove_folder.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("package_result", [True, False, RuntimeError("清理旧分身文件失败")])
async def test_legacy_clone_uninstall_reports_actual_package_cleanup(monkeypatch, package_result):
    """
    旧版磁盘分身清理按真实结果回执，拒绝或异常不能误报文件已删除。
    """
    manager = Mock()
    manager.mutation.side_effect = lambda _operation: nullcontext()
    manager.get_plugin_instance.return_value = None
    manager.get_plugin_source_instances.return_value = []
    manager.plugins = {"LegacyClone": SimpleNamespace(is_clone=True)}
    manager.stop.return_value = None
    if isinstance(package_result, Exception):
        manager.remove_plugin_package.side_effect = package_result
    else:
        manager.remove_plugin_package.return_value = package_result
    configuration = SimpleNamespace(async_update_atomically=AsyncMock())
    monkeypatch.setattr(management, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(management, "get_configured_system_config", lambda: configuration)
    monkeypatch.setattr(routes, "remove_plugin_api", Mock())
    monkeypatch.setattr(scheduling, "remove_plugin_job", Mock())
    monkeypatch.setattr(folders, "remove_plugin_from_folders", Mock())
    result = await management.uninstall_plugin_runtime("LegacyClone")
    expected = package_result is True
    assert result == {"was_clone": True, "clone_files_removed": expected}
    configuration.async_update_atomically.assert_awaited_once()
    manager.remove_plugin_package.assert_called_once_with("LegacyClone")
    manager.delete_plugin_config.assert_called_once_with("LegacyClone", force=True)
    manager.delete_plugin_data.assert_called_once_with("LegacyClone", force=True)
    manager.remove_plugin.assert_called_once_with("LegacyClone")


@pytest.mark.parametrize("enabled", [True, False])
def test_manager_restores_host_enabled_without_discarding_its_descriptor(enabled):
    """
    通过真实实例存储恢复冻结启用位，保留已有展示字段且不会为启用操作停止实例。
    """
    host = _host("ReviewHost", enabled=not enabled).model_copy(update={"plugin_name": "保留名称"})
    store, directory = _store({"ReviewHost": host})
    manager = object.__new__(PluginManager)
    manager._plugin_instance_store = store
    manager.stop = Mock(return_value=None)
    assert manager.is_plugin_host_enabled("MissingHost") is False
    assert manager.is_plugin_host_enabled("ReviewHost") is not enabled
    manager.restore_plugin_host_enabled("ReviewHost", enabled)
    assert manager.is_plugin_host_enabled("ReviewHost") is enabled
    assert directory.records["ReviewHost"].plugin_name == "保留名称"
    if enabled:
        manager.stop.assert_not_called()
    else:
        manager.stop.assert_called_once_with("ReviewHost")


def test_manager_does_not_persist_disabled_flag_until_running_owner_stops():
    """
    停止失败时不能先写停用位，原实例及其描述留给下一轮补偿。
    """
    store, directory = _store({"ReviewHost": _host("ReviewHost")})
    manager = object.__new__(PluginManager)
    manager._plugin_instance_store = store
    manager.stop = Mock(return_value=False)
    with pytest.raises(RuntimeError, match="尚未停止"):
        manager.restore_plugin_host_enabled("ReviewHost", False)
    assert manager.is_plugin_host_enabled("ReviewHost") is True
    assert directory.records["ReviewHost"].is_enabled is True
