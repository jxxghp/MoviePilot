from unittest.mock import MagicMock

from app.application.plugin import folders
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

    assert stored["常用"]["plugins"] == ["OtherPlugin"]
    assert stored["旧目录"] == ["ThirdPlugin"]
    config.get.assert_called_once_with(SystemConfigKey.PluginFolders)
    config.set.assert_called_once_with(SystemConfigKey.PluginFolders, stored)


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
