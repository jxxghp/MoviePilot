from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from app.adapters.system.plugin.dependency import PluginDependencyInstaller
from app.adapters.system.plugin.manifest import load_dependency_file


def _write_requirements(root: Path, plugin_id: str, content: str) -> None:
    """写入一个测试插件的 requirements 文件。"""
    plugin_dir = root / plugin_id.lower()
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "requirements.txt").write_text(content, encoding="utf-8")


def _write_pyproject(root: Path, plugin_id: str, content: str) -> Path:
    """写入一个测试插件的 pyproject 依赖清单。"""
    plugin_dir = root / plugin_id.lower()
    plugin_dir.mkdir(parents=True, exist_ok=True)
    pyproject_file = plugin_dir / "pyproject.toml"
    pyproject_file.write_text(content, encoding="utf-8")
    return plugin_dir


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


def test_find_missing_preserves_merged_extras(tmp_path, monkeypatch):
    """同一包的多插件约束合并后必须保留全部 extras。"""
    plugin_root = tmp_path / "plugins"
    _write_requirements(plugin_root, "Alpha", "Demo-Pkg[alpha]>=2\n")
    _write_requirements(plugin_root, "Beta", "demo.pkg[beta]<4\n")
    installer = PluginDependencyInstaller(
        Mock(),
        installed_plugins_provider=lambda: ["Alpha", "Beta"],
        plugin_dir=plugin_root,
    )
    monkeypatch.setattr(installer, "_installed_packages", lambda: {})

    missing = installer.find_missing()

    assert len(missing) == 1
    requirement = Requirement(missing[0])
    assert requirement.name == "demo_pkg"
    assert requirement.extras == {"alpha", "beta"}
    assert ">=2" in str(requirement.specifier)
    assert "<4" in str(requirement.specifier)


def test_find_missing_preserves_direct_url(tmp_path, monkeypatch):
    """缺失的 direct URL 依赖必须按原安装来源返回。"""
    plugin_root = tmp_path / "plugins"
    direct_url = "https://example.com/packages/demo_pkg-2.0.0-py3-none-any.whl"
    _write_requirements(
        plugin_root,
        "Alpha",
        f"Demo-Pkg[feature] @ {direct_url}\n",
    )
    installer = PluginDependencyInstaller(
        Mock(),
        installed_plugins_provider=lambda: ["Alpha"],
        plugin_dir=plugin_root,
    )
    monkeypatch.setattr(installer, "_installed_packages", lambda: {})

    missing = installer.find_missing()

    assert len(missing) == 1
    requirement = Requirement(missing[0])
    assert canonicalize_name(requirement.name) == canonicalize_name("Demo-Pkg")
    assert requirement.extras == {"feature"}
    assert requirement.url == direct_url


def test_find_missing_does_not_accept_base_package_for_extra(
    tmp_path, monkeypatch
):
    """已安装基础包但未安装其 extra 依赖时必须继续恢复。"""
    plugin_root = tmp_path / "plugins"
    _write_requirements(plugin_root, "Alpha", "Demo[feature]>=1\n")
    installer = PluginDependencyInstaller(
        Mock(),
        installed_plugins_provider=lambda: ["Alpha"],
        plugin_dir=plugin_root,
    )
    monkeypatch.setattr(
        installer, "_installed_packages", lambda: {"demo": Version("2.0")}
    )
    metadata = SimpleNamespace(
        get_all=lambda key: {"Provides-Extra": ["feature"], "Requires-Dist": [
            "feature-dependency>=1; extra == 'feature'"
        ]}.get(key, []),
    )
    monkeypatch.setattr(
        installer,
        "_installed_distribution",
        lambda package_name: SimpleNamespace(metadata=metadata)
        if package_name == "demo"
        else None,
    )

    assert installer.find_missing() == ["demo[feature]>=1"]


def test_find_missing_accepts_satisfied_extra_dependencies(tmp_path, monkeypatch):
    """已安装 extra 及其依赖时不得重复恢复。"""
    plugin_root = tmp_path / "plugins"
    _write_requirements(plugin_root, "Alpha", "Demo[feature]>=1\n")
    installer = PluginDependencyInstaller(
        Mock(),
        installed_plugins_provider=lambda: ["Alpha"],
        plugin_dir=plugin_root,
    )
    monkeypatch.setattr(
        installer,
        "_installed_packages",
        lambda: {"demo": Version("2.0"), "feature_dependency": Version("1.2")},
    )
    metadata = SimpleNamespace(
        get_all=lambda key: {"Provides-Extra": ["feature"], "Requires-Dist": [
            "feature-dependency>=1; extra == 'feature'"
        ]}.get(key, []),
    )
    monkeypatch.setattr(
        installer,
        "_installed_distribution",
        lambda package_name: SimpleNamespace(metadata=metadata)
        if package_name == "demo"
        else None,
    )

    assert installer.find_missing() == []


