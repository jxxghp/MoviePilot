from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from packaging.version import Version

from app.adapters.system.plugin.dependency import PluginDependencyInstaller


def _write_requirements(root: Path, plugin_id: str, content: str) -> None:
    """写入一个测试插件的 requirements 文件。"""
    plugin_dir = root / plugin_id.lower()
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "requirements.txt").write_text(content, encoding="utf-8")


def test_find_missing_merges_only_installed_plugin_constraints(tmp_path, monkeypatch):
    """依赖扫描只覆盖安装清单，并合并同名包的多插件约束。"""
    plugin_root = tmp_path / "plugins"
    _write_requirements(plugin_root, "Alpha", "Demo-Pkg>=2\n")
    _write_requirements(plugin_root, "Beta", "demo.pkg<4\n")
    _write_requirements(plugin_root, "Ignored", "unused>=1\n")
    installer = PluginDependencyInstaller(
        Mock(),
        installed_plugins_provider=lambda: ["Alpha", "Beta"],
        plugin_dir=plugin_root,
    )
    monkeypatch.setattr(
        installer,
        "_installed_packages",
        lambda: {"demo_pkg": Version("1.0")},
    )

    missing = installer.find_missing()

    assert len(missing) == 1
    assert missing[0].startswith("demo_pkg")
    assert ">=2" in missing[0]
    assert "<4" in missing[0]
    assert all("unused" not in item for item in missing)


def test_find_missing_skips_satisfied_constraints(tmp_path, monkeypatch):
    """已安装版本满足合并约束时不得重复调用 pip。"""
    plugin_root = tmp_path / "plugins"
    _write_requirements(plugin_root, "Alpha", "demo>=1,<3\n")
    installer = PluginDependencyInstaller(
        Mock(),
        installed_plugins_provider=lambda: ["Alpha"],
        plugin_dir=plugin_root,
    )
    monkeypatch.setattr(
        installer,
        "_installed_packages",
        lambda: {"demo": Version("2.0")},
    )

    assert installer.find_missing() == []


def test_install_uses_adapter_owned_temporary_requirements(tmp_path, monkeypatch):
    """批量依赖文件由依赖适配器创建并在 pip 返回后清理。"""
    helper = Mock()
    helper.pip_install_with_fallback.return_value = (True, "installed")
    monkeypatch.setattr(
        "app.adapters.system.plugin.dependency.settings",
        SimpleNamespace(ROOT_PATH=tmp_path, TEMP_PATH=tmp_path / "temp"),
    )
    installer = PluginDependencyInstaller(
        helper,
        installed_plugins_provider=lambda: [],
        plugin_dir=tmp_path / "plugins",
    )

    result = installer.install(["demo>=2", "other"])

    assert result == (True, "installed")
    requirements_file = helper.pip_install_with_fallback.call_args.args[0]
    assert requirements_file.name == "requirements.txt"
    assert not requirements_file.exists()
