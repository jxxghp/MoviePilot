"""插件联邦远程入口按实际运行版本隔离的契约测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest
from fastapi import HTTPException

from app.api.endpoints import plugin as plugin_endpoint
from app.runtime.extensions.plugin.manager import PluginManager
from app.runtime.extensions.plugin.projection import PluginProjection
from app.runtime.extensions.plugin.version import (
    plugin_version_dir_name,
    register_plugin_version,
)


def _write_version_dir(plugins_root: Path, plugin_id: str, version: str) -> Path:
    """创建一个版本目录并放入可读取的联邦入口文件。"""
    plugin_root = plugins_root / "app" / "plugins" / plugin_id.lower()
    version_dir = plugin_root / plugin_version_dir_name(version)
    version_dir.mkdir(parents=True, exist_ok=True)
    remote_entry = version_dir / "dist" / "assets" / "remoteEntry.js"
    remote_entry.parent.mkdir(parents=True, exist_ok=True)
    remote_entry.write_text(f"export default '{version}'", encoding="utf-8")
    register_plugin_version(plugin_root, version, source="test")
    return version_dir


@pytest.fixture
def runtime_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把静态入口构造器的运行根目录指向用例临时目录。"""
    monkeypatch.setattr(
        "app.runtime.extensions.plugin.manager.get_runtime_setting",
        lambda _key: tmp_path,
    )
    return tmp_path


def test_remote_entry_uses_the_requested_version_without_fallback(
    runtime_root: Path,
):
    """指定版本定位对应目录，缺失版本仍保留请求版本供端点返回 404。"""
    _write_version_dir(runtime_root, "RemotePlugin", "1.0.0")
    _write_version_dir(runtime_root, "RemotePlugin", "2.0.0")

    pinned = PluginManager.get_plugin_remote_entry(
        "RemotePlugin", "dist/assets", version="1.0.0"
    )
    missing = PluginManager.get_plugin_remote_entry(
        "RemotePlugin", "dist/assets", version="9.9.9"
    )

    assert pinned == (
        "/plugin/file/remoteplugin/__versions__/v1_0_0/dist/assets/remoteEntry.js"
    )
    assert missing == (
        "/plugin/file/remoteplugin/__versions__/v9_9_9/dist/assets/remoteEntry.js"
    )


def test_projection_uses_actual_plugin_version_for_remote_url_and_key(
    runtime_root: Path,
):
    """本体与分身的运行版本分别进入地址和 remote_key，绑定记录不参与投影。"""
    _write_version_dir(runtime_root, "RemotePlugin", "1.0.0")
    _write_version_dir(runtime_root, "RemotePlugin", "2.0.0")

    class VuePlugin:
        """提供联邦声明所需最小 hook 的运行态插件替身。"""

        plugin_name = "远程插件"

        def __init__(self, version: str, source_plugin_id: Optional[str] = None):
            self.plugin_version = version
            self.plugin_source_id = source_plugin_id

        def get_render_mode(self):
            """声明 Vue 联邦产物路径。"""
            return "vue", "dist/assets"

    projection = PluginProjection(
        {
            "RemotePlugin": VuePlugin("1.0.0"),
            "RemotePluginWork": VuePlugin("2.0.0", "RemotePlugin"),
        },
        remote_entry_factory=PluginManager.get_plugin_remote_entry,
    )

    remotes = {remote["id"]: remote for remote in projection.remotes()}

    assert remotes["RemotePlugin"]["version"] == "1.0.0"
    assert remotes["RemotePlugin"]["remote_key"] == "RemotePlugin#1.0.0"
    assert remotes["RemotePlugin"]["url"].endswith(
        "__versions__/v1_0_0/dist/assets/remoteEntry.js"
    )
    assert remotes["RemotePluginWork"]["version"] == "2.0.0"
    assert remotes["RemotePluginWork"]["remote_key"] == "RemotePluginWork#2.0.0"
    assert remotes["RemotePluginWork"]["source_plugin_id"] == "RemotePlugin"
    assert remotes["RemotePluginWork"]["url"].startswith(
        "/plugin/file/remotepluginwork/__versions__/v2_0_0/"
    )


def test_auth_provider_remote_uses_the_same_version_descriptor():
    """认证远程入口与普通 remote 使用同一版本字段，避免登录页加载旧版本。"""

    class VueAuthPlugin:
        """提供认证 hook 的运行态插件替身。"""

        plugin_name = "认证插件"
        plugin_version = "3.1.0"

        def get_state(self):
            """声明插件已启用。"""
            return True

        def get_render_mode(self):
            """声明 Vue 联邦产物路径。"""
            return "vue", "dist"

        def get_auth_providers(self):
            """声明一个插件认证提供方。"""
            return [{"id": "remote-login"}]

    projection = PluginProjection(
        {"AuthPlugin": VueAuthPlugin()},
        remote_entry_factory=lambda plugin_id, path, version: (
            f"/{plugin_id}/{version}/{path}"
        ),
    )

    remote = projection.auth_providers()[0]["remote"]

    assert remote["version"] == "3.1.0"
    assert remote["remote_key"] == "AuthPlugin#3.1.0"
    assert remote["url"] == "/AuthPlugin/3.1.0/dist"