def test_find_missing_rejects_missing_transitive_extra_dependency(
    tmp_path, monkeypatch
):
    """extra 的传递依赖缺失时不能只因根包已安装就跳过恢复。"""
    plugin_root = tmp_path / "plugins"
    _write_requirements(plugin_root, "Alpha", "Demo[feature]>=1\n")
    installer = PluginDependencyInstaller(
        Mock(),
        installed_plugins_provider=lambda: ["Alpha"],
        plugin_dir=plugin_root,
    )
    monkeypatch.setattr(
        installer,
        "_installed_packages",
        lambda: {"demo": Version("2.0"), "bridge": Version("1.0")},
    )
    metadata_by_name = {
        "demo": SimpleNamespace(
            metadata=SimpleNamespace(
                get_all=lambda key: {
                    "Provides-Extra": ["feature"],
                    "Requires-Dist": ["bridge>=1; extra == 'feature'"],
                }.get(key, [])
            )
        ),
        "bridge": SimpleNamespace(
            metadata=SimpleNamespace(
                get_all=lambda key: {
                    "Requires-Dist": ["leaf>=1"],
                }.get(key, [])
            )
        ),
    }
    monkeypatch.setattr(
        installer,
        "_installed_distribution",
        lambda package_name: metadata_by_name.get(package_name),
    )

    assert installer.find_missing() == ["demo[feature]>=1"]


def test_find_missing_rejects_same_name_package_from_wrong_direct_url(
    tmp_path, monkeypatch
):
    """存在不同 PEP 610 来源时，同名包不能满足 direct URL 依赖。"""
    plugin_root = tmp_path / "plugins"
    required_url = "https://example.com/packages/demo-2.0.0-py3-none-any.whl"
    installed_url = "https://mirror.example.com/packages/demo-2.0.0-py3-none-any.whl"
    _write_requirements(plugin_root, "Alpha", f"Demo @ {required_url}\n")
    installer = PluginDependencyInstaller(
        Mock(),
        installed_plugins_provider=lambda: ["Alpha"],
        plugin_dir=plugin_root,
    )
    monkeypatch.setattr(
        installer, "_installed_packages", lambda: {"demo": Version("2.0")}
    )
    metadata = SimpleNamespace(get_all=lambda _key: [])
    monkeypatch.setattr(
        installer,
        "_installed_distribution",
        lambda _package_name: SimpleNamespace(
            metadata=metadata,
            read_text=lambda _name: '{"url": "' + installed_url + '"}',
        ),
    )

    assert installer.find_missing() == [f"demo @ {required_url}"]


def test_find_missing_accepts_matching_direct_url(tmp_path, monkeypatch):
    """同名包且 PEP 610 来源一致时应视为已满足。"""
    plugin_root = tmp_path / "plugins"
    direct_url = "https://example.com/packages/demo-2.0.0-py3-none-any.whl"
    _write_requirements(plugin_root, "Alpha", f"Demo @ {direct_url}\n")
    installer = PluginDependencyInstaller(
        Mock(),
        installed_plugins_provider=lambda: ["Alpha"],
        plugin_dir=plugin_root,
    )
    monkeypatch.setattr(
        installer, "_installed_packages", lambda: {"demo": Version("2.0")}
    )
    metadata = SimpleNamespace(get_all=lambda _key: [])
    monkeypatch.setattr(
        installer,
        "_installed_distribution",
        lambda _package_name: SimpleNamespace(
            metadata=metadata,
            read_text=lambda _name: '{"url": "' + direct_url + '"}',
        ),
    )

    assert installer.find_missing() == []


def test_find_missing_prefers_pyproject_project_dependencies(
    tmp_path,
    monkeypatch,
):
    """现代清单优先，且只消费 project.dependencies。"""
    plugin_root = tmp_path / "plugins"
    plugin_dir = _write_pyproject(
        plugin_root,
        "Alpha",
        """
[project]
name = "alpha"
version = "1.0.0"
dependencies = ["Modern-Pkg>=2"]

[dependency-groups]
dev = ["group-only>=1"]
""",
    )
    (plugin_dir / "requirements.txt").write_text(
        "legacy-only>=1\n",
        encoding="utf-8",
    )
    (plugin_dir / "uv.lock").write_text(
        'package = [{ name = "lock-only", version = "1.0.0" }]\n',
        encoding="utf-8",
    )
    installer = PluginDependencyInstaller(
        Mock(),
        installed_plugins_provider=lambda: ["Alpha"],
        plugin_dir=plugin_root,
    )
    monkeypatch.setattr(installer, "_installed_packages", lambda: {})

    missing = installer.find_missing()

    assert missing == ["modern_pkg>=2"]


