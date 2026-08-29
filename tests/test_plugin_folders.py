import asyncio
from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.plugin import folders
from app.schemas.exception import PersistenceUnavailableError
from app.schemas.types import SystemConfigKey


def test_remove_plugin_from_folders_updates_current_and_legacy_shapes(monkeypatch):
    """卸载插件时应同时清理字典格式和旧列表格式的文件夹引用。"""
    stored = {
        "常用": {"plugins": ["DemoPlugin", "OtherPlugin"], "order": 1},
        "旧目录": ["DemoPlugin", "ThirdPlugin"],
    }
    config = MagicMock()
    config.get.return_value = stored
    monkeypatch.setattr(folders, "get_configured_system_config", lambda: config)

    folders.remove_plugin_from_folders("DemoPlugin")

    config.get.assert_called_once_with(SystemConfigKey.PluginFolders)
    written = config.set.call_args.args[1]
    assert written["常用"]["plugins"] == ["OtherPlugin"]
    assert written["旧目录"] == ["ThirdPlugin"]
    assert stored["常用"]["plugins"] == ["DemoPlugin", "OtherPlugin"]
    config.set.assert_called_once_with(SystemConfigKey.PluginFolders, written)


def test_remove_plugin_from_folders_skips_write_when_plugin_is_absent(monkeypatch):
    """插件不在任何文件夹时不得产生无意义配置写入。"""
    config = MagicMock()
    config.get.return_value = {"常用": {"plugins": ["OtherPlugin"]}}
    monkeypatch.setattr(folders, "get_configured_system_config", lambda: config)

    folders.remove_plugin_from_folders("DemoPlugin")

    config.set.assert_not_called()


def test_remove_plugin_from_folders_does_not_block_uninstall_on_config_error(
    monkeypatch,
):
    """文件夹配置读取失败时只记录错误，不应阻断插件卸载主流程。"""
    config = MagicMock()
    config.get.side_effect = RuntimeError("broken folders")
    error = MagicMock()
    monkeypatch.setattr(folders, "get_configured_system_config", lambda: config)
    monkeypatch.setattr(folders.logger, "error", error)

    folders.remove_plugin_from_folders("DemoPlugin")

    error.assert_called_once()


def test_save_folder_maps_ordinary_error_to_failure_result():
    """普通持久化失败应保持旧 HTTP 语义，由结果表达而不是抛出 500。"""
    service = folders.PluginFolderService(
        read=lambda: {},
        write=AsyncMock(side_effect=RuntimeError("write failed")),
        write_sync=MagicMock(),
        mutation=lambda _operation: nullcontext(),
    )

    result = asyncio.run(service.save({"常用": []}))

    assert result.success is False
    assert result.message == "write failed"


def test_get_folder_maps_read_error_to_empty_snapshot():
    """文件夹读取失败应保持旧接口的空对象兜底语义。"""
    service = folders.PluginFolderService(
        read=MagicMock(side_effect=RuntimeError("read failed")),
        write=AsyncMock(),
        write_sync=MagicMock(),
        mutation=lambda _operation: nullcontext(),
    )

    assert service.get_or_empty() == {}


def test_save_folder_propagates_persistence_unavailable():
    """持久化基础设施不可用必须继续交给统一异常处理器映射。"""
    service = folders.PluginFolderService(
        read=lambda: {},
        write=AsyncMock(side_effect=PersistenceUnavailableError()),
        write_sync=MagicMock(),
        mutation=lambda _operation: nullcontext(),
    )

    with pytest.raises(PersistenceUnavailableError):
        asyncio.run(service.save({"常用": []}))
