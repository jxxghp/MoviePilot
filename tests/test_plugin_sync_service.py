"""插件市场同步服务用例。"""

from types import SimpleNamespace
from unittest.mock import Mock

from app.runtime.extensions.plugin.sync import PluginSyncService


def test_market_sync_keeps_install_rollback_enabled() -> None:
    """自动更新插件时保留旧版本，失败后可由安装器恢复。"""
    plugin = SimpleNamespace(
        id="DemoPlugin",
        repo_url="https://example.com/plugins",
        plugin_name="Demo",
        plugin_version="1.0.0",
        system_version_compatible=True,
    )
    install = Mock(return_value=(True, ""))
    service = PluginSyncService(
        frozen=lambda: False,
        installed_plugins=lambda: [plugin.id],
        online_plugins=lambda: [plugin],
        local_plugins=lambda: [],
        merge_plugins=lambda items, *_args: items,
        plugin_exists=lambda *_args: False,
        install=install,
        report=Mock(),
        log=Mock(),
    )

    assert service.sync() == [plugin.id]
    install.assert_called_once_with(plugin.id, plugin.repo_url, False)
