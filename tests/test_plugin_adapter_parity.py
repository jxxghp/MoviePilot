"""插件市场、包安装和依赖安装同步异步一致性测试。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.adapters.external.plugin.client import PluginMarketTransport
from app.adapters.system.plugin.dependency import PluginDependencyInstaller
from app.adapters.system.plugin.package import PluginPackageManager

REPO_URL = "https://github.com/example/moviepilot-plugins"
SYNC_INDEX_REQUEST = "_PluginMarketTransport__request_plugin_index_with_fallback"
ASYNC_INDEX_REQUEST = (
    "_PluginMarketTransport__async_request_plugin_index_with_fallback"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected", "error"),
    [
        ((404, ""), None, None),
        ((200, '{"DemoPlugin": {"version": "1.2.3"}}'),
         {"DemoPlugin": {"version": "1.2.3"}}, None),
        (None, None, "插件索引请求失败：连接失败"),
        ((500, "upstream failed"), None, "插件索引请求失败：HTTP 500"),
        ((200, "not-json"), None, "插件索引响应格式无效"),
    ],
)
async def test_plugin_index_sync_async_result_contract_is_identical(
    monkeypatch,
    response: tuple[int, str] | None,
    expected: dict | None,
    error: str | None,
) -> None:
    """同步与异步索引传输必须经过同一响应分类和错误语义。"""
    transport = PluginMarketTransport()

    def request(_url: str, *, headers: dict) -> tuple[int, str] | None:
        return response

    async def async_request(
        _url: str, *, headers: dict
    ) -> tuple[int, str] | None:
        return response

    monkeypatch.setattr(transport, SYNC_INDEX_REQUEST, request)
    monkeypatch.setattr(transport, ASYNC_INDEX_REQUEST, async_request)
    transport.get_plugin_index_result.cache_clear()
    await transport.async_get_plugin_index_result.cache_clear()

    if error is not None:
        with pytest.raises(RuntimeError, match=error):
            transport.get_plugin_index_result(REPO_URL, "v3")
        with pytest.raises(RuntimeError, match=error):
            await transport.async_get_plugin_index_result(REPO_URL, "v3")
        return

    sync_result = transport.get_plugin_index_result(REPO_URL, "v3")
    async_result = await transport.async_get_plugin_index_result(REPO_URL, "v3")

    assert sync_result == async_result == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin_id", "repo_url", "frozen", "repo_info", "selected", "expected"),
    [
        ("DemoPlugin", REPO_URL, True, ("example", "repo"), "v3",
         (False, "可执行文件模式下，只能安装本地插件")),
        ("", REPO_URL, False, ("example", "repo"), "v3",
         (False, "参数错误")),
        ("DemoPlugin", "invalid", False, (None, None), "v3",
         (False, "不支持的插件仓库地址格式")),
        ("DemoPlugin", REPO_URL, False, ("example", "repo"), None,
         (False, "DemoPlugin 没有找到适用于当前版本的插件")),
    ],
)
async def test_plugin_package_sync_async_preflight_failures_are_identical(
    monkeypatch,
    plugin_id: str,
    repo_url: str,
    frozen: bool,
    repo_info: tuple[str | None, str | None],
    selected: str | None,
    expected: tuple[bool, str],
) -> None:
    """远端包安装的环境、参数、仓库和代际失败必须保持同一结果。"""
    manager = PluginPackageManager(source=Mock())
    monkeypatch.setattr(manager, "is_local_repo_url", lambda _repo_url: False)
    monkeypatch.setattr(
        "app.adapters.system.plugin.package.SystemUtils.is_frozen",
        lambda: frozen,
    )
    monkeypatch.setattr(manager, "get_repo_info", lambda _repo_url: repo_info)
    monkeypatch.setattr(
        manager,
        "get_plugin_package_version",
        lambda *_args: selected,
    )

    async def async_get_plugin_package_version(*_args) -> str | None:
        return selected

    monkeypatch.setattr(
        manager,
        "async_get_plugin_package_version",
        async_get_plugin_package_version,
    )

    sync_result = manager.install_raw(plugin_id, repo_url, package_version="v3")
    async_result = await manager.async_install_raw(
        plugin_id, repo_url, package_version="v3"
    )

    assert sync_result == async_result == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata", "release_version", "release_items", "expected_calls"),
    [
        ({"version": "1.2.3", "release": False}, None, [], ["filelist"]),
        (
            {"version": "1.2.3", "release": True},
            None,
            [],
            ["release", "filelist"],
        ),
        (
            {"version": "1.2.3", "release": True},
            "1.2.3",
            [{"version": "1.2.3"}],
            ["release"],
        ),
    ],
)
async def test_plugin_package_sync_async_execute_same_selected_strategy(
    monkeypatch,
    tmp_path: Path,
    metadata: dict,
    release_version: str | None,
    release_items: list[dict],
    expected_calls: list[str],
) -> None:
    """共享安装选择必须让同步与异步外壳执行相同 Release 回退路径。"""
    manager = PluginPackageManager(source=Mock())
    sync_calls: list[str] = []
    async_calls: list[str] = []
    monkeypatch.setattr(manager, "is_local_repo_url", lambda _repo_url: False)
    monkeypatch.setattr(
        "app.adapters.system.plugin.package.SystemUtils.is_frozen",
        lambda: False,
    )
    monkeypatch.setattr(
        manager, "get_repo_info", lambda _repo_url: ("example", "repo")
    )
    monkeypatch.setattr(
        manager, "get_plugin_package_version", lambda *_args: "v3"
    )
    monkeypatch.setattr(
        manager, "get_plugin_release_versions", lambda *_args: release_items
    )
    monkeypatch.setattr(
        manager, "_PluginPackageManager__get_plugin_meta", lambda *_args: metadata
    )

    async def selected_version(*_args) -> str:
        return "v3"

    async def releases(*_args) -> list[dict]:
        return release_items

    async def meta(*_args) -> dict:
        return metadata

    def install_release(*_args) -> tuple[bool, str]:
        sync_calls.append("release")
        return (True, "installed") if release_version else (False, "missing asset")

    async def async_install_release(*_args) -> tuple[bool, str]:
        async_calls.append("release")
        return (True, "installed") if release_version else (False, "missing asset")

    def prepare_filelist(*_args) -> tuple[bool, str]:
        sync_calls.append("filelist")
        return True, "installed"

    async def async_prepare_filelist(*_args) -> tuple[bool, str]:
        async_calls.append("filelist")
        return True, "installed"

    def install_flow(_pid, _force, prepare, _repo_url, _before):
        return prepare(tmp_path / "staging")

    async def async_install_flow(_pid, _force, prepare, _repo_url, _before):
        return await prepare(tmp_path / "staging")

    monkeypatch.setattr(manager, "async_get_plugin_package_version", selected_version)
    monkeypatch.setattr(manager, "async_get_plugin_release_versions", releases)
    monkeypatch.setattr(manager, "_PluginPackageManager__async_get_plugin_meta", meta)
    monkeypatch.setattr(manager, "_PluginPackageManager__install_from_release", install_release)
    monkeypatch.setattr(manager, "_PluginPackageManager__async_install_from_release", async_install_release)
    monkeypatch.setattr(manager, "_PluginPackageManager__prepare_content_via_filelist_sync", prepare_filelist)
    monkeypatch.setattr(manager, "_PluginPackageManager__prepare_content_via_filelist_async", async_prepare_filelist)
    monkeypatch.setattr(manager, "_PluginPackageManager__install_flow_sync", install_flow)
    monkeypatch.setattr(manager, "_PluginPackageManager__install_flow_async", async_install_flow)

    sync_result = manager.install_raw(
        "DemoPlugin",
        REPO_URL,
        package_version="v3",
        release_version=release_version,
    )
    async_result = await manager.async_install_raw(
        "DemoPlugin",
        REPO_URL,
        package_version="v3",
        release_version=release_version,
    )

    assert sync_result == async_result == (True, "installed")
    assert sync_calls == async_calls == expected_calls


@pytest.mark.asyncio
async def test_dependency_install_sync_async_share_request_and_result(
    tmp_path: Path, monkeypatch
) -> None:
    """依赖安装必须向同步与异步包端口提交同一份规范请求。"""
    manifest_path = tmp_path / "requirements.txt"
    wheels_dir = tmp_path / "wheels"
    packages = SimpleNamespace(
        install_packages_with_fallback=Mock(return_value=(True, "installed")),
        async_install_packages_with_fallback=AsyncMock(
            return_value=(True, "installed")
        ),
    )
    installer = PluginDependencyInstaller(packages, plugin_dir=tmp_path)
    monkeypatch.setattr(
        installer,
        "_plugin_manifests",
        lambda: [SimpleNamespace(path=manifest_path)],
    )
    monkeypatch.setattr(installer, "_wheels_dirs", lambda: [wheels_dir])

    sync_result = installer.install(["demo>=1"])
    async_result = await installer.async_install(["demo>=1"])

    assert sync_result == async_result == (True, "installed")
    packages.install_packages_with_fallback.assert_called_once_with(
        [manifest_path], [wheels_dir]
    )
    packages.async_install_packages_with_fallback.assert_awaited_once_with(
        [manifest_path], [wheels_dir]
    )


@pytest.mark.asyncio
async def test_dependency_install_sync_async_share_error_mapping(
    tmp_path: Path, monkeypatch
) -> None:
    """清单准备和包端口异常必须映射为完全一致的公开失败结果。"""
    packages = SimpleNamespace(
        install_packages_with_fallback=Mock(side_effect=OSError("disk full")),
        async_install_packages_with_fallback=AsyncMock(
            side_effect=OSError("disk full")
        ),
    )
    installer = PluginDependencyInstaller(packages, plugin_dir=tmp_path)
    monkeypatch.setattr(
        installer,
        "_plugin_manifests",
        lambda: [SimpleNamespace(path=tmp_path / "requirements.txt")],
    )
    monkeypatch.setattr(installer, "_wheels_dirs", lambda: [])

    sync_result = installer.install(["demo>=1"])
    async_result = await installer.async_install(["demo>=1"])

    assert sync_result == async_result == (
        False,
        "安装依赖项时发生错误：disk full",
    )