@pytest.mark.parametrize(
    "pyproject",
    [
        "[project\n",
        '[project]\ndependencies = "demo>=2"\n',
        '[project]\ndependencies = ["not a requirement !!!"]\n',
        '[project]\ndynamic = ["dependencies"]\n',
    ],
)
def test_find_missing_fails_closed_for_invalid_pyproject(
    tmp_path,
    monkeypatch,
    pyproject,
):
    """现代清单无效时不得回退并消费旧 requirements。"""
    plugin_root = tmp_path / "plugins"
    plugin_dir = _write_pyproject(plugin_root, "Alpha", pyproject)
    (plugin_dir / "requirements.txt").write_text(
        "legacy-only>=1\n",
        encoding="utf-8",
    )
    installer = PluginDependencyInstaller(
        Mock(),
        installed_plugins_provider=lambda: ["Alpha"],
        plugin_dir=plugin_root,
    )
    monkeypatch.setattr(installer, "_installed_packages", lambda: {})

    with pytest.raises(ValueError, match="pyproject.toml"):
        installer.find_missing()


@pytest.mark.parametrize(
    "pyproject",
    [
        '[project]\nversion = "1.0.0"\ndependencies = ["demo>=2"]\n',
        '[project]\nname = "alpha"\ndependencies = ["demo>=2"]\n',
        '[project]\nname = "   "\nversion = "1.0.0"\n'
        'dependencies = ["demo>=2"]\n',
        '[project]\nname = "alpha"\nversion = "   "\n'
        'dependencies = ["demo>=2"]\n',
    ],
)
def test_find_missing_fails_closed_without_required_project_identity(
    tmp_path,
    monkeypatch,
    pyproject,
):
    """现代清单缺少 uv 消费所需的 name 或 version 时必须拒绝安装。"""
    plugin_root = tmp_path / "plugins"
    _write_pyproject(plugin_root, "Alpha", pyproject)
    installer = PluginDependencyInstaller(
        Mock(),
        installed_plugins_provider=lambda: ["Alpha"],
        plugin_dir=plugin_root,
    )
    monkeypatch.setattr(installer, "_installed_packages", lambda: {})

    with pytest.raises(ValueError, match="pyproject.toml"):
        installer.find_missing()


def test_find_missing_accepts_dynamic_project_version(tmp_path, monkeypatch):
    """version 由构建后端动态提供时仍可消费静态 dependencies。"""
    plugin_root = tmp_path / "plugins"
    _write_pyproject(
        plugin_root,
        "Alpha",
        '[project]\nname = "alpha"\ndynamic = ["version"]\n'
        'dependencies = ["demo>=2"]\n',
    )
    installer = PluginDependencyInstaller(
        Mock(),
        installed_plugins_provider=lambda: ["Alpha"],
        plugin_dir=plugin_root,
    )
    monkeypatch.setattr(installer, "_installed_packages", lambda: {})

    assert installer.find_missing() == ["demo>=2"]


def test_load_dependency_file_accepts_custom_legacy_filename(tmp_path):
    """临时或自定义命名的旧格式依赖文件复用统一解析器。"""
    dependency_file = tmp_path / "plugin-dependencies.txt"
    dependency_file.write_text("Demo-Pkg>=2\n", encoding="utf-8")

    manifest = load_dependency_file(dependency_file)

    assert manifest.path == dependency_file
    assert [str(requirement) for requirement in manifest.dependencies] == [
        "Demo-Pkg>=2"
    ]


def test_install_passes_all_active_manifests_to_one_install(tmp_path):
    """缺失依赖恢复必须保留 modern 与 legacy 清单的原始内容。"""
    plugin_root = tmp_path / "plugins"
    modern_dir = _write_pyproject(
        plugin_root,
        "Alpha",
        """
[project]
name = "alpha"
version = "1.0.0"
dependencies = ["demo>=2"]

[[tool.uv.index]]
name = "private"
url = "https://packages.example/simple"
explicit = true

[tool.uv.sources]
demo = { index = "private" }
""",
    )
    _write_requirements(
        plugin_root,
        "Beta",
        "--extra-index-url https://legacy.example/simple\nother\n",
    )
    helper = Mock()
    helper.install_packages_with_fallback.return_value = (True, "installed")
    installer = PluginDependencyInstaller(
        helper,
        installed_plugins_provider=lambda: ["Alpha", "Beta"],
        plugin_dir=plugin_root,
    )

    result = installer.install([
        "demo[feature] @ https://example.com/demo.whl",
        "other",
    ])

    assert result == (True, "installed")
    manifest_paths = helper.install_packages_with_fallback.call_args.args[0]
    assert manifest_paths == [
        modern_dir / "pyproject.toml",
        plugin_root / "beta" / "requirements.txt",
    ]
    assert "[tool.uv.sources]" in manifest_paths[0].read_text(encoding="utf-8")
    assert "--extra-index-url" in manifest_paths[1].read_text(encoding="utf-8")
