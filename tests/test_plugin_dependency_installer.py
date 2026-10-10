import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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


def test_classify_plugins_preserves_ids_and_separates_startup_paths(
    tmp_path,
    monkeypatch,
):
    """启动分类保留规范插件 ID，并区分可加载、缺依赖和缺源码。"""
    plugin_root = tmp_path / "plugins"
    (plugin_root / "readyplugin").mkdir(parents=True)
    _write_requirements(plugin_root, "DependencyPending", "demo>=2\n")
    installer = PluginDependencyInstaller(
        Mock(),
        installed_plugins_provider=lambda: [
            "ReadyPlugin",
            "DependencyPending",
            "SourcePending",
        ],
        plugin_dir=plugin_root,
    )
    monkeypatch.setattr(
        installer,
        "_installed_packages",
        lambda: {"demo": Version("1.0")},
    )

    ready, missing_dependencies, missing_source = installer.classify_plugins()

    assert ready == ["ReadyPlugin"]
    assert missing_dependencies == ["DependencyPending"]
    assert missing_source == ["SourcePending"]


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


def test_disabled_plugins_do_not_contribute_dependencies_or_wheels(
    tmp_path, monkeypatch
):
    """停用插件的版本钉子、无效清单和 wheels 都不得进入恢复输入。"""
    _write_requirements(tmp_path, "Enabled", "demo==2\n")
    _write_requirements(tmp_path, "Disabled", "demo==1\n")
    _write_pyproject(tmp_path, "Invalid", "[project\n")
    enabled_wheels = tmp_path / "enabled" / "wheels"
    enabled_wheels.mkdir()
    (tmp_path / "disabled" / "wheels").mkdir()
    enabled = {"Enabled"}
    packages = Mock()
    packages.install_packages_with_fallback.return_value = (True, "installed")
    installer = PluginDependencyInstaller(
        packages,
        installed_plugins_provider=lambda: ["Enabled", "Disabled", "Invalid"],
        enabled_plugins_provider=lambda: enabled,
        plugin_dir=tmp_path,
    )
    monkeypatch.setattr(installer, "_installed_packages", lambda: {})

    assert installer.find_missing() == ["demo==2"]
    assert installer.install(installer.find_missing()) == (True, "installed")
    packages.install_packages_with_fallback.assert_called_once_with(
        [tmp_path / "enabled" / "requirements.txt"], [enabled_wheels]
    )

    enabled.clear()
    assert installer.find_missing() == []
    assert installer._wheels_dirs() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.parametrize("already_ready", [False, True])
async def test_conflicting_plugin_does_not_block_others_or_replace_ready_pin(
    tmp_path, monkeypatch, use_async, already_ready
):
    """冲突降级时保护已就绪和刚恢复的插件，并继续恢复无关插件。"""
    _write_requirements(tmp_path, "Alpha", "demo==1\n")
    _write_requirements(tmp_path, "Beta", "demo==2\n")
    _write_requirements(tmp_path, "Other", "unrelated>=1\n")
    installed = {"demo": Version("2")} if already_ready else {}
    calls = []

    def install(paths, _wheels):
        """模拟解析器拒绝互斥约束，不访问真实 Python 环境或网络。"""
        calls.append([path.parent.name for path in paths])
        requirements = [
            requirement
            for path in paths
            for requirement in load_dependency_file(path).dependencies
        ]
        pins = {str(item.specifier) for item in requirements if item.name == "demo"}
        if len(pins) > 1:
            return False, "No solution found when resolving dependencies"
        for item in requirements:
            installed[item.name] = Version("2" if str(item.specifier) == "==2" else "1")
        return True, "installed"

    packages = SimpleNamespace(
        install_packages_with_fallback=Mock(side_effect=install),
        async_install_packages_with_fallback=AsyncMock(side_effect=install),
    )
    installer = PluginDependencyInstaller(
        packages,
        installed_plugins_provider=lambda: ["Alpha", "Beta", "Other"],
        plugin_dir=tmp_path,
    )
    monkeypatch.setattr(installer, "_installed_packages", lambda: dict(installed))
    monkeypatch.setattr(installer, "_installed_distribution", lambda _name: None)
    missing = installer.find_missing()

    result = (
        await installer.async_install(missing)
        if use_async else installer.install(missing)
    )

    assert result[0] is False
    assert ("alpha" if already_ready else "beta") in result[1]
    assert installed["demo"] == Version("2" if already_ready else "1")
    assert installed["unrelated"] == Version("1")
    assert calls == (
        [["alpha", "beta", "other"], ["beta", "alpha"], ["beta", "other"]]
        if already_ready else
        [
            ["alpha", "beta", "other"], ["alpha"],
            ["alpha", "beta"], ["alpha", "other"],
        ]
    )
    ready, pending, _source_missing = installer.classify_plugins()
    assert ready == (["Beta", "Other"] if already_ready else ["Alpha", "Other"])
    assert pending == (["Alpha"] if already_ready else ["Beta"])


