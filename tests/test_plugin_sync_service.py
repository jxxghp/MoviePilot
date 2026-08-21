"""插件市场后台同步用例。"""

from types import SimpleNamespace
from typing import Iterator
from unittest.mock import MagicMock

import pytest

from app.foundation.singleton import Singleton
from app.runtime.extensions.plugin_manager import PluginManager


@pytest.fixture
def plugin_manager() -> Iterator[PluginManager]:
    """构造隔离的插件管理器实例，避免单例状态污染其它用例。"""
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()
    yield manager
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


def test_market_sync_keeps_install_rollback_enabled(
    monkeypatch,
    tmp_path,
    plugin_manager: PluginManager,
) -> None:
    """自动更新插件时保留旧版本，失败后可由安装器恢复。"""
    plugin = SimpleNamespace(
        id="DemoPlugin",
        repo_url="https://example.com/plugins",
        plugin_name="Demo",
        plugin_version="1.0.0",
        system_version_compatible=True,
    )
    install = MagicMock(return_value=(True, ""))
    monkeypatch.setattr(
        "app.runtime.extensions.plugin_manager.settings",
        SimpleNamespace(ROOT_PATH=tmp_path),
    )
    monkeypatch.setattr(
        "app.runtime.extensions.plugin_manager.get_plugin_system",
        lambda: SimpleNamespace(
            is_frozen=lambda: False,
            package=SimpleNamespace(install=install),
        ),
    )
    monkeypatch.setattr(
        "app.runtime.extensions.plugin_manager.get_plugin_storage",
        lambda: SimpleNamespace(read=lambda _key: [plugin.id]),
    )
    monkeypatch.setattr(
        "app.runtime.extensions.plugin_manager.ensure_plugin_version_dir_available",
        lambda _root, _version: "v1_0_0",
    )
    monkeypatch.setattr(
        "app.runtime.extensions.plugin_manager.register_plugin_version",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.runtime.extensions.plugin_manager._plugin_install_reporter",
        MagicMock(),
    )
    monkeypatch.setattr(plugin_manager, "get_online_plugins", lambda: [plugin])
    monkeypatch.setattr(plugin_manager, "get_local_repo_plugins", lambda: [])
    monkeypatch.setattr(
        plugin_manager, "process_plugins_list", lambda higher, _base: list(higher)
    )
    monkeypatch.setattr(
        plugin_manager, "is_plugin_exists", lambda *_args, **_kwargs: False
    )

    assert plugin_manager.sync() == [plugin.id]

    install.assert_called_once_with(
        plugin_id=plugin.id,
        repo_url=plugin.repo_url,
        force_install=False,
        version_dir="v1_0_0",
    )