def test_versioned_remote_url_reads_the_requested_version(
    runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """版本化 URL 显式选择目录，不会因当前运行版本不同而重复拼版本段。"""
    _write_version_dir(runtime_root, "RemotePlugin", "1.0.0")
    _write_version_dir(runtime_root, "RemotePlugin", "2.0.0")
    manager = SimpleNamespace(
        get_plugin_source_id=lambda _plugin_id: "RemotePlugin",
        get_plugin_running_version=lambda _plugin_id: "2.0.0",
        get_plugin_version_binding=lambda _plugin_id: None,
    )
    monkeypatch.setattr(plugin_endpoint, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(
        plugin_endpoint,
        "get_api_runtime_config_snapshot",
        lambda: SimpleNamespace(root_path=runtime_root),
    )

    response = asyncio.run(
        plugin_endpoint.plugin_static_file(
            "RemotePlugin",
            "__versions__/v1_0_0/dist/assets/remoteEntry.js",
            None,
        )
    )

    async def read_body() -> bytes:
        """读取流式响应的完整内容。"""
        return b"".join([chunk async for chunk in response.body_iterator])

    assert asyncio.run(read_body()) == b"export default '1.0.0'"


def test_versioned_remote_url_does_not_fallback_to_another_installed_version(
    runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """已版本化布局缺少 URL 指定版本时返回 404，避免旧入口污染新版本。"""
    _write_version_dir(runtime_root, "RemotePlugin", "2.0.0")
    manager = SimpleNamespace(
        get_plugin_source_id=lambda _plugin_id: "RemotePlugin",
        get_plugin_running_version=lambda _plugin_id: "2.0.0",
        get_plugin_version_binding=lambda _plugin_id: None,
    )
    monkeypatch.setattr(plugin_endpoint, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(
        plugin_endpoint,
        "get_api_runtime_config_snapshot",
        lambda: SimpleNamespace(root_path=runtime_root),
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            plugin_endpoint.plugin_static_file(
                "RemotePlugin",
                "__versions__/v1_0_0/dist/assets/remoteEntry.js",
                None,
            )
        )

    assert error.value.status_code == 404


def test_versioned_remote_url_keeps_flat_plugin_compatibility(
    runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """尚未迁移版本目录的插件仍可通过带版本缓存段的 remote URL 读取资源。"""
    remote_entry = (
        runtime_root
        / "app"
        / "plugins"
        / "flatplugin"
        / "dist"
        / "remoteEntry.js"
    )
    plugin_root = remote_entry.parent.parent
    plugin_root.mkdir(parents=True, exist_ok=True)
    (plugin_root / "__init__.py").write_text(
        "class FlatPlugin:\n    plugin_version = '1.0.0'\n",
        encoding="utf-8",
    )
    remote_entry.parent.mkdir(parents=True)
    remote_entry.write_text("export default 'flat'", encoding="utf-8")
    legacy_entry = runtime_root / "app" / "plugins" / "flatplugin" / "legacy" / "remoteEntry.js"
    legacy_entry.parent.mkdir(parents=True)
    legacy_entry.write_text("export default 'legacy'", encoding="utf-8")
    manager = SimpleNamespace(
        get_plugin_source_id=lambda _plugin_id: "FlatPlugin",
        get_plugin_running_version=lambda _plugin_id: "1.0.0",
        get_plugin_version_binding=lambda _plugin_id: None,
    )
    monkeypatch.setattr(plugin_endpoint, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(
        plugin_endpoint,
        "get_api_runtime_config_snapshot",
        lambda: SimpleNamespace(root_path=runtime_root),
    )

    response = asyncio.run(
        plugin_endpoint.plugin_static_file(
            "FlatPlugin",
            "__versions__/v1_0_0/dist/remoteEntry.js",
            None,
        )
    )

    async def read_body() -> bytes:
        """读取流式响应的完整内容。"""
        return b"".join([chunk async for chunk in response.body_iterator])

    assert asyncio.run(read_body()) == b"export default 'flat'"

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            plugin_endpoint.plugin_static_file(
                "FlatPlugin",
                "__versions__/v2_0_0/dist/remoteEntry.js",
                None,
            )
        )

    assert error.value.status_code == 404

    legacy_response = asyncio.run(
        plugin_endpoint.plugin_static_file(
            "FlatPlugin", "legacy/remoteEntry.js", None
        )
    )

    async def read_legacy_body() -> bytes:
        """读取旧版静态目录响应的完整内容。"""
        return b"".join([chunk async for chunk in legacy_response.body_iterator])

    assert asyncio.run(read_legacy_body()) == b"export default 'legacy'"