@pytest.mark.asyncio
@pytest.mark.parametrize("use_async", [False, True])
async def test_group_failure_or_exception_keeps_other_manifests_recoverable(
    tmp_path, monkeypatch, use_async
):
    """单组异常也不阻断后续组，原始 modern/legacy 来源声明保持完整。"""
    _write_requirements(tmp_path, "Broken", "broken>=1\n")
    _write_requirements(
        tmp_path, "Legacy", "--extra-index-url https://example.com\nlegacy\n"
    )
    modern = _write_pyproject(
        tmp_path, "Modern",
        '[project]\nname="modern"\nversion="1"\ndependencies=["modern"]\n'
        '[tool.uv.sources]\nmodern={url="https://example.com/modern.whl"}\n',
    )
    calls = []

    def install(paths, _wheels):
        """拒绝包含坏插件的请求，记录后续原始清单输入。"""
        calls.append(paths)
        if len(calls) == 1:
            return False, "batch failed"
        if paths[0].parent.name == "broken":
            raise OSError("group failed")
        return True, "installed"

    packages = SimpleNamespace(
        install_packages_with_fallback=Mock(side_effect=install),
        async_install_packages_with_fallback=AsyncMock(side_effect=install),
    )
    installer = PluginDependencyInstaller(
        packages,
        installed_plugins_provider=lambda: ["Broken", "Legacy", "Modern"],
        plugin_dir=tmp_path,
    )
    monkeypatch.setattr(installer, "_installed_packages", lambda: {})

    dependencies = ["broken", "legacy", "modern"]
    result = (
        await installer.async_install(dependencies)
        if use_async else installer.install(dependencies)
    )

    assert result[0] is False
    assert "broken" in result[1]
    assert "group failed" in result[1]
    assert calls[-1] == [
        tmp_path / "legacy" / "requirements.txt", modern / "pyproject.toml"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("use_async", [False, True])
async def test_fallback_reports_success_when_every_group_recovers(
    tmp_path, monkeypatch, use_async
):
    """批量失败但所有分组均恢复后，同步和异步结果都报告成功。"""
    for plugin_id in ("Alpha", "Beta"):
        _write_requirements(tmp_path, plugin_id, f"{plugin_id.lower()}>=1\n")
    results = [(False, "batch failed"), (True, "alpha"), (True, "beta")]
    packages = SimpleNamespace(
        install_packages_with_fallback=Mock(side_effect=results),
        async_install_packages_with_fallback=AsyncMock(side_effect=results),
    )
    installer = PluginDependencyInstaller(
        packages,
        installed_plugins_provider=lambda: ["Alpha", "Beta"],
        plugin_dir=tmp_path,
    )
    monkeypatch.setattr(installer, "_installed_packages", lambda: {})
    result = (
        await installer.async_install(["alpha", "beta"])
        if use_async else installer.install(["alpha", "beta"])
    )
    assert result == (True, "插件依赖已分组恢复")


@pytest.mark.asyncio
async def test_async_fallback_cancellation_stops_remaining_groups(tmp_path, monkeypatch):
    """取消分组安装必须传播到启动调用方，后续插件不得继续写环境。"""
    for plugin_id in ("Alpha", "Beta"):
        _write_requirements(tmp_path, plugin_id, f"{plugin_id.lower()}>=1\n")
    packages = SimpleNamespace(
        async_install_packages_with_fallback=AsyncMock(
            side_effect=[(False, "batch failed"), asyncio.CancelledError()]
        ),
    )
    installer = PluginDependencyInstaller(
        packages,
        installed_plugins_provider=lambda: ["Alpha", "Beta"],
        plugin_dir=tmp_path,
    )
    monkeypatch.setattr(installer, "_installed_packages", lambda: {})

    with pytest.raises(asyncio.CancelledError):
        await installer.async_install(["alpha", "beta"])

    assert packages.async_install_packages_with_fallback.await_count == 2


@pytest.mark.asyncio
async def test_async_install_inspection_runs_outside_event_loop(tmp_path, monkeypatch):
    """请求准备和失败分组扫描都由托管 worker 执行，不阻塞宿主事件循环。"""
    for plugin_id in ("Alpha", "Beta"):
        _write_requirements(tmp_path, plugin_id, f"{plugin_id.lower()}>=1\n")
    packages = SimpleNamespace(async_install_packages_with_fallback=AsyncMock(
        side_effect=[(False, "batch failed"), (True, "alpha"), (True, "beta")]
    ))
    installer = PluginDependencyInstaller(
        packages,
        installed_plugins_provider=lambda: ["Alpha", "Beta"],
        plugin_dir=tmp_path,
    )
    monkeypatch.setattr(installer, "_installed_packages", lambda: {})
    inspections = []

    def record(operation):
        """保留真实扫描行为并记录执行线程。"""
        def inspect(*args):
            """记录当前检查操作的线程身份。"""
            inspections.append((operation.__name__, threading.get_ident()))
            return operation(*args)
        return inspect

    monkeypatch.setattr(installer, "_prepare_install_request", record(
        installer._prepare_install_request
    ))
    monkeypatch.setattr(installer, "_fallback_plan", record(installer._fallback_plan))
    assert (await installer.async_install(["alpha", "beta"]))[0] is True
    assert [name for name, _thread in inspections] == [
        "_prepare_install_request", "_fallback_plan"
    ]
    assert all(thread != threading.get_ident() for _name, thread in inspections)


@pytest.mark.asyncio
async def test_async_install_waits_for_inspection_before_propagating_cancel(
    tmp_path, monkeypatch
):
    """取消请求等待在途扫描收敛，再向上抛出，且不启动任何包写入。"""
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    packages = SimpleNamespace(async_install_packages_with_fallback=AsyncMock())
    installer = PluginDependencyInstaller(packages, plugin_dir=tmp_path)

    def prepare(_dependencies):
        """用受控只读扫描模拟取消发生时仍在途的 worker。"""
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=5)
        return None, (False, "no manifest")

    monkeypatch.setattr(installer, "_prepare_install_request", prepare)
    task = asyncio.create_task(installer.async_install(["demo"]))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        packages.async_install_packages_with_fallback.assert_not_awaited()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    packages.async_install_packages_with_fallback.assert_not_awaited()
