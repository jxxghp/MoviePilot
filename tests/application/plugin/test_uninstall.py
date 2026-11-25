"""
插件卸载拒绝与旧分身清理回执的应用边界。
"""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.application import scheduling
from app.application.plugin import folders, management, routes


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
