from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest
from packaging.version import Version

from app.core.plugin import PluginManager
from app.helper.plugin import PluginHelper
from app.schemas.types import SystemConfigKey
from app.utils.singleton import Singleton


@pytest.fixture
def plugin_manager() -> Iterator[PluginManager]:
    """构造隔离的插件管理器实例，避免单例状态污染其它用例。"""
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()
    yield manager
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


def _build_local_plugin_repo(tmp_path: Path) -> tuple[Path, Path]:
    """构造带系统版本要求的本地 v2 插件仓库。"""
    repo_path = tmp_path / "local-plugins"
    source_dir = repo_path / "plugins.v2" / "demoplugin"
    source_file = source_dir / "__init__.py"
    source_dir.mkdir(parents=True)
    source_file.write_text(
        "from app.plugins import _PluginBase\n"
        "class DemoPlugin(_PluginBase):\n"
        "    plugin_name = 'Demo'\n",
        encoding="utf-8",
    )
    (repo_path / "package.v2.json").write_text(
        '{"DemoPlugin": {"version": "1.0.0", "system_version": ">=2.13.11"}}',
        encoding="utf-8",
    )
    return repo_path, source_file


def test_dev_local_plugin_candidate_keeps_hot_sync_allowed_when_system_version_lags(
    tmp_path,
    monkeypatch,
    plugin_manager: PluginManager,
) -> None:
    """DEV 本地源码候选保留热同步资格，系统版本差异只作为兼容性提示。"""
    repo_path, source_file = _build_local_plugin_repo(tmp_path)
    runtime_dir = tmp_path / "app" / "plugins" / "demoplugin"

    monkeypatch.setattr("app.core.plugin.settings", SimpleNamespace(DEV=True, ROOT_PATH=tmp_path))
    monkeypatch.setattr("app.helper.plugin.settings.PLUGIN_LOCAL_REPO_PATHS", str(repo_path))
    monkeypatch.setattr(PluginHelper, "get_current_system_version", lambda: Version("2.13.10"))
    monkeypatch.setattr(
        "app.core.plugin.SystemConfigOper.get",
        lambda _self, key: ["DemoPlugin"] if key == SystemConfigKey.UserInstalledPlugins else None,
    )

    candidate = plugin_manager._get_local_plugin_candidate_from_path(source_file)

    assert candidate["system_version_compatible"] is False
    assert candidate.get("compatible") is not False
    assert plugin_manager._sync_local_plugin_if_installed("DemoPlugin", candidate)
    assert (runtime_dir / "__init__.py").read_text(encoding="utf-8") == source_file.read_text(encoding="utf-8")


def test_local_plugin_candidate_keeps_system_version_gate_outside_dev(
    tmp_path,
    monkeypatch,
    plugin_manager: PluginManager,
) -> None:
    """非 DEV 本地候选继续受主系统版本门禁保护，避免自动热加载绕过安装约束。"""
    repo_path, source_file = _build_local_plugin_repo(tmp_path)

    monkeypatch.setattr("app.core.plugin.settings", SimpleNamespace(DEV=False, ROOT_PATH=tmp_path))
    monkeypatch.setattr("app.helper.plugin.settings.PLUGIN_LOCAL_REPO_PATHS", str(repo_path))
    monkeypatch.setattr(PluginHelper, "get_current_system_version", lambda: Version("2.13.10"))

    candidate = plugin_manager._get_local_plugin_candidate_from_path(source_file)

    assert candidate["system_version_compatible"] is False
    assert candidate["compatible"] is False
    assert "MoviePilot 版本 >=2.13.11" in candidate["skip_reason"]


def test_local_plugin_sync_without_candidate_respects_system_version_gate(
    tmp_path,
    monkeypatch,
    plugin_manager: PluginManager,
) -> None:
    """未传候选时的本地同步兜底查询也必须遵守系统版本门禁。"""
    repo_path, _source_file = _build_local_plugin_repo(tmp_path)
    runtime_dir = tmp_path / "app" / "plugins" / "demoplugin"
    settings_stub = SimpleNamespace(
        DEV=False,
        ROOT_PATH=tmp_path,
        VERSION_FLAG="v2",
        PLUGIN_LOCAL_REPO_PATHS=str(repo_path),
    )

    monkeypatch.setattr("app.core.plugin.settings", settings_stub)
    monkeypatch.setattr("app.helper.plugin.settings", settings_stub)
    monkeypatch.setattr(PluginHelper, "get_current_system_version", lambda: Version("2.13.10"))
    monkeypatch.setattr(
        "app.core.plugin.SystemConfigOper.get",
        lambda _self, key: ["DemoPlugin"] if key == SystemConfigKey.UserInstalledPlugins else None,
    )

    assert not plugin_manager._sync_local_plugin_if_installed("DemoPlugin")
    assert not runtime_dir.exists()
