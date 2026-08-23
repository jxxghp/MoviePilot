import asyncio
import io
import os
import stat
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from packaging.requirements import Requirement
from packaging.version import Version


PLUGIN_ID = "DemoPlugin"
REPO_URL = "https://github.com/demo/MoviePilot-Plugins"


@pytest.fixture(autouse=True)
def _configure_plugin_catalog_factory(monkeypatch):
    """为直接构造 PluginManager 的测试注入真实目录用例和假持久化接缝。"""
    from app.adapters.external.plugin.client import PluginMarketClient
    from app.application.plugin.catalog import PluginCatalogService
    from app.foundation.version import compare_version
    from app.runtime.extensions import plugin_manager as manager_module

    def build_catalog(manager):
        """按生产组合方式连接目录服务，但保留测试可替换的依赖。"""
        client = PluginMarketClient()
        return PluginCatalogService(
            market_loader=client.get_plugins,
            async_market_loader=client.async_get_plugins,
            installed_plugins_provider=lambda: manager_module.get_plugin_storage().read(
                manager_module.SystemConfigKey.UserInstalledPlugins
            ) or [],
            plugin_mapper=manager._process_plugin_info,
            is_local_repo=PluginMarketClient.is_local_repo_url,
            version_compare=compare_version,
            warning=manager_module.logger.warning,
            error=manager_module.logger.error,
        )

    monkeypatch.setattr(manager_module, "_plugin_catalog_factory", build_catalog)


class _FakeResponse:
    """模拟 requests/httpx 响应对象，覆盖插件 release 安装分支读取的最小协议。"""

    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        """返回构造时注入的 JSON payload。"""
        return self._payload


class _FakeContentResponse(_FakeResponse):
    """带二进制正文的响应对象，用于模拟 GitHub release asset 下载。"""

    def __init__(self, status_code: int, content: bytes):
        super().__init__(status_code)
        self.content = content


class _FakeTextResponse(_FakeResponse):
    """带文本正文的响应对象，用于模拟 GitHub release 列表响应。"""

    def __init__(self, status_code: int, payload: list[dict] | dict):
        super().__init__(status_code, payload if isinstance(payload, dict) else {})
        self._payload = payload

    def json(self):
        """返回构造时注入的 JSON payload。"""
        return self._payload


def _build_zip(entries: dict[str, bytes]) -> bytes:
    """构造内存 zip 包，键为包内路径、值为文件内容。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buffer.getvalue()


def _build_release_zip_member(name: str, *, symlink: bool = False) -> bytes:
    """构造单成员 release zip，用于覆盖成员路径与文件类型校验。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        if symlink:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, b"target")
        else:
            zf.writestr(name, b"evil")
    return buffer.getvalue()


def _create_fake_uv(root: Path) -> Path:
    """创建仅供命令构造测试定位的 uv 可执行文件。"""
    uv_bin = root / "venv" / "bin" / "uv"
    uv_bin.parent.mkdir(parents=True, exist_ok=True)
    uv_bin.write_text("", encoding="utf-8")
    return uv_bin


def _patch_release_install_settings(monkeypatch, tmp_path: Path) -> None:
    """隔离 release 安装根目录，并阻止测试误触真实根路径。"""
    monkeypatch.setattr("app.adapters.external.market.settings", SimpleNamespace(
        ROOT_PATH=tmp_path,
        REPO_GITHUB_HEADERS=lambda repo=None: {},
    ))

    original_mkdir = Path.mkdir
    safe_root = tmp_path.resolve()

    def guarded_mkdir(path: Path, *args, **kwargs):
        try:
            path.resolve().relative_to(safe_root)
        except ValueError as exc:
            raise AssertionError(f"unsafe mkdir attempted: {path}") from exc
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", guarded_mkdir)


def _patch_sync_remote_install(helper, monkeypatch, meta: dict,
                               release_result: tuple[bool, str],
                               filelist_result: tuple[bool, str] = (True, "")):
    """隔离同步远端插件安装流程，只观察 release 与文件列表准备路径选择。"""
    calls = []
    monkeypatch.setattr(helper, "get_plugin_package_version", lambda *_args: "v2")
    monkeypatch.setattr(helper, "_PluginHelper__get_plugin_meta", lambda *_args: meta)
    monkeypatch.setattr(helper, "_PluginHelper__backup_plugin", lambda _pid: None)
    monkeypatch.setattr(helper, "_PluginHelper__remove_old_plugin", lambda _pid: calls.append("remove"))
    monkeypatch.setattr(helper, "_PluginHelper__install_dependencies_if_required", lambda _pid: (False, True, ""))
    monkeypatch.setattr(helper, "refresh_persistent_plugin_backup", lambda _pid: calls.append("refresh"))

    def fake_release(_pid, _user_repo, _release_tag):
        calls.append("release")
        return release_result

    def fake_filelist(_pid, _user_repo, _package_version):
        calls.append("filelist")
        return filelist_result

    monkeypatch.setattr(helper, "_PluginHelper__install_from_release", fake_release)
    monkeypatch.setattr(helper, "_PluginHelper__prepare_content_via_filelist_sync", fake_filelist)
    return calls


def _patch_async_remote_install(helper, monkeypatch, meta: dict,
                                release_result: tuple[bool, str],
                                filelist_result: tuple[bool, str] = (True, "")):
    """隔离异步远端插件安装流程，只观察 release 与文件列表准备路径选择。"""
    calls = []

    async def fake_package_version(*_args):
        return "v2"

    async def fake_meta(*_args):
        return meta

    async def fake_backup(_pid):
        return None

    async def fake_remove(_pid):
        calls.append("remove")

    async def fake_dependencies(_pid):
        return False, True, ""

    async def fake_release(_pid, _user_repo, _release_tag):
        calls.append("release")
        return release_result

    async def fake_filelist(_pid, _user_repo, _package_version):
        calls.append("filelist")
        return filelist_result

    async def fake_to_thread(func, *args, **kwargs):
        calls.append(("to_thread", func, args, kwargs))
        return None

    monkeypatch.setattr(helper, "async_get_plugin_package_version", fake_package_version)
    monkeypatch.setattr(helper, "_PluginHelper__async_get_plugin_meta", fake_meta)
    monkeypatch.setattr(helper, "_PluginHelper__async_backup_plugin", fake_backup)
    monkeypatch.setattr(helper, "_PluginHelper__async_remove_old_plugin", fake_remove)
    monkeypatch.setattr(helper, "_PluginHelper__async_install_dependencies_if_required", fake_dependencies)
    monkeypatch.setattr(helper, "_PluginHelper__async_install_from_release", fake_release)
    monkeypatch.setattr(helper, "_PluginHelper__prepare_content_via_filelist_async", fake_filelist)
    monkeypatch.setattr("app.adapters.external.market.asyncio.to_thread", fake_to_thread)
    return calls


class TestPluginHelper:

    def test_sanitize_plugin_repo_url_keeps_remote_url(self):
        """
        插件安装统计脱敏保留远端仓库地址。
        """
        try:
            from app.adapters.external.server import MoviePilotServerHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")
        repo_url = "https://github.com/InfinityPacer/MoviePilot-Plugins"
        assert repo_url == MoviePilotServerHelper.sanitize_plugin_repo_url(repo_url)

    def test_sanitize_plugin_repo_url_strips_local_path(self):
        """
        插件安装统计脱敏移除本地仓库绝对路径。
        """
        try:
            from app.adapters.external.server import MoviePilotServerHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")
        repo_url = "local://TestPlugin?path=/Users/InfinityPacer/GitHub/MoviePilot/MoviePilot-Plugins&version=v2"
        assert "local://TestPlugin?version=v2" == MoviePilotServerHelper.sanitize_plugin_repo_url(repo_url)

    def test_append_cache_buster_only_during_fresh_context(self):
        """
        插件库强制刷新时远端索引 URL 也要变化，避免命中镜像或代理缓存。
        """
        try:
            from app.runtime.cache import fresh
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        url = "https://raw.githubusercontent.com/user/repo/main/package.json"

        assert url == PluginHelper._PluginHelper__append_cache_buster(url)
        with patch("app.adapters.external.market.time.time_ns", return_value=1234567890):
            with fresh(True):
                refreshed_url = PluginHelper._PluginHelper__append_cache_buster(url)

        assert "https://raw.githubusercontent.com/user/repo/main/package.json?_refresh=1234567890" == refreshed_url

    def test_check_plugin_system_version_allows_missing_field(self):
        """
        未声明主系统版本范围时保持旧插件兼容，不做额外限制。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        success, message = PluginHelper.check_plugin_system_version({"version": "1.0.0"})

        assert success
        assert "" == message

    def test_check_plugin_system_version_rejects_out_of_range(self):
        """
        插件声明的主系统版本范围不满足当前版本时拒绝安装。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        with patch.object(PluginHelper, "get_current_system_version", return_value=Version("2.12.2")):
            success, message = PluginHelper.check_plugin_system_version({"system_version": ">=2.13.0"})

        assert not success
        assert "MoviePilot 版本 >=2.13.0" in message

    def test_check_plugin_system_version_accepts_v_prefix_specifier(self):
        """
        兼容带 v 前缀的版本范围，降低插件索引维护成本。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        with patch.object(PluginHelper, "get_current_system_version", return_value=Version("2.12.2")):
            success, message = PluginHelper.check_plugin_system_version({"system_version": ">=v2.12.0"})

        assert success
        assert "" == message

    def test_get_plugin_release_versions_keeps_only_matching_zip_assets(self, monkeypatch):
        """
        release 版本列表只暴露符合插件 tag 规范且存在同名 zip 资产的版本。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        payload = [
            {
                "tag_name": "DemoPlugin_v1.2.3",
                "name": "DemoPlugin v1.2.3",
                "published_at": "2026-06-01T00:00:00Z",
                "body": "稳定版本",
                "assets": [{"name": "demoplugin_v1.2.3.zip", "id": 1}],
            },
            {
                "tag_name": "DemoPlugin_v1.2.2",
                "name": "missing asset",
                "assets": [{"name": "other.zip", "id": 2}],
            },
            {
                "tag_name": "OtherPlugin_v9.9.9",
                "name": "other plugin",
                "assets": [{"name": "otherplugin_v9.9.9.zip", "id": 3}],
            },
        ]
        helper = PluginHelper()
        monkeypatch.setattr(
            helper,
            "_PluginHelper__request_with_fallback",
            lambda *_args, **_kwargs: _FakeTextResponse(200, payload),
        )

        releases = helper.get_plugin_release_versions(PLUGIN_ID, REPO_URL)

        assert releases == [
            {
                "version": "1.2.3",
                "tag_name": "DemoPlugin_v1.2.3",
                "name": "DemoPlugin v1.2.3",
                "published_at": "2026-06-01T00:00:00Z",
                "body": "稳定版本",
                "asset_name": "demoplugin_v1.2.3.zip",
            }
        ]

    def test_get_plugin_release_versions_uses_cache_buster_during_fresh_context(self, monkeypatch):
        """
        插件市场强制刷新时 Release 列表请求也要绕过 GitHub 镜像或代理缓存。
        """
        try:
            from app.runtime.cache import fresh
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        requested_urls = []

        def fake_request(url, **_kwargs):
            requested_urls.append(url)
            return _FakeTextResponse(200, [])

        helper = PluginHelper()
        helper.get_plugin_release_versions.cache_clear()
        monkeypatch.setattr(helper, "_PluginHelper__request_with_fallback", fake_request)

        with patch("app.adapters.external.market.time.time_ns", return_value=1234567890):
            with fresh(True):
                helper.get_plugin_release_versions(PLUGIN_ID, REPO_URL)

        assert requested_urls == [
            "https://api.github.com/repos/demo/MoviePilot-Plugins/releases?per_page=100&page=1&_refresh=1234567890"
        ]

    def test_get_plugin_release_versions_fetches_multiple_pages(self, monkeypatch):
        """
        多插件共用 Release 列表时需要分页，避免目标插件历史发行版被第一页之外的数据遮蔽。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        payload_by_page = {
            "1": [
                {
                    "tag_name": f"OtherPlugin_v9.9.{index}",
                    "assets": [{"name": f"otherplugin_v9.9.{index}.zip", "id": index}],
                }
                for index in range(100)
            ],
            "2": [{"tag_name": "DemoPlugin_v1.2.0", "assets": [{"name": "demoplugin_v1.2.0.zip", "id": 2}]}],
        }
        requested_pages = []

        def fake_request(url, **_kwargs):
            page = url.rsplit("page=", 1)[1].split("&", 1)[0]
            requested_pages.append(page)
            return _FakeTextResponse(200, payload_by_page[page])

        helper = PluginHelper()
        helper.get_plugin_release_versions.cache_clear()
        monkeypatch.setattr(helper, "_PluginHelper__request_with_fallback", fake_request)

        releases = helper.get_plugin_release_versions(PLUGIN_ID, REPO_URL)

        assert requested_pages == ["1", "2"]
        assert [item["version"] for item in releases] == ["1.2.0"]

    def test_get_plugin_release_versions_reuses_repository_pages_across_plugins(self, monkeypatch):
        """
        同一仓库的不同插件共享 GitHub Release 分页结果，避免按插件 ID 重复请求。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        payload = [
            {
                "tag_name": "DemoPlugin_v1.2.3",
                "assets": [{"name": "demoplugin_v1.2.3.zip", "id": 1}],
            },
            {
                "tag_name": "OtherPlugin_v2.0.0",
                "assets": [{"name": "otherplugin_v2.0.0.zip", "id": 2}],
            },
        ]
        request_count = 0

        def fake_request(*_args, **_kwargs):
            nonlocal request_count
            request_count += 1
            return _FakeTextResponse(200, payload)

        helper = PluginHelper()
        helper.get_plugin_release_versions.cache_clear()
        monkeypatch.setattr(helper, "_PluginHelper__request_with_fallback", fake_request)

        demo_releases = helper.get_plugin_release_versions("DemoPlugin", REPO_URL)
        other_releases = helper.get_plugin_release_versions("OtherPlugin", REPO_URL)

        assert request_count == 1
        assert [item["version"] for item in demo_releases] == ["1.2.3"]
        assert [item["version"] for item in other_releases] == ["2.0.0"]

    def test_async_get_plugin_release_versions_coalesces_forced_repository_requests(self, monkeypatch):
        """
        同一仓库的并发强制刷新共享一个请求任务，避免缓存失效瞬间放大 GitHub 请求。
        """
        try:
            from app.runtime.cache import async_fresh
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        payload = [
            {
                "tag_name": "DemoPlugin_v1.2.3",
                "assets": [{"name": "demoplugin_v1.2.3.zip", "id": 1}],
            },
            {
                "tag_name": "OtherPlugin_v2.0.0",
                "assets": [{"name": "otherplugin_v2.0.0.zip", "id": 2}],
            },
        ]
        request_count = 0

        async def fake_request(*_args, **_kwargs):
            nonlocal request_count
            request_count += 1
            await asyncio.sleep(0.01)
            return _FakeTextResponse(200, payload)

        async def run_test():
            helper = PluginHelper()
            await helper.async_get_plugin_release_versions.cache_clear()
            monkeypatch.setattr(helper, "_PluginHelper__async_request_with_fallback", fake_request)
            async with async_fresh(True):
                return await asyncio.gather(
                    helper.async_get_plugin_release_versions("DemoPlugin", REPO_URL),
                    helper.async_get_plugin_release_versions("OtherPlugin", REPO_URL),
                )

        demo_releases, other_releases = asyncio.run(run_test())

        assert request_count == 1
        assert [item["version"] for item in demo_releases] == ["1.2.3"]
        assert [item["version"] for item in other_releases] == ["2.0.0"]

    def test_async_forced_release_refresh_does_not_reuse_normal_read_task(self, monkeypatch):
        """强刷等待在途普通读取后再请求，最终缓存必须保留强刷结果。"""
        try:
            from app.runtime.cache import async_fresh
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        old_payload = [{
            "tag_name": "DemoPlugin_v1.2.2",
            "assets": [{"name": "demoplugin_v1.2.2.zip", "id": 1}],
        }]
        fresh_payload = [{
            "tag_name": "DemoPlugin_v1.2.3",
            "assets": [{"name": "demoplugin_v1.2.3.zip", "id": 2}],
        }]
        first_request_started = asyncio.Event()
        release_first_request = asyncio.Event()
        request_count = 0

        async def fake_request(*_args, **_kwargs):
            nonlocal request_count
            request_count += 1
            if request_count == 1:
                first_request_started.set()
                await release_first_request.wait()
                return _FakeTextResponse(200, old_payload)
            return _FakeTextResponse(200, fresh_payload)

        async def run_test():
            helper = PluginHelper()
            await helper.async_get_plugin_release_versions.cache_clear()
            monkeypatch.setattr(helper, "_PluginHelper__async_request_with_fallback", fake_request)
            normal_task = asyncio.create_task(
                helper.async_get_plugin_release_versions("DemoPlugin", REPO_URL)
            )
            await first_request_started.wait()
            async with async_fresh(True):
                force_task = asyncio.create_task(
                    helper.async_get_plugin_release_versions("DemoPlugin", REPO_URL)
                )
                await asyncio.sleep(0.01)
                request_count_before_normal_finished = request_count
            release_first_request.set()
            normal_result, force_result = await asyncio.gather(normal_task, force_task)
            cached_result = await helper.async_get_plugin_release_versions("DemoPlugin", REPO_URL)
            return request_count_before_normal_finished, normal_result, force_result, cached_result

        request_count_before_normal_finished, normal_result, force_result, cached_result = asyncio.run(run_test())

        assert request_count_before_normal_finished == 1
        assert [item["version"] for item in normal_result] == ["1.2.2"]
        assert [item["version"] for item in force_result] == ["1.2.3"]
        assert [item["version"] for item in cached_result] == ["1.2.3"]
        assert request_count == 2

    def test_async_normal_release_read_does_not_wait_for_pending_force_refresh(self, monkeypatch):
        """普通读取遇到后台强刷时仍优先返回已有缓存，避免页面响应被强刷阻塞。"""
        try:
            from app.runtime.cache import async_fresh
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        old_payload = [{
            "tag_name": "DemoPlugin_v1.2.2",
            "assets": [{"name": "demoplugin_v1.2.2.zip", "id": 1}],
        }]
        fresh_payload = [{
            "tag_name": "DemoPlugin_v1.2.3",
            "assets": [{"name": "demoplugin_v1.2.3.zip", "id": 2}],
        }]
        force_request_started = asyncio.Event()
        release_force_request = asyncio.Event()
        request_count = 0

        async def fake_request(*_args, **_kwargs):
            nonlocal request_count
            request_count += 1
            if request_count == 1:
                return _FakeTextResponse(200, old_payload)
            force_request_started.set()
            await release_force_request.wait()
            return _FakeTextResponse(200, fresh_payload)

        async def run_test():
            helper = PluginHelper()
            await helper.async_get_plugin_release_versions.cache_clear()
            monkeypatch.setattr(helper, "_PluginHelper__async_request_with_fallback", fake_request)
            initial = await helper.async_get_plugin_release_versions("DemoPlugin", REPO_URL)
            async with async_fresh(True):
                force_task = asyncio.create_task(
                    helper.async_get_plugin_release_versions("DemoPlugin", REPO_URL)
                )
            await force_request_started.wait()
            normal_task = asyncio.create_task(
                helper.async_get_plugin_release_versions("DemoPlugin", REPO_URL)
            )
            normal_before_force_finished = await asyncio.wait_for(normal_task, timeout=1)
            force_done_before_normal_finished = force_task.done()
            release_force_request.set()
            force_result = await force_task
            cached_result = await helper.async_get_plugin_release_versions("DemoPlugin", REPO_URL)
            return (
                initial,
                force_done_before_normal_finished,
                normal_before_force_finished,
                force_result,
                cached_result,
            )

        (
            initial,
            force_done_before_normal_finished,
            normal_before_force_finished,
            force_result,
            cached_result,
        ) = asyncio.run(run_test())

        assert [item["version"] for item in initial] == ["1.2.2"]
        assert force_done_before_normal_finished is False
        assert [item["version"] for item in normal_before_force_finished] == ["1.2.2"]
        assert [item["version"] for item in force_result] == ["1.2.3"]
        assert [item["version"] for item in cached_result] == ["1.2.3"]
        assert request_count == 2

    def test_async_has_plugin_release_cache_reflects_repository_cache(self, monkeypatch):
        """Release 缓存探针只判断仓库级缓存是否已经存在，不触发网络请求。"""
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        payload = [{
            "tag_name": "DemoPlugin_v1.2.3",
            "assets": [{"name": "demoplugin_v1.2.3.zip", "id": 1}],
        }]
        request_count = 0

        async def fake_request(*_args, **_kwargs):
            nonlocal request_count
            request_count += 1
            return _FakeTextResponse(200, payload)

        async def run_test():
            helper = PluginHelper()
            await helper.async_get_plugin_release_versions.cache_clear()
            monkeypatch.setattr(helper, "_PluginHelper__async_request_with_fallback", fake_request)
            before = await helper.async_has_plugin_release_cache(REPO_URL)
            await helper.async_get_plugin_release_versions("DemoPlugin", REPO_URL)
            after = await helper.async_has_plugin_release_cache(REPO_URL)
            return before, after

        before, after = asyncio.run(run_test())

        assert before is False
        assert after is True
        assert request_count == 1

    def test_failed_forced_release_refresh_preserves_cached_repository_payload(self, monkeypatch):
        """GitHub 强刷失败时不以空值覆盖该仓库已有 Release 缓存。"""
        try:
            from app.runtime.cache import fresh
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        payload = [{
            "tag_name": "DemoPlugin_v1.2.3",
            "assets": [{"name": "demoplugin_v1.2.3.zip", "id": 1}],
        }]
        responses = [_FakeTextResponse(200, payload), None]

        def fake_request(*_args, **_kwargs):
            return responses.pop(0)

        helper = PluginHelper()
        helper.get_plugin_release_versions.cache_clear()
        monkeypatch.setattr(helper, "_PluginHelper__request_with_fallback", fake_request)

        initial = helper.get_plugin_release_versions("DemoPlugin", REPO_URL)
        with fresh(True):
            failed_refresh = helper.get_plugin_release_versions("DemoPlugin", REPO_URL)
        cached = helper.get_plugin_release_versions("DemoPlugin", REPO_URL)

        assert [item["version"] for item in initial] == ["1.2.3"]
        assert failed_refresh == []
        assert [item["version"] for item in cached] == ["1.2.3"]
        assert responses == []

    def test_get_plugins_from_market_normalizes_list_labels(self, monkeypatch) -> None:
        """
        插件市场 labels 为列表时应转换为字符串，避免响应模型序列化异常。
        """
        try:
            from app.runtime.extensions.plugin_manager import PluginManager
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        market_plugins = {
            "DemoPlugin": {
                "name": "Demo Plugin",
                "description": "Demo",
                "version": "1.0.0",
                "labels": ["站点", "通知", ""],
                "v2": True,
            }
        }
        plugin_manager = PluginManager()
        monkeypatch.setattr(plugin_manager, "_plugins", {})
        monkeypatch.setattr(plugin_manager, "_running_plugins", {})
        monkeypatch.setattr("app.runtime.extensions.plugin_manager.settings", SimpleNamespace(VERSION_FLAG="v2"))
        monkeypatch.setattr(
            "app.runtime.extensions.plugin_manager.get_plugin_storage",
            lambda: SimpleNamespace(read=lambda _key: []),
        )
        monkeypatch.setattr(
            "app.runtime.extensions.plugin_manager._site_auth_level_provider",
            lambda: 1,
        )
        monkeypatch.setattr(PluginHelper, "get_plugins", lambda _self, *_args: market_plugins)

        plugins = plugin_manager.get_plugins_from_market(REPO_URL)

        assert len(plugins) == 1
        assert plugins[0].plugin_label == "站点 通知"
        assert plugins[0].model_dump()["plugin_label"] == "站点 通知"

    def test_get_online_plugins_includes_backward_compatible_v2_plugins(self, monkeypatch) -> None:
        """
        V3 升级后插件市场不应空白：需同时展示 package.json 中声明 v2 兼容的插件、
        package.v2.json 中的 v2 原生插件，并过滤掉未声明任何版本兼容的 v1 插件。
        """
        try:
            from app.runtime.extensions.plugin_manager import PluginManager
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        base_plugins = {
            "V2FlagPlugin": {"name": "V2Flag", "version": "1.0.0", "v2": True, "level": 1},
            "RejectedSharedPlugin": {
                "name": "Rejected shared",
                "version": "1.0.0",
                "v2": True,
                "v3": False,
                "level": 1,
            },
            "LegacyPlugin": {"name": "Legacy", "version": "1.0.0", "level": 1},
        }
        v2_native_plugins = {
            "V2NativePlugin": {"name": "V2Native", "version": "1.0.0", "level": 1},
            "RejectedV2Plugin": {
                "name": "Rejected V2",
                "version": "1.0.0",
                "v3": False,
                "level": 1,
            },
        }

        def fake_get_plugins(_self, _repo_url, package_version=None):
            # package.v3.json 不存在（404 → 空字典），package.v2.json 返回 v2 原生插件
            if package_version == "v3":
                return {}
            if package_version == "v2":
                return v2_native_plugins
            return base_plugins

        plugin_manager = PluginManager()
        monkeypatch.setattr(plugin_manager, "_plugins", {})
        monkeypatch.setattr(plugin_manager, "_running_plugins", {})
        monkeypatch.setattr(
            "app.runtime.extensions.plugin_manager.settings",
            SimpleNamespace(VERSION_FLAG="v3", PLUGIN_MARKET=REPO_URL),
        )
        monkeypatch.setattr("app.adapters.external.market.settings", SimpleNamespace(VERSION_FLAG="v3"))
        monkeypatch.setattr(
            "app.runtime.extensions.plugin_manager.get_plugin_storage",
            lambda: SimpleNamespace(read=lambda _key: []),
        )
        monkeypatch.setattr(
            "app.runtime.extensions.plugin_manager._site_auth_level_provider",
            lambda: 1,
        )
        monkeypatch.setattr(PluginHelper, "get_plugins", fake_get_plugins)

        plugins = plugin_manager.get_online_plugins(force=False)
        plugin_ids = {p.id for p in plugins}

        assert "V2FlagPlugin" in plugin_ids
        assert "V2NativePlugin" in plugin_ids
        assert "RejectedSharedPlugin" not in plugin_ids
        assert "RejectedV2Plugin" not in plugin_ids
        assert "LegacyPlugin" not in plugin_ids

    def test_get_plugin_package_version_resolves_backward_compatible_v2_sources(self, monkeypatch) -> None:
        """
        V3 安装链路应能解析 v2 兼容插件：package.v2.json 命中返回 v2，package.json 声明 v2 返回基础版本。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        base_plugins = {
            "V2FlagPlugin": {"name": "V2Flag", "version": "1.0.0", "v2": True},
            "RejectedSharedPlugin": {
                "name": "Rejected shared",
                "version": "1.0.0",
                "v2": True,
                "v3": False,
            },
            "LegacyPlugin": {"name": "Legacy", "version": "1.0.0"},
        }
        v2_native_plugins = {
            "V2NativePlugin": {"name": "V2Native", "version": "1.0.0"},
            "RejectedV2Plugin": {
                "name": "Rejected V2",
                "version": "1.0.0",
                "v3": False,
            },
        }

        def fake_get_plugins(_self, _repo_url, package_version=None):
            if package_version == "v3":
                return {}
            if package_version == "v2":
                return v2_native_plugins
            return base_plugins

        monkeypatch.setattr("app.adapters.external.market.settings", SimpleNamespace(VERSION_FLAG="v3"))
        helper = PluginHelper.__new__(PluginHelper)
        monkeypatch.setattr(PluginHelper, "get_plugins", fake_get_plugins)

        assert helper.get_plugin_package_version("V2NativePlugin", REPO_URL) == "v2"
        assert helper.get_plugin_package_version("V2FlagPlugin", REPO_URL) == ""
        assert helper.get_plugin_package_version("RejectedV2Plugin", REPO_URL) is None
        assert helper.get_plugin_package_version("RejectedSharedPlugin", REPO_URL) is None
        assert helper.get_plugin_package_version("LegacyPlugin", REPO_URL) is None

    def test_explicit_v2_resolution_still_respects_v3_false(self, monkeypatch) -> None:
        """V3 显式解析 V2 索引时也不得绕过专用副本的排除标志。"""
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        monkeypatch.setattr(
            "app.adapters.external.market.settings",
            SimpleNamespace(VERSION_FLAG="v3"),
        )
        helper = PluginHelper.__new__(PluginHelper)
        monkeypatch.setattr(
            PluginHelper,
            "get_plugins",
            lambda _self, _repo, package_version=None: {
                "DefaultV2": {"version": "1.0.0"},
                "V3Copied": {"version": "1.0.0", "v3": False},
            } if package_version == "v2" else {},
        )

        assert helper.get_plugin_package_version(
            "DefaultV2", REPO_URL, package_version="v2"
        ) == "v2"
        assert helper.get_plugin_package_version(
            "V3Copied", REPO_URL, package_version="v2"
        ) is None

    def test_async_resolution_matches_v2_default_compatibility(self, monkeypatch) -> None:
        """异步安装解析应默认接纳 V2，并排除显式 v3:false 的旧实现。"""
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        monkeypatch.setattr(
            "app.adapters.external.market.settings",
            SimpleNamespace(VERSION_FLAG="v3"),
        )
        helper = PluginHelper.__new__(PluginHelper)

        async def fake_get_plugins(_repo, package_version=None):
            """按索引版本返回异步解析测试数据。"""
            if package_version == "v3":
                return {}
            if package_version == "v2":
                return {
                    "DefaultV2": {"version": "1.0.0"},
                    "V3Copied": {"version": "1.0.0", "v3": False},
                }
            return {"SharedV2": {"version": "1.0.0", "v2": True}}

        monkeypatch.setattr(helper, "async_get_plugins", fake_get_plugins)

        assert asyncio.run(helper.async_get_plugin_package_version(
            "DefaultV2", REPO_URL
        )) == "v2"
        assert asyncio.run(helper.async_get_plugin_package_version(
            "SharedV2", REPO_URL
        )) == ""
        assert asyncio.run(helper.async_get_plugin_package_version(
            "V3Copied", REPO_URL
        )) is None

    def test_v3_package_compatibility_defaults_v2_to_allowed(self, monkeypatch) -> None:
        """V3 临时兼容 V2，显式 false 优先拒绝且纯 V1 不被带入。"""
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        monkeypatch.setattr(
            "app.adapters.external.market.settings",
            SimpleNamespace(VERSION_FLAG="v3"),
        )

        assert PluginHelper.is_package_plugin_compatible({}, "v3")
        assert PluginHelper.is_package_plugin_compatible({}, "v2")
        assert not PluginHelper.is_package_plugin_compatible(
            {"v3": False}, "v2"
        )
        assert PluginHelper.is_package_plugin_compatible({"v2": True}, "")
        assert not PluginHelper.is_package_plugin_compatible(
            {"v2": True, "v3": False}, ""
        )
        assert not PluginHelper.is_package_plugin_compatible({}, "")

    def test_get_online_plugins_force_keeps_release_cache_scoped(self, monkeypatch):
        """
        全市场刷新不清理 Release 缓存，Release 接口按请求仓库协调刷新两类数据。
        """
        try:
            from app.runtime.extensions.plugin_manager import PluginManager
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        clear_calls = []
        monkeypatch.setattr("app.runtime.extensions.plugin_manager.settings.PLUGIN_MARKET", "https://github.com/demo/plugins")
        monkeypatch.setattr(PluginManager, "get_plugins_from_market", lambda *_args, **_kwargs: [])

        PluginManager().get_online_plugins(force=True)

        assert clear_calls == []

    def test_async_get_online_plugins_force_keeps_release_cache_scoped(self, monkeypatch):
        """异步全市场刷新同样不得清理其他仓库的 Release 缓存。"""
        try:
            from app.runtime.extensions.plugin_manager import PluginManager
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        clear_calls = []

        async def fake_clear():
            clear_calls.append("clear")

        async def fake_market(*_args, **_kwargs):
            return []

        monkeypatch.setattr("app.runtime.extensions.plugin_manager.settings.PLUGIN_MARKET", "https://github.com/demo/plugins")
        monkeypatch.setattr(PluginManager, "async_get_plugins_from_market", fake_market)

        asyncio.run(PluginManager().async_get_online_plugins(force=True))

        assert clear_calls == []

    def test_get_local_plugin_version_reads_only_requested_installed_plugin(self, monkeypatch):
        """单插件版本查询不构建全部本地插件信息。"""
        try:
            from app.runtime.extensions.plugin_manager import PluginManager
            from app.schemas.types import SystemConfigKey
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        class DemoPlugin:
            plugin_version = "1.2.0"

        plugin_manager = PluginManager()
        monkeypatch.setattr(plugin_manager, "_plugins", {"DemoPlugin": DemoPlugin})
        monkeypatch.setattr(
            "app.runtime.extensions.plugin_manager.get_plugin_storage",
            lambda: SimpleNamespace(
                read=lambda key: ["DemoPlugin"]
                if key == SystemConfigKey.UserInstalledPlugins
                else None
            ),
        )

        assert plugin_manager.get_local_plugin_version("DemoPlugin") == "1.2.0"
        assert plugin_manager.get_local_plugin_version("OtherPlugin") is None

    def test_annotate_plugin_system_version_marks_incompatible(self):
        """
        插件市场列表会带出系统版本兼容状态，供前端禁用安装入口。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        plugin_info = {"system_version": ">=2.13.0"}
        with patch.object(PluginHelper, "get_current_system_version", return_value=Version("2.12.2")):
            annotated = PluginHelper.annotate_plugin_system_version(plugin_info)

        assert not annotated["system_version_compatible"]
        assert "当前版本" in annotated["system_version_message"]

    def test_uv_install_keeps_modules_imported_during_install(self):
        """
        验证依赖安装窗口内被其他任务导入的运行态模块不会被误删。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        module_names = ["app.plugins.dynamicwechat.helper", "Crypto.Cipher._mode_cbc"]

        def fake_execute(_cmd, env=None, safe_command=None):
            for module_name in module_names:
                sys.modules[module_name] = ModuleType(module_name)
            return True, "ok"

        # patch.dict 进入时快照 sys.modules、退出时整体还原，替代手写逐项 save/restore；
        # 保证 fake_execute 在安装窗口注入的运行态模块在用例结束后被清理、不污染其他用例
        with patch.dict(sys.modules):
            with tempfile.TemporaryDirectory() as temp_dir:
                requirements_file = Path(temp_dir) / "requirements.txt"
                requirements_file.write_text("demo-package\n", encoding="utf-8")
                with patch("app.adapters.external.market.SystemUtils.execute_with_subprocess", side_effect=fake_execute):
                    success, message = PluginHelper.install_packages_with_fallback(requirements_file)

            assert success
            assert "ok" == message
            for module_name in module_names:
                assert module_name in sys.modules

    def test_uv_install_builds_uv_strategy_without_proxy_argument(self):
        """
        插件依赖安装优先使用 uv 时，传输代理只进入子进程环境。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        seen = []

        def fake_execute(command, env=None, safe_command=None):
            seen.append((command, env, safe_command))
            return True, "ok"

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            req = root / "requirements.txt"
            req.write_text("demo\n", encoding="utf-8")
            uv_bin = root / "venv" / "bin" / "uv"
            uv_bin.parent.mkdir(parents=True)
            uv_bin.write_text("", encoding="utf-8")

            with patch("app.adapters.system.package.find_uv", return_value=uv_bin), \
                    patch.object(PluginHelper, "_PluginHelper__get_protected_runtime_packages", return_value={}), \
                    patch.object(
                        PluginHelper,
                        "_PluginHelper__run_runtime_healthcheck",
                        return_value={"uv check": (True, "ok"), "核心依赖导入检查": (True, "ok")},
                    ), \
                    patch("app.adapters.external.market.SystemUtils.execute_with_subprocess", side_effect=fake_execute), \
                    patch("app.adapters.external.market.settings.PROXY_HOST", "http://proxy.example:7890"), \
                    patch("app.adapters.external.market.settings.PIP_PROXY", "https://user:pass@mirror.example/simple"):
                success, message = PluginHelper.install_packages_with_fallback(req)

        assert success
        assert message == "ok"
        assert seen
        command, env, safe_command = seen[0]
        assert command[:3] == [str(uv_bin), "pip", "install"]
        assert "--proxy" not in command
        assert env["HTTPS_PROXY"] == "http://proxy.example:7890"
        assert "user:pass" not in " ".join(safe_command)

    def test_uv_install_keeps_multiple_original_manifests_in_one_command(self):
        """批量恢复必须让 uv 直接读取每个插件的原始生效清单。"""
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        seen_commands = []

        def fake_execute(command, env=None, safe_command=None):
            seen_commands.append(command)
            return True, "ok"

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            modern = root / "modern" / "pyproject.toml"
            modern.parent.mkdir()
            modern.write_text(
                """
[project]
name = "modern"
version = "1.0.0"
dependencies = ["demo>=2"]

[[tool.uv.index]]
name = "private"
url = "https://packages.example/simple"
explicit = true

[tool.uv.sources]
demo = { index = "private" }
""",
                encoding="utf-8",
            )
            legacy = root / "legacy" / "requirements.txt"
            legacy.parent.mkdir()
            legacy.write_text(
                "--extra-index-url https://legacy.example/simple\nother\n",
                encoding="utf-8",
            )
            uv_bin = _create_fake_uv(root)

            with patch("app.adapters.system.package.find_uv", return_value=uv_bin), \
                    patch.object(PluginHelper, "_PluginHelper__get_installed_packages", return_value={}), \
                    patch.object(PluginHelper, "_PluginHelper__get_protected_runtime_packages", return_value={}), \
                    patch.object(
                        PluginHelper,
                        "_PluginHelper__run_runtime_healthcheck",
                        return_value={"uv check": (True, "ok"), "核心依赖导入检查": (True, "ok")},
                    ), \
                    patch("app.adapters.external.market.SystemUtils.execute_with_subprocess", side_effect=fake_execute):
                success, message = PluginHelper.install_packages_with_fallback(
                    [modern, legacy]
                )

        assert success
        assert message == "ok"
        install_command = next(
            command for command in seen_commands
            if command[:3] == [str(uv_bin), "pip", "install"]
        )
        requirement_positions = [
            index for index, value in enumerate(install_command) if value == "-r"
        ]
        assert [install_command[index + 1] for index in requirement_positions] == [
            str(modern),
            str(legacy),
        ]

    def test_uv_install_serializes_concurrent_calls(self):
        """
        验证多个依赖安装请求会复用同一把锁串行执行 uv。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        thread_count = 2
        active_installs = 0
        max_active_installs = 0
        state_lock = threading.Lock()
        start_event = threading.Event()
        errors = []

        def fake_execute(_cmd, env=None, safe_command=None):
            nonlocal active_installs, max_active_installs
            with state_lock:
                active_installs += 1
                max_active_installs = max(max_active_installs, active_installs)
            time.sleep(0.05)
            with state_lock:
                active_installs -= 1
            return True, "ok"

        def worker(requirements_file: Path):
            try:
                start_event.wait()
                PluginHelper.install_packages_with_fallback(requirements_file)
            except Exception as err:  # pragma: no cover - 仅用于并发测试失败诊断
                errors.append(err)

        with tempfile.TemporaryDirectory() as temp_dir:
            requirements_files = []
            for index in range(thread_count):
                requirements_file = Path(temp_dir) / f"requirements-{index}.txt"
                requirements_file.write_text("demo-package\n", encoding="utf-8")
                requirements_files.append(requirements_file)

            threads = [
                threading.Thread(target=worker, args=(requirements_file,))
                for requirements_file in requirements_files
            ]
            with patch("app.adapters.external.market.SystemUtils.execute_with_subprocess", side_effect=fake_execute):
                for thread in threads:
                    thread.start()
                start_event.set()
                for thread in threads:
                    thread.join()

        assert [] == errors
        assert 1 == max_active_installs

    def test_get_protected_runtime_packages_only_keeps_main_dependency_graph(self):
        """
        验证仅主程序依赖链上的包会被纳入保护集合。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        installed_packages = {
            "passlib": Version("1.7.4"),
            "bcrypt": Version("4.0.1"),
            "demo_package": Version("1.0"),
        }
        requirement_graph = {
            "passlib": (Version("1.7.4"), [Requirement("bcrypt>=4")]),
            "bcrypt": (Version("4.0.1"), []),
            "demo_package": (Version("1.0"), []),
        }

        with patch.object(
                PluginHelper,
                "_PluginHelper__parse_project_requirement_roots",
                return_value={"passlib": set()}
        ):
            with patch.object(
                    PluginHelper,
                    "_PluginHelper__get_installed_distribution_requirements",
                    return_value=requirement_graph
            ):
                protected_packages = PluginHelper._PluginHelper__get_protected_runtime_packages(installed_packages)

        assert {
            "passlib": Version("1.7.4"),
            "bcrypt": Version("4.0.1"),
        } == protected_packages

    def test_uv_install_rejects_conflicting_runtime_dependency(self):
        """
        验证插件如果试图覆盖主程序核心依赖，会在真正执行安装前被直接拒绝。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        with tempfile.TemporaryDirectory() as temp_dir:
            requirements_file = Path(temp_dir) / "requirements.txt"
            requirements_file.write_text("fastapi<0.1\n", encoding="utf-8")
            with patch.object(
                    PluginHelper,
                    "_PluginHelper__get_protected_runtime_packages",
                    return_value={"fastapi": Version("0.115.14")}
            ):
                success, message = PluginHelper.install_packages_with_fallback(requirements_file)

        assert not success
        assert "主程序核心依赖" in message
        assert "fastapi" in message

    def test_uv_install_allows_changing_non_runtime_dependency(self):
        """
        验证非主程序依赖即便已安装，插件后续仍可调整其版本约束。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        seen_install_commands = []

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            requirements_file = root / "requirements.txt"
            requirements_file.write_text("demo-package>=2\n", encoding="utf-8")
            uv_bin = _create_fake_uv(root)

            def fake_execute(cmd, env=None, safe_command=None):
                if cmd[:3] == [str(uv_bin), "pip", "install"]:
                    seen_install_commands.append(cmd)
                    assert "-c" not in cmd
                    return True, "ok"
                return True, "ok"

            with patch.object(
                    PluginHelper,
                    "_PluginHelper__get_installed_packages",
                    return_value={"demo_package": Version("1.0")}
            ):
                with patch.object(
                        PluginHelper,
                        "_PluginHelper__get_protected_runtime_packages",
                        return_value={}
                ):
                    with patch("app.adapters.external.market.SystemUtils.execute_with_subprocess", side_effect=fake_execute):
                        with patch("app.adapters.system.package.find_uv", return_value=uv_bin):
                            success, message = PluginHelper.install_packages_with_fallback(requirements_file)

        assert success
        assert "ok" == message
        assert 1 == len(seen_install_commands)

    def test_uv_install_uses_runtime_constraints_file(self):
        """
        验证插件依赖安装会固定主程序依赖的当前版本，防止共享 venv 被改写。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        seen_constraints = []

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            requirements_file = root / "requirements.txt"
            requirements_file.write_text("demo-package\n", encoding="utf-8")
            uv_bin = _create_fake_uv(root)

            def fake_execute(cmd, env=None, safe_command=None):
                if cmd[:3] == [str(uv_bin), "pip", "install"]:
                    constraint_index = cmd.index("-c") + 1
                    constraint_file = Path(cmd[constraint_index])
                    seen_constraints.append(constraint_file)
                    assert constraint_file.exists()
                    assert "fastapi==0.115.14" in constraint_file.read_text(encoding="utf-8")
                    return True, "ok"
                return True, "ok"

            with patch.object(
                    PluginHelper,
                    "_PluginHelper__get_protected_runtime_packages",
                    return_value={"fastapi": Version("0.115.14")}
            ):
                with patch("app.adapters.external.market.SystemUtils.execute_with_subprocess", side_effect=fake_execute):
                    with patch("app.adapters.system.package.find_uv", return_value=uv_bin):
                        success, message = PluginHelper.install_packages_with_fallback(requirements_file)

        assert success
        assert "ok" == message
        assert 1 == len(seen_constraints)
        assert not seen_constraints[0].exists()

    def test_uv_install_repairs_runtime_when_healthcheck_fails(self):
        """
        验证插件依赖安装后若破坏运行环境，会先恢复主程序依赖，再向上层返回失败。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        repair_commands = []
        uv_check_count = 0

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            requirements_file = root / "requirements.txt"
            requirements_file.write_text("demo-package\n", encoding="utf-8")
            uv_bin = _create_fake_uv(root)

            def fake_execute(cmd, env=None, safe_command=None):
                nonlocal uv_check_count
                if cmd[:3] == [str(uv_bin), "pip", "install"]:
                    if "-c" not in cmd:
                        repair_commands.append(cmd)
                        return True, "repaired"
                    return True, "installed"
                if cmd[1:3] == ["pip", "check"]:
                    uv_check_count += 1
                    if uv_check_count == 2:
                        return False, "broken"
                    return True, "healthy"
                if len(cmd) >= 3 and cmd[1] == "-c":
                    return True, "probe ok"
                raise AssertionError(f"unexpected command: {cmd}")

            with patch.object(
                    PluginHelper,
                    "_PluginHelper__get_protected_runtime_packages",
                    return_value={"fastapi": Version("0.115.14")}
            ):
                with patch("app.adapters.external.market.SystemUtils.execute_with_subprocess", side_effect=fake_execute):
                    with patch("app.adapters.system.package.find_uv", return_value=uv_bin):
                        success, message = PluginHelper.install_packages_with_fallback(requirements_file)

        assert not success
        assert "已自动恢复主程序依赖" in message
        assert 1 == len(repair_commands)
        assert "runtime-constraints-" in repair_commands[0][-1]

    def test_uv_install_allows_preexisting_healthcheck_failure(self):
        """
        安装前已存在且安装后未新增的环境异常不应归因于本次插件依赖安装。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        health_snapshots = [
            {
                "uv check": (False, "existing issue before install"),
                "核心依赖导入检查": (True, "ok"),
            },
            {
                "uv check": (False, "same issue with different command summary"),
                "核心依赖导入检查": (True, "ok"),
            },
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            requirements_file = root / "requirements.txt"
            requirements_file.write_text("demo-package\n", encoding="utf-8")
            uv_bin = _create_fake_uv(root)
            with patch("app.adapters.system.package.find_uv", return_value=uv_bin), \
                    patch.object(PluginHelper, "_PluginHelper__get_protected_runtime_packages", return_value={}), \
                    patch.object(
                        PluginHelper,
                        "_PluginHelper__run_runtime_healthcheck",
                        side_effect=health_snapshots,
                    ), \
                    patch.object(PluginHelper, "_PluginHelper__repair_main_runtime_dependencies") as repair_mock, \
                    patch(
                        "app.adapters.external.market.SystemUtils.execute_with_subprocess",
                        return_value=(True, "installed"),
                    ):
                success, message = PluginHelper.install_packages_with_fallback(requirements_file)

        assert success
        assert message == "installed"
        repair_mock.assert_not_called()

    def test_preexisting_healthcheck_failure_does_not_hide_new_core_failure(self):
        """
        既有全局依赖异常不能遮蔽本次安装新造成的核心依赖导入失败。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        health_snapshots = [
            {
                "uv check": (False, "existing issue"),
                "核心依赖导入检查": (True, "ok"),
            },
            {
                "uv check": (False, "existing issue"),
                "核心依赖导入检查": (False, "import failed"),
            },
            {
                "uv check": (False, "existing issue"),
                "核心依赖导入检查": (True, "ok"),
            },
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            requirements_file = root / "requirements.txt"
            requirements_file.write_text("demo-package\n", encoding="utf-8")
            uv_bin = _create_fake_uv(root)
            with patch("app.adapters.system.package.find_uv", return_value=uv_bin), \
                    patch.object(PluginHelper, "_PluginHelper__get_protected_runtime_packages", return_value={}), \
                    patch.object(
                        PluginHelper,
                        "_PluginHelper__run_runtime_healthcheck",
                        side_effect=health_snapshots,
                    ), \
                    patch.object(
                        PluginHelper,
                        "_PluginHelper__repair_main_runtime_dependencies",
                        return_value=(True, "repaired"),
                    ) as repair_mock, \
                    patch(
                        "app.adapters.external.market.SystemUtils.execute_with_subprocess",
                        return_value=(True, "installed"),
                    ):
                success, message = PluginHelper.install_packages_with_fallback(requirements_file)

        assert not success
        assert "核心依赖导入检查失败" in message
        repair_mock.assert_called_once()

    def test_failed_install_repairs_runtime_before_returning_error(self):
        """
        安装策略失败后如果主运行环境异常，应先恢复主程序依赖再返回失败。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        repair_calls = []

        def fake_execute(command, env=None, safe_command=None):
            if "install" in command and "-r" in command and "plugin" in str(command[-1]):
                return False, "partial failure"
            return True, "ok"

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            req = root / "plugin-requirements.txt"
            req.write_text("demo\n", encoding="utf-8")
            uv_bin = _create_fake_uv(root)

            with patch("app.adapters.system.package.find_uv", return_value=uv_bin), \
                    patch.object(PluginHelper, "_PluginHelper__get_protected_runtime_packages", return_value={}), \
                    patch.object(
                        PluginHelper,
                        "_PluginHelper__run_runtime_healthcheck",
                        side_effect=[
                            {"uv check": (True, "ok"), "核心依赖导入检查": (True, "ok")},
                            {"uv check": (False, "broken"), "核心依赖导入检查": (True, "ok")},
                            {"uv check": (True, "ok"), "核心依赖导入检查": (True, "ok")},
                        ],
                    ), \
                    patch.object(
                        PluginHelper,
                        "_PluginHelper__repair_main_runtime_dependencies",
                        side_effect=lambda snapshot_file=None: repair_calls.append(snapshot_file)
                        or (True, "runtime repaired"),
                    ), \
                    patch("app.adapters.external.market.SystemUtils.execute_with_subprocess", side_effect=fake_execute):
                success, message = PluginHelper.install_packages_with_fallback(req)

        assert not success
        assert "partial failure" in message or "恢复" in message
        assert repair_calls

    def test_failed_strategy_stops_after_runtime_repair_even_if_later_strategy_could_succeed(self):
        """
        一旦失败策略污染主运行环境并触发恢复，不能继续 fallback 后把安装结果伪装成成功。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        seen_install_commands = []
        repair_calls = []

        def fake_execute(command, env=None, safe_command=None):
            if "install" in command and "-r" in command:
                seen_install_commands.append(command)
                if len(seen_install_commands) == 1:
                    return False, "resolver failed"
                return True, "later success"
            return True, "ok"

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            req = root / "requirements.txt"
            req.write_text("demo\n", encoding="utf-8")
            uv_bin = root / "venv" / "bin" / "uv"
            uv_bin.parent.mkdir(parents=True)
            uv_bin.write_text("", encoding="utf-8")

            with patch("app.adapters.system.package.find_uv", return_value=uv_bin), \
                    patch.object(PluginHelper, "_PluginHelper__get_protected_runtime_packages", return_value={}), \
                    patch.object(
                        PluginHelper,
                        "_PluginHelper__run_runtime_healthcheck",
                        side_effect=[
                            {"uv check": (True, "ok"), "核心依赖导入检查": (True, "ok")},
                            {"uv check": (False, "broken"), "核心依赖导入检查": (True, "ok")},
                            {"uv check": (True, "ok"), "核心依赖导入检查": (True, "ok")},
                        ],
                    ), \
                    patch.object(
                        PluginHelper,
                        "_PluginHelper__repair_main_runtime_dependencies",
                        side_effect=lambda snapshot_file=None: repair_calls.append(snapshot_file)
                        or (True, "runtime repaired"),
                    ), \
                    patch("app.adapters.external.market.settings.PIP_PROXY", "https://mirror.example/simple"), \
                    patch("app.adapters.external.market.settings.PROXY_HOST", "http://proxy.example:7890"), \
                    patch("app.adapters.external.market.SystemUtils.execute_with_subprocess", side_effect=fake_execute):
                success, message = PluginHelper.install_packages_with_fallback(req)

        assert not success
        assert "resolver failed" in message
        assert "主运行环境已恢复" in message
        assert len(seen_install_commands) == 1
        assert repair_calls

    def test_repair_main_runtime_dependencies_uses_package_helper_semantics(self):
        """
        主运行环境恢复与插件安装使用同一套 cache、index、proxy 和安全日志语义。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        seen = []

        def fake_execute(command, env=None, safe_command=None):
            seen.append((command, env, safe_command))
            return True, "ok"

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            req = root / "requirements.txt"
            req.write_text("fastapi==1.0\n", encoding="utf-8")
            uv_bin = root / "venv" / "bin" / "uv"
            uv_bin.parent.mkdir(parents=True)
            uv_bin.write_text("", encoding="utf-8")

            with patch("app.adapters.system.package.find_uv", return_value=uv_bin), \
                    patch.dict(os.environ, {}, clear=True), \
                    patch("app.adapters.external.market.settings.CONFIG_DIR", str(root / "config")), \
                    patch("app.adapters.external.market.settings.PACKAGE_CACHE_ROOT", str(root / "custom-package-cache")), \
                    patch("app.adapters.external.market.settings.PIP_PROXY", "https://user:pass@mirror.example/simple"), \
                    patch("app.adapters.external.market.settings.PROXY_HOST", "http://proxy.example:7890"), \
                    patch("app.adapters.external.market.SystemUtils.execute_with_subprocess", side_effect=fake_execute):
                success, message = PluginHelper._PluginHelper__repair_main_runtime_dependencies(req)

        assert success
        assert message == "ok"
        assert seen
        command, env, safe_command = seen[0]
        assert command[:3] == [str(uv_bin), "pip", "install"]
        assert "--proxy" not in command
        assert env["PACKAGE_CACHE_ROOT"] == str(root / "custom-package-cache")
        assert env["UV_CACHE_DIR"] == str(root / "custom-package-cache" / "uv")
        assert env["HTTPS_PROXY"] == "http://proxy.example:7890"
        assert "user:pass" not in " ".join(safe_command)

    def test_async_package_install_uses_cancellable_subprocess(self):
        """异步依赖安装应直接使用可取消的子进程执行器。"""
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        with tempfile.TemporaryDirectory() as temp_dir:
            requirements_file = Path(temp_dir) / "demo-requirements.txt"
            requirements_file.write_text("demo-package\n", encoding="utf-8")
            find_links_dirs = [Path(temp_dir) / "wheels"]

            async def run_install():
                return await helper._PluginHelper__async_install_packages_with_fallback(
                    requirements_file,
                    find_links_dirs,
                )

            health = {
                "uv check": (True, "ok"),
                "核心依赖导入检查": (True, "ok"),
            }
            strategy = Mock(
                strategy_name="uv:test",
                command=["uv", "pip", "install"],
                env={},
                safe_log_command=["uv", "pip", "install"],
            )

            with patch.object(
                    PluginHelper,
                    "_PluginHelper__get_installed_packages",
                    return_value={},
            ), patch.object(
                    PluginHelper,
                    "_PluginHelper__get_protected_runtime_packages",
                    return_value={},
            ), patch.object(
                    PluginHelper,
                    "_PluginHelper__validate_runtime_dependency_conflicts",
                    return_value=(True, ""),
            ), patch(
                    "app.adapters.external.market.build_package_install_strategies",
                    return_value=[strategy],
            ), patch.object(
                    PluginHelper,
                    "_PluginHelper__async_run_runtime_healthcheck",
                    side_effect=[health, health],
            ), patch.object(
                    PluginHelper,
                    "_PluginHelper__refresh_import_system",
            ), patch(
                    "app.adapters.external.market.SystemUtils.execute_with_subprocess_async",
                    new=AsyncMock(return_value=(True, "ok")),
            ) as execute_mock:
                success, message = asyncio.run(run_install())

        assert success
        assert "ok" == message
        execute_mock.assert_awaited_once()
        assert execute_mock.await_args.kwargs["timeout"] == (
            PluginHelper.PLUGIN_DEPENDENCY_INSTALL_TIMEOUT
        )

    def test_async_package_install_cancellation_closes_full_lifecycle(self, tmp_path):
        """取消真实安装进程后必须回收进程树、临时约束和安装锁。"""
        import psutil

        from app.adapters.external.market import PluginHelper

        helper = PluginHelper()
        requirements_file = tmp_path / "requirements.txt"
        requirements_file.write_text("demo-package\n", encoding="utf-8")
        constraints_file = tmp_path / "runtime-constraints.txt"
        marker = tmp_path / "install-pids"
        child_code = "import time; time.sleep(60)"
        install_code = (
            "from pathlib import Path; import os, subprocess, time; "
            f"child = subprocess.Popen([{sys.executable!r}, '-c', {child_code!r}]); "
            f"Path({str(marker)!r}).write_text(str(os.getpid()) + ':' + str(child.pid)); "
            "time.sleep(60)"
        )
        strategy = Mock(
            strategy_name="uv:test",
            command=[sys.executable, "-c", install_code],
            env=os.environ.copy(),
            safe_log_command=[sys.executable, "-c", "<install>"],
        )
        health = {
            "uv check": (True, "ok"),
            "核心依赖导入检查": (True, "ok"),
        }

        def create_constraints(_protected_packages):
            constraints_file.write_text("fastapi==0\n", encoding="utf-8")
            return constraints_file

        async def run_install():
            task = asyncio.create_task(
                helper.async_install_packages_with_fallback(requirements_file)
            )
            deadline = time.monotonic() + 2
            while not marker.exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert marker.exists()

            pids = [int(value) for value in marker.read_text().split(":")]
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            assert not constraints_file.exists()
            assert PluginHelper._package_install_lock.acquire(blocking=False)
            PluginHelper._package_install_lock.release()
            for _ in range(100):
                alive = []
                for pid in pids:
                    try:
                        process = psutil.Process(pid)
                        if (
                            process.is_running()
                            and process.status() != psutil.STATUS_ZOMBIE
                        ):
                            alive.append(pid)
                    except (psutil.Error, OSError):
                        continue
                if not alive:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail(f"安装进程树仍在运行：{alive}")

        with patch.object(
                PluginHelper,
                "_PluginHelper__get_installed_packages",
                return_value={},
        ), patch.object(
                PluginHelper,
                "_PluginHelper__get_protected_runtime_packages",
                return_value={"fastapi": "0"},
        ), patch.object(
                PluginHelper,
                "_PluginHelper__validate_runtime_dependency_conflicts",
                return_value=(True, ""),
        ), patch.object(
                PluginHelper,
                "_PluginHelper__create_runtime_constraints_file",
                side_effect=create_constraints,
        ), patch(
                "app.adapters.external.market.build_package_install_strategies",
                return_value=[strategy],
        ), patch.object(
                PluginHelper,
                "_PluginHelper__async_run_runtime_healthcheck",
                new=AsyncMock(return_value=health),
        ):
            asyncio.run(run_install())

    def test_constraints_created_during_cancellation_are_removed(self, tmp_path):
        """约束文件创建线程收口后仍须响应取消并删除临时文件。"""
        from app.adapters.external.market import PluginHelper

        helper = PluginHelper()
        requirements_file = tmp_path / "requirements.txt"
        requirements_file.write_text("demo-package\n", encoding="utf-8")
        constraints_file = tmp_path / "runtime-constraints.txt"
        created = threading.Event()
        release = threading.Event()

        def create_constraints(_protected_packages):
            constraints_file.write_text("fastapi==0\n", encoding="utf-8")
            created.set()
            release.wait(timeout=2)
            return constraints_file

        async def run_install():
            task = asyncio.create_task(
                helper.async_install_packages_with_fallback(requirements_file)
            )
            assert await asyncio.to_thread(created.wait, 2)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        with patch.object(
                PluginHelper,
                "_PluginHelper__get_installed_packages",
                return_value={},
        ), patch.object(
                PluginHelper,
                "_PluginHelper__get_protected_runtime_packages",
                return_value={"fastapi": "0"},
        ), patch.object(
                PluginHelper,
                "_PluginHelper__validate_runtime_dependency_conflicts",
                return_value=(True, ""),
        ), patch.object(
                PluginHelper,
                "_PluginHelper__create_runtime_constraints_file",
                side_effect=create_constraints,
        ):
            asyncio.run(run_install())

        assert not constraints_file.exists()

    def test_constraints_cleanup_failure_preserves_cancellation(self, tmp_path):
        """临时文件删除失败只记录日志，不得替换调用方的取消异常。"""
        from app.adapters.external.market import PluginHelper

        constraints_file = tmp_path / "runtime-constraints.txt"
        created = threading.Event()
        release = threading.Event()

        def create_constraints(_protected_packages):
            constraints_file.write_text("fastapi==0\n", encoding="utf-8")
            created.set()
            release.wait(timeout=2)
            return constraints_file

        async def run_create():
            task = asyncio.create_task(
                PluginHelper._PluginHelper__async_create_runtime_constraints_file(
                    {"fastapi": Version("0")}
                )
            )
            assert await asyncio.to_thread(created.wait, 2)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        with patch.object(
                PluginHelper,
                "_PluginHelper__create_runtime_constraints_file",
                side_effect=create_constraints,
        ), patch.object(
                Path,
                "unlink",
                side_effect=PermissionError("locked"),
        ), patch("app.adapters.external.market.logger.warning") as warning:
            asyncio.run(run_create())

        warning.assert_called_once()

    def test_install_uses_release_package_when_asset_is_available(self, monkeypatch):
        """
        release 包可用时优先使用 zip 安装，不再额外访问文件列表。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        calls = _patch_sync_remote_install(
            helper,
            monkeypatch,
            {"release": True, "version": "1.2.3"},
            (True, ""),
        )

        success, message = helper.install(PLUGIN_ID, REPO_URL, package_version="v2", force_install=True)

        assert success
        assert "" == message
        assert ["remove", "release", "refresh"] == calls

    def test_install_falls_back_to_filelist_when_release_is_missing(self, monkeypatch):
        """
        release 标记存在但 tag 或 zip 尚未生成时，清理可能残留的安装目录后回退文件列表安装。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        calls = _patch_sync_remote_install(
            helper,
            monkeypatch,
            {"release": True, "version": "1.2.3"},
            (False, "获取 Release 信息失败：404"),
            (True, ""),
        )

        success, message = helper.install(PLUGIN_ID, REPO_URL, package_version="v2", force_install=True)

        assert success
        assert "" == message
        assert ["remove", "release", "remove", "filelist", "refresh"] == calls

    def test_install_reports_filelist_error_after_release_fallback_fails(self, monkeypatch):
        """
        release 和文件列表都不可用时返回最终文件列表错误，并在每次写入前后保持目录可回滚。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        calls = _patch_sync_remote_install(
            helper,
            monkeypatch,
            {"release": True, "version": "1.2.3"},
            (False, "未找到资产文件：demoplugin_v1.2.3.zip"),
            (False, "获取文件列表失败"),
        )

        success, message = helper.install(PLUGIN_ID, REPO_URL, package_version="v2", force_install=True)

        assert not success
        assert "获取文件列表失败" == message
        assert ["remove", "release", "remove", "filelist", "remove"] == calls

    def test_install_uses_filelist_when_release_flag_is_disabled(self, monkeypatch):
        """
        未开启 release 标记的插件保持原有文件列表安装路径。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        calls = _patch_sync_remote_install(
            helper,
            monkeypatch,
            {"release": False, "version": "1.2.3"},
            (False, "release should not be called"),
            (True, ""),
        )

        success, message = helper.install(PLUGIN_ID, REPO_URL, package_version="v2", force_install=True)

        assert success
        assert "" == message
        assert ["remove", "filelist", "refresh"] == calls

    def test_install_rejects_release_without_version(self, monkeypatch):
        """
        release 安装必须有插件版本号，否则无法构造稳定 tag。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        calls = _patch_sync_remote_install(
            helper,
            monkeypatch,
            {"release": True},
            (True, ""),
        )

        success, message = helper.install(PLUGIN_ID, REPO_URL, package_version="v2", force_install=True)

        assert not success
        assert f"未在插件清单中找到 {PLUGIN_ID} 的版本号" in message
        assert [] == calls

    def test_install_rejects_incompatible_plugin_before_content_preparation(self, monkeypatch):
        """
        系统版本不兼容时不会删除旧插件，也不会尝试 release 或文件列表安装。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        calls = _patch_sync_remote_install(
            helper,
            monkeypatch,
            {"release": True, "version": "1.2.3", "system_version": ">=9.0.0"},
            (True, ""),
        )
        monkeypatch.setattr(PluginHelper, "get_current_system_version", lambda: Version("2.0.0"))

        success, message = helper.install(PLUGIN_ID, REPO_URL, package_version="v2", force_install=True)

        assert not success
        assert "MoviePilot 版本 >=9.0.0" in message
        assert [] == calls

    def test_install_rejects_latest_release_version_when_system_version_is_incompatible(self, monkeypatch):
        """
        指定安装当前最新 release 时仍按当前 package 元数据校验主程序版本。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        calls = _patch_sync_remote_install(
            helper,
            monkeypatch,
            {"release": True, "version": "1.2.3", "system_version": ">=9.0.0"},
            (True, ""),
        )
        monkeypatch.setattr(PluginHelper, "get_current_system_version", lambda: Version("2.0.0"))
        monkeypatch.setattr(
            helper,
            "get_plugin_release_versions",
            lambda *_args: [{"version": "1.2.3", "tag_name": "DemoPlugin_v1.2.3"}],
        )

        success, message = helper.install(
            PLUGIN_ID, REPO_URL, package_version="v2", release_version="1.2.3", force_install=True
        )

        assert not success
        assert "MoviePilot 版本 >=9.0.0" in message
        assert [] == calls

    def test_install_old_release_version_uses_release_asset_without_filelist_fallback(self, monkeypatch):
        """
        指定旧 release 版本时直接安装对应资产，失败也不回退当前分支文件列表。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        calls = _patch_sync_remote_install(
            helper,
            monkeypatch,
            {"release": True, "version": "1.2.3", "system_version": ">=9.0.0"},
            (False, "未找到资产文件：demoplugin_v1.2.0.zip"),
            (True, ""),
        )
        monkeypatch.setattr(PluginHelper, "get_current_system_version", lambda: Version("2.0.0"))
        monkeypatch.setattr(
            helper,
            "get_plugin_release_versions",
            lambda *_args: [{"version": "1.2.0", "tag_name": "DemoPlugin_v1.2.0"}],
        )

        success, message = helper.install(
            PLUGIN_ID, REPO_URL, package_version="v2", release_version="1.2.0", force_install=True
        )

        assert not success
        assert "未找到资产文件：demoplugin_v1.2.0.zip" == message
        assert ["remove", "release", "remove"] == calls

    def test_install_rejects_release_version_missing_from_release_list(self, monkeypatch):
        """
        指定版本必须来自可安装 Release 列表，避免客户端绕过前端约束拼接任意 tag。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        calls = _patch_sync_remote_install(
            helper,
            monkeypatch,
            {"release": True, "version": "1.2.3"},
            (True, ""),
        )
        monkeypatch.setattr(
            helper,
            "get_plugin_release_versions",
            lambda *_args: [{"version": "1.2.3", "tag_name": "DemoPlugin_v1.2.3"}],
        )

        success, message = helper.install(
            PLUGIN_ID, REPO_URL, package_version="v2", release_version="1.2.0", force_install=True
        )

        assert not success
        assert f"{PLUGIN_ID} 未找到可安装的 Release 版本：1.2.0" == message
        assert [] == calls

    def test_install_rejects_invalid_parameters_before_remote_lookup(self):
        """
        远端安装缺少插件 ID 或仓库地址时直接拒绝。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        success, message = PluginHelper().install("", REPO_URL)

        assert not success
        assert "参数错误" == message

    def test_install_rejects_invalid_repo_url(self):
        """
        仓库地址无法解析出 owner/repo 时直接拒绝。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        success, message = PluginHelper().install(PLUGIN_ID, "not-a-repo-url")

        assert not success
        assert "不支持的插件仓库地址格式" == message

    def test_install_rejects_missing_package_version(self, monkeypatch):
        """
        当前系统版本找不到匹配插件索引时直接返回兼容性错误。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        monkeypatch.setattr(helper, "get_plugin_package_version", lambda *_args: None)

        success, message = helper.install(PLUGIN_ID, REPO_URL)

        assert not success
        assert f"{PLUGIN_ID} 没有找到适用于当前版本的插件" == message

    def test_install_uses_default_package_version_when_not_provided(self, monkeypatch):
        """
        调用方未指定索引版本时使用系统版本标记继续安装。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        seen_versions = []
        monkeypatch.setattr(helper, "get_plugin_package_version", lambda _pid, _repo, version: seen_versions.append(version) or "")
        monkeypatch.setattr(helper, "_PluginHelper__get_plugin_meta", lambda *_args: {"release": False, "version": "1.2.3"})
        monkeypatch.setattr(helper, "_PluginHelper__backup_plugin", lambda _pid: None)
        monkeypatch.setattr(helper, "_PluginHelper__remove_old_plugin", lambda _pid: None)
        monkeypatch.setattr(helper, "_PluginHelper__install_dependencies_if_required", lambda _pid: (False, True, ""))
        monkeypatch.setattr(helper, "refresh_persistent_plugin_backup", lambda _pid: None)
        monkeypatch.setattr(helper, "_PluginHelper__prepare_content_via_filelist_sync", lambda *_args: (True, ""))

        success, message = helper.install(PLUGIN_ID, REPO_URL, force_install=True)

        assert success
        assert "" == message
        assert seen_versions

    def test_install_local_copies_runtime_assets_without_build_dependencies(self, monkeypatch, tmp_path):
        """
        local:// 来源保留运行资产，但不把本地前端构建依赖复制到运行目录。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        repo_path = tmp_path / "local-plugins"
        source_dir = repo_path / "plugins.v2" / PLUGIN_ID.lower()
        remote_entry = source_dir / "dist" / "assets" / "remoteEntry.js"
        remote_entry.parent.mkdir(parents=True)
        remote_entry.write_text("export default {}\n", encoding="utf-8")
        dependency_file = source_dir / "node_modules" / "example" / "index.js"
        dependency_file.parent.mkdir(parents=True)
        dependency_file.write_text("module.exports = {}\n", encoding="utf-8")

        runtime_root = tmp_path / "runtime-plugins"
        helper = PluginHelper()
        monkeypatch.setattr(
            helper,
            "get_local_plugin_candidate",
            lambda *_args, **_kwargs: {
                "path": source_dir,
                "repo_path": repo_path,
                "package_version": "v2",
                "version": "1.0.0",
            },
        )
        monkeypatch.setattr("app.adapters.external.market.PLUGIN_DIR", runtime_root)
        monkeypatch.setattr(helper, "refresh_persistent_plugin_backup", lambda _pid: True)

        success, message = helper.install(
            PLUGIN_ID,
            helper.make_local_repo_url(PLUGIN_ID, repo_path, "v2"),
            force_install=True,
        )

        assert success
        assert "" == message
        runtime_dir = runtime_root / PLUGIN_ID.lower()
        assert (runtime_dir / "dist" / "assets" / "remoteEntry.js").is_file()
        assert not (runtime_dir / "node_modules").exists()

    def test_install_release_download_failure_falls_back_to_filelist(self, monkeypatch):
        """
        release tag 存在但 zip 下载失败时清理可能残留的目录，再回退文件列表安装。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        calls = _patch_sync_remote_install(
            helper,
            monkeypatch,
            {"release": True, "version": "1.2.3"},
            (False, "下载资产失败：502"),
            (True, ""),
        )

        success, message = helper.install(PLUGIN_ID, REPO_URL, package_version="v2", force_install=True)

        assert success
        assert "" == message
        assert ["remove", "release", "remove", "filelist", "refresh"] == calls

    def test_async_install_uses_release_package_when_asset_is_available(self, monkeypatch):
        """
        异步安装路径在 release 包可用时优先使用 zip 安装。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        calls = _patch_async_remote_install(
            helper,
            monkeypatch,
            {"release": True, "version": "1.2.3"},
            (True, ""),
        )

        success, message = asyncio.run(
            helper.async_install(PLUGIN_ID, REPO_URL, package_version="v2", force_install=True)
        )

        assert success
        assert "" == message
        assert calls[:2] == ["remove", "release"]
        assert calls[2][0] == "to_thread"

    def test_async_install_falls_back_to_filelist_when_release_is_missing(self, monkeypatch):
        """
        异步安装路径在 release tag 或 zip 未生成时，清理可能残留的安装目录后回退文件列表安装。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        calls = _patch_async_remote_install(
            helper,
            monkeypatch,
            {"release": True, "version": "1.2.3"},
            (False, "获取 Release 信息失败：404"),
            (True, ""),
        )

        success, message = asyncio.run(
            helper.async_install(PLUGIN_ID, REPO_URL, package_version="v2", force_install=True)
        )

        assert success
        assert "" == message
        assert calls[:4] == ["remove", "release", "remove", "filelist"]
        assert calls[4][0] == "to_thread"

    def test_async_install_old_release_version_uses_release_asset_without_filelist_fallback(self, monkeypatch):
        """
        异步路径指定旧 release 版本时直接安装对应资产，失败也不回退当前分支文件列表。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        calls = _patch_async_remote_install(
            helper,
            monkeypatch,
            {"release": True, "version": "1.2.3", "system_version": ">=9.0.0"},
            (False, "未找到资产文件：demoplugin_v1.2.0.zip"),
            (True, ""),
        )
        monkeypatch.setattr(PluginHelper, "get_current_system_version", lambda: Version("2.0.0"))

        async def fake_releases(*_args):
            return [{"version": "1.2.0", "tag_name": "DemoPlugin_v1.2.0"}]

        monkeypatch.setattr(helper, "async_get_plugin_release_versions", fake_releases)

        success, message = asyncio.run(
            helper.async_install(
                PLUGIN_ID, REPO_URL, package_version="v2", release_version="1.2.0", force_install=True
            )
        )

        assert not success
        assert "未找到资产文件：demoplugin_v1.2.0.zip" == message
        assert calls[:3] == ["remove", "release", "remove"]

    def test_async_install_rejects_release_version_missing_from_release_list(self, monkeypatch):
        """
        异步安装同样只接受 Release 列表中存在的指定版本。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        calls = _patch_async_remote_install(
            helper,
            monkeypatch,
            {"release": True, "version": "1.2.3"},
            (True, ""),
        )

        async def fake_releases(*_args):
            return [{"version": "1.2.3", "tag_name": "DemoPlugin_v1.2.3"}]

        monkeypatch.setattr(helper, "async_get_plugin_release_versions", fake_releases)

        success, message = asyncio.run(
            helper.async_install(
                PLUGIN_ID, REPO_URL, package_version="v2", release_version="1.2.0", force_install=True
            )
        )

        assert not success
        assert f"{PLUGIN_ID} 未找到可安装的 Release 版本：1.2.0" == message
        assert [] == calls

    def test_async_install_reports_filelist_error_after_release_fallback_fails(self, monkeypatch):
        """
        异步安装路径在 release 与文件列表都失败时返回文件列表错误，并保持失败清理顺序稳定。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        calls = _patch_async_remote_install(
            helper,
            monkeypatch,
            {"release": True, "version": "1.2.3"},
            (False, "未找到资产文件：demoplugin_v1.2.3.zip"),
            (False, "获取文件列表失败"),
        )

        success, message = asyncio.run(
            helper.async_install(PLUGIN_ID, REPO_URL, package_version="v2", force_install=True)
        )

        assert not success
        assert "获取文件列表失败" == message
        assert calls == ["remove", "release", "remove", "filelist", "remove"]

    def test_async_install_release_fallback_uses_lowercase_filelist_pid(self, monkeypatch):
        """
        异步 release 回退文件列表安装时使用小写插件 ID，保持 GitHub 目录查询与同步路径一致。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        filelist_pids = []
        _patch_async_remote_install(
            helper,
            monkeypatch,
            {"release": True, "version": "1.2.3"},
            (False, "获取 Release 信息失败：404"),
            (True, ""),
        )

        async def fake_filelist(pid, _user_repo, _package_version):
            filelist_pids.append(pid)
            return True, ""

        monkeypatch.setattr(helper, "_PluginHelper__prepare_content_via_filelist_async", fake_filelist)

        success, message = asyncio.run(
            helper.async_install(PLUGIN_ID, REPO_URL, package_version="v2", force_install=True)
        )

        assert success
        assert "" == message
        assert ["demoplugin"] == filelist_pids

    def test_async_install_non_release_uses_lowercase_filelist_pid(self, monkeypatch):
        """
        异步文件列表直装使用小写插件 ID，避免大小写插件 ID 影响远端目录匹配。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        filelist_pids = []
        _patch_async_remote_install(
            helper,
            monkeypatch,
            {"release": False, "version": "1.2.3"},
            (False, "release should not be called"),
            (True, ""),
        )

        async def fake_filelist(pid, _user_repo, _package_version):
            filelist_pids.append(pid)
            return True, ""

        monkeypatch.setattr(helper, "_PluginHelper__prepare_content_via_filelist_async", fake_filelist)

        success, message = asyncio.run(
            helper.async_install(PLUGIN_ID, REPO_URL, package_version="v2", force_install=True)
        )

        assert success
        assert "" == message
        assert ["demoplugin"] == filelist_pids

    def test_install_from_release_reports_missing_tag(self, monkeypatch):
        """
        release tag 不存在时返回可用于降级判断的失败消息。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        monkeypatch.setattr(helper, "_PluginHelper__request_with_fallback", lambda *_args, **_kwargs: _FakeResponse(404))

        success, message = helper._PluginHelper__install_from_release(PLUGIN_ID, "demo/repo", "DemoPlugin_v1.2.3")

        assert not success
        assert "获取 Release 信息失败：404" == message

    def test_install_from_release_reports_missing_asset(self, monkeypatch):
        """
        release tag 存在但缺少规范 zip 资产时返回明确错误。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        monkeypatch.setattr(
            helper,
            "_PluginHelper__request_with_fallback",
            lambda *_args, **_kwargs: _FakeResponse(200, {"assets": [{"name": "other.zip", "id": 1}]}),
        )

        success, message = helper._PluginHelper__install_from_release(PLUGIN_ID, "demo/repo", "DemoPlugin_v1.2.3")

        assert not success
        assert "未找到资产文件：demoplugin_v1.2.3.zip" == message

    def test_install_from_release_reports_missing_asset_id(self, monkeypatch):
        """
        release 资产缺少 id 时无法使用 API 下载，返回明确错误。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        monkeypatch.setattr(
            helper,
            "_PluginHelper__request_with_fallback",
            lambda *_args, **_kwargs: _FakeResponse(200, {"assets": [{"name": "demoplugin_v1.2.3.zip"}]}),
        )

        success, message = helper._PluginHelper__install_from_release(PLUGIN_ID, "demo/repo", "DemoPlugin_v1.2.3")

        assert not success
        assert "资产缺少ID信息" == message

    def test_install_from_release_reports_malformed_release_payload(self, monkeypatch):
        """
        release API 返回无法解析的结构时返回解析错误。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        class BadResponse(_FakeResponse):
            """json() 抛错的响应对象。"""

            def json(self):
                """模拟响应体不是合法 JSON。"""
                raise ValueError("bad json")

        helper = PluginHelper()
        monkeypatch.setattr(helper, "_PluginHelper__request_with_fallback", lambda *_args, **_kwargs: BadResponse(200))

        success, message = helper._PluginHelper__install_from_release(PLUGIN_ID, "demo/repo", "DemoPlugin_v1.2.3")

        assert not success
        assert "解析 Release 信息失败" in message

    def test_install_from_release_reports_asset_download_failure(self, monkeypatch):
        """
        release asset API 下载失败时返回下载错误。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        responses = iter([
            _FakeResponse(200, {"assets": [{"name": "demoplugin_v1.2.3.zip", "id": 42}]}),
            _FakeResponse(502),
        ])
        monkeypatch.setattr(helper, "_PluginHelper__request_with_fallback", lambda *_args, **_kwargs: next(responses))

        success, message = helper._PluginHelper__install_from_release(PLUGIN_ID, "demo/repo", "DemoPlugin_v1.2.3")

        assert not success
        assert "下载资产失败：502" == message

    @pytest.mark.parametrize(
        "member_name, symlink",
        [
            ("../evil.py", False),
            ("/tmp/evil.py", False),
            ("..\\evil.py", False),
            ("C:/evil.py", False),
            ("//server/share/evil.py", False),
            ("demoplugin/link.py", True),
        ],
    )
    def test_install_from_release_rejects_unsafe_zip_member(self, monkeypatch, tmp_path, member_name, symlink):
        """
        release zip 成员必须限制在插件运行目录内，且不能是符号链接或特殊文件。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        responses = iter([
            _FakeResponse(200, {"assets": [{"name": "demoplugin_v1.2.3.zip", "id": 42}]}),
            _FakeContentResponse(200, _build_release_zip_member(member_name, symlink=symlink)),
        ])
        _patch_release_install_settings(monkeypatch, tmp_path)
        monkeypatch.setattr(helper, "_PluginHelper__request_with_fallback", lambda *_args, **_kwargs: next(responses))

        success, message = helper._PluginHelper__install_from_release(PLUGIN_ID, "demo/repo", "DemoPlugin_v1.2.3")

        assert not success
        assert "非法 Release 压缩包成员" in message
        assert not (tmp_path / "app" / "plugins" / "evil.py").exists()
        assert not (tmp_path / "app" / "plugins" / "demoplugin" / "..\\evil.py").exists()
        assert not (tmp_path / "app" / "plugins" / "demoplugin" / "C:").exists()
        assert not (tmp_path / "app" / "plugins" / "demoplugin" / "link.py").exists()

    def test_install_from_release_extracts_zip_with_top_level_directory(self, monkeypatch, tmp_path):
        """
        release zip 带顶层插件目录时剥离该层后写入运行目录。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        release_payload = {"assets": [{"name": "demoplugin_v1.2.3.zip", "id": 42}]}
        zip_content = _build_zip({
            "demoplugin/__init__.py": b"plugin",
            "demoplugin/nested/config.json": b"{}",
        })
        responses = iter([
            _FakeResponse(200, release_payload),
            _FakeContentResponse(200, zip_content),
        ])
        monkeypatch.setattr("app.adapters.external.market.settings", SimpleNamespace(
            ROOT_PATH=tmp_path,
            REPO_GITHUB_HEADERS=lambda repo=None: {},
        ))
        monkeypatch.setattr(helper, "_PluginHelper__request_with_fallback", lambda *_args, **_kwargs: next(responses))

        success, message = helper._PluginHelper__install_from_release(PLUGIN_ID, "demo/repo", "DemoPlugin_v1.2.3")

        assert success
        assert "" == message
        assert (tmp_path / "app" / "plugins" / "demoplugin" / "__init__.py").read_bytes() == b"plugin"
        assert (tmp_path / "app" / "plugins" / "demoplugin" / "nested" / "config.json").read_bytes() == b"{}"

    def test_install_from_release_creates_directory_entries(self, monkeypatch, tmp_path):
        """
        release zip 内显式目录项会被创建，并继续写入后续文件。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("demoplugin/assets/", b"")
            zf.writestr("demoplugin/assets/icon.png", b"icon")
        responses = iter([
            _FakeResponse(200, {"assets": [{"name": "demoplugin_v1.2.3.zip", "id": 42}]}),
            _FakeContentResponse(200, buffer.getvalue()),
        ])
        monkeypatch.setattr("app.adapters.external.market.settings", SimpleNamespace(
            ROOT_PATH=tmp_path,
            REPO_GITHUB_HEADERS=lambda repo=None: {},
        ))
        monkeypatch.setattr(helper, "_PluginHelper__request_with_fallback", lambda *_args, **_kwargs: next(responses))

        success, message = helper._PluginHelper__install_from_release(PLUGIN_ID, "demo/repo", "DemoPlugin_v1.2.3")

        assert success
        assert "" == message
        assert (tmp_path / "app" / "plugins" / "demoplugin" / "assets").is_dir()
        assert (tmp_path / "app" / "plugins" / "demoplugin" / "assets" / "icon.png").read_bytes() == b"icon"

    def test_install_from_release_reports_empty_zip(self, monkeypatch):
        """
        release zip 为空时返回明确错误，避免安装出空插件目录。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        responses = iter([
            _FakeResponse(200, {"assets": [{"name": "demoplugin_v1.2.3.zip", "id": 42}]}),
            _FakeContentResponse(200, _build_zip({})),
        ])
        monkeypatch.setattr(helper, "_PluginHelper__request_with_fallback", lambda *_args, **_kwargs: next(responses))

        success, message = helper._PluginHelper__install_from_release(PLUGIN_ID, "demo/repo", "DemoPlugin_v1.2.3")

        assert not success
        assert "压缩包内容为空" == message

    def test_install_from_release_reports_directory_only_zip(self, monkeypatch, tmp_path):
        """
        release zip 只有目录项时返回无可写入文件错误。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("demoplugin/assets/", b"")
        responses = iter([
            _FakeResponse(200, {"assets": [{"name": "demoplugin_v1.2.3.zip", "id": 42}]}),
            _FakeContentResponse(200, buffer.getvalue()),
        ])
        monkeypatch.setattr("app.adapters.external.market.settings", SimpleNamespace(
            ROOT_PATH=tmp_path,
            REPO_GITHUB_HEADERS=lambda repo=None: {},
        ))
        monkeypatch.setattr(helper, "_PluginHelper__request_with_fallback", lambda *_args, **_kwargs: next(responses))

        success, message = helper._PluginHelper__install_from_release(PLUGIN_ID, "demo/repo", "DemoPlugin_v1.2.3")

        assert not success
        assert "压缩包中无可写入文件" == message

    def test_install_from_release_reports_bad_zip(self, monkeypatch):
        """
        release asset 不是合法 zip 时返回解压错误。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        responses = iter([
            _FakeResponse(200, {"assets": [{"name": "demoplugin_v1.2.3.zip", "id": 42}]}),
            _FakeContentResponse(200, b"not a zip"),
        ])
        monkeypatch.setattr(helper, "_PluginHelper__request_with_fallback", lambda *_args, **_kwargs: next(responses))

        success, message = helper._PluginHelper__install_from_release(PLUGIN_ID, "demo/repo", "DemoPlugin_v1.2.3")

        assert not success
        assert "解压 Release 压缩包失败" in message

    def test_install_flow_sync_restores_backup_when_prepare_fails(self, monkeypatch):
        """
        内容准备失败时恢复备份，避免安装失败后留下半成品目录。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        calls = []
        monkeypatch.setattr(helper, "_PluginHelper__backup_plugin", lambda _pid: "/backup")
        monkeypatch.setattr(helper, "_PluginHelper__remove_old_plugin", lambda _pid: calls.append("remove"))
        monkeypatch.setattr(helper, "_PluginHelper__restore_plugin", lambda _pid, _backup: calls.append("restore"))

        success, message = helper._PluginHelper__install_flow_sync(
            PLUGIN_ID, False, lambda: (False, "prepare failed")
        )

        assert not success
        assert "prepare failed" == message
        assert ["remove", "restore"] == calls

    def test_install_flow_sync_restores_backup_when_dependency_install_fails(self, monkeypatch):
        """
        依赖安装失败时恢复备份，避免新插件内容破坏可用版本。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        calls = []
        monkeypatch.setattr(helper, "_PluginHelper__backup_plugin", lambda _pid: "/backup")
        monkeypatch.setattr(helper, "_PluginHelper__remove_old_plugin", lambda _pid: calls.append("remove"))
        monkeypatch.setattr(helper, "_PluginHelper__restore_plugin", lambda _pid, _backup: calls.append("restore"))
        monkeypatch.setattr(
            helper,
            "_PluginHelper__install_dependencies_if_required",
            lambda _pid: (True, False, "dependency failed"),
        )

        success, message = helper._PluginHelper__install_flow_sync(
            PLUGIN_ID, False, lambda: (True, "")
        )

        assert not success
        assert "dependency failed" == message
        assert ["remove", "restore"] == calls

    def test_install_flow_sync_restores_backup_for_invalid_modern_manifest(self, tmp_path, monkeypatch):
        """现代清单无效时恢复旧插件目录。"""
        from app.adapters.external import market as market_module

        plugin_root = tmp_path / "plugins"
        plugin_dir = plugin_root / PLUGIN_ID.lower()
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "old.txt").write_text("old", encoding="utf-8")
        monkeypatch.setattr(market_module, "PLUGIN_DIR", plugin_root)
        monkeypatch.setattr(market_module.settings, "CONFIG_DIR", str(tmp_path))

        def prepare_content():
            plugin_dir.mkdir(parents=True)
            (plugin_dir / "pyproject.toml").write_text(
                "[project]\nname = 'demo'\n",
                encoding="utf-8",
            )
            return True, ""

        success, message = market_module.PluginHelper()._PluginHelper__install_flow_sync(
            PLUGIN_ID,
            False,
            prepare_content,
        )

        assert not success
        assert "project.version" in message
        assert (plugin_dir / "old.txt").read_text(encoding="utf-8") == "old"
        assert not (plugin_dir / "pyproject.toml").exists()

    def test_install_dependencies_prefers_plugin_pyproject(self, tmp_path, monkeypatch):
        """同步安装入口只消费双清单中的 pyproject。"""
        from app.adapters.external import market as market_module

        plugin_root = tmp_path / "plugins"
        plugin_dir = plugin_root / "demoplugin"
        plugin_dir.mkdir(parents=True)
        pyproject_file = plugin_dir / "pyproject.toml"
        pyproject_file.write_text(
            '[project]\nname = "demo"\nversion = "1.0.0"\ndependencies = ["modern>=1"]\n',
            encoding="utf-8",
        )
        (plugin_dir / "requirements.txt").write_text("legacy>=1\n", encoding="utf-8")
        helper = market_module.PluginHelper()
        seen = []
        monkeypatch.setattr(market_module, "PLUGIN_DIR", plugin_root)
        monkeypatch.setattr(
            helper,
            "install_packages_with_fallback",
            lambda path: seen.append(path) or (True, ""),
        )

        result = helper._PluginHelper__install_dependencies_if_required("DemoPlugin")

        assert result == (True, True, "")
        assert seen == [pyproject_file]

    def test_async_install_dependencies_prefers_plugin_pyproject(self, tmp_path, monkeypatch):
        """异步安装入口只消费双清单中的 pyproject。"""
        from app.adapters.external import market as market_module

        plugin_root = tmp_path / "plugins"
        plugin_dir = plugin_root / "demoplugin"
        plugin_dir.mkdir(parents=True)
        pyproject_file = plugin_dir / "pyproject.toml"
        pyproject_file.write_text(
            '[project]\nname = "demo"\nversion = "1.0.0"\ndependencies = ["modern>=1"]\n',
            encoding="utf-8",
        )
        (plugin_dir / "requirements.txt").write_text("legacy>=1\n", encoding="utf-8")
        helper = market_module.PluginHelper()
        seen = []

        async def fake_install(path, _find_links=None):
            seen.append(path)
            return True, ""

        monkeypatch.setattr(market_module, "PLUGIN_DIR", plugin_root)
        monkeypatch.setattr(
            helper,
            "_PluginHelper__async_install_packages_with_fallback",
            fake_install,
        )

        result = asyncio.run(
            helper._PluginHelper__async_install_dependencies_if_required("DemoPlugin")
        )

        assert result == (True, True, "")
        assert seen == [pyproject_file]

    def test_prepare_content_via_filelist_sync_downloads_dependency_manifests_once(self, monkeypatch):
        """文件列表准备会完整下载内容，依赖由统一安装流程处理。"""
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        calls = []
        file_list = [
            {"name": "pyproject.toml", "download_url": "https://example.com/pyproject.toml"},
            {"name": "requirements.txt", "download_url": "https://example.com/requirements.txt"},
            {"name": "__init__.py", "download_url": "https://example.com/__init__.py"},
        ]
        monkeypatch.setattr(helper, "_PluginHelper__get_file_list", lambda *_args: (file_list, ""))

        def fake_download(*args):
            calls.append(args)
            return True, ""

        monkeypatch.setattr(
            helper,
            "_PluginHelper__download_files",
            fake_download,
        )

        success, message = helper._PluginHelper__prepare_content_via_filelist_sync("demoplugin", "demo/repo", "v2")

        assert success
        assert "" == message
        assert calls == [("demoplugin", file_list, "demo/repo", "v2")]

    def test_prepare_content_via_filelist_sync_reports_missing_file_list(self, monkeypatch):
        """
        文件列表为空时直接返回列表获取错误。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        monkeypatch.setattr(helper, "_PluginHelper__get_file_list", lambda *_args: ([], "list failed"))

        success, message = helper._PluginHelper__prepare_content_via_filelist_sync("demoplugin", "demo/repo", "v2")

        assert not success
        assert "list failed" == message

    def test_prepare_content_via_filelist_sync_returns_download_error(self, monkeypatch):
        """
        文件列表存在但文件下载失败时向上返回下载错误。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        monkeypatch.setattr(helper, "_PluginHelper__get_file_list", lambda *_args: ([{"name": "__init__.py"}], ""))
        monkeypatch.setattr(helper, "_PluginHelper__download_files", lambda *_args: (False, "download failed"))

        success, message = helper._PluginHelper__prepare_content_via_filelist_sync("demoplugin", "demo/repo", "v2")

        assert not success
        assert "download failed" == message

    def test_async_prepare_content_via_filelist_downloads_dependency_manifests_once(self, monkeypatch):
        """异步文件列表准备会完整下载内容，依赖由统一安装流程处理。"""
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        calls = []
        file_list = [
            {"name": "pyproject.toml", "download_url": "https://example.com/pyproject.toml"},
            {"name": "requirements.txt", "download_url": "https://example.com/requirements.txt"},
            {"name": "__init__.py", "download_url": "https://example.com/__init__.py"},
        ]

        async def fake_file_list(*_args):
            return file_list, ""

        async def fake_download(*args):
            calls.append(args)
            return True, ""

        monkeypatch.setattr(helper, "_PluginHelper__async_get_file_list", fake_file_list)
        monkeypatch.setattr(helper, "_PluginHelper__async_download_files", fake_download)

        success, message = asyncio.run(
            helper._PluginHelper__prepare_content_via_filelist_async("demoplugin", "demo/repo", "v2")
        )

        assert success
        assert "" == message
        assert calls == [("demoplugin", file_list, "demo/repo", "v2")]

    def test_async_prepare_content_via_filelist_reports_missing_file_list(self, monkeypatch):
        """
        异步文件列表为空时直接返回列表获取错误。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()

        async def fake_file_list(*_args):
            return [], "list failed"

        monkeypatch.setattr(helper, "_PluginHelper__async_get_file_list", fake_file_list)

        success, message = asyncio.run(
            helper._PluginHelper__prepare_content_via_filelist_async("demoplugin", "demo/repo", "v2")
        )

        assert not success
        assert "list failed" == message

    def test_async_prepare_content_via_filelist_returns_download_error(self, monkeypatch):
        """
        异步文件列表下载失败时向上返回下载错误。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()

        async def fake_file_list(*_args):
            return [{"name": "__init__.py"}], ""

        async def fake_download(*_args):
            return False, "download failed"

        monkeypatch.setattr(helper, "_PluginHelper__async_get_file_list", fake_file_list)
        monkeypatch.setattr(helper, "_PluginHelper__async_download_files", fake_download)

        success, message = asyncio.run(
            helper._PluginHelper__prepare_content_via_filelist_async("demoplugin", "demo/repo", "v2")
        )

        assert not success
        assert "download failed" == message

    def test_install_flow_async_restores_backup_when_prepare_fails(self, monkeypatch):
        """
        异步内容准备失败时恢复备份。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        calls = []

        async def backup(_pid):
            return "/backup"

        async def remove(_pid):
            calls.append("remove")

        async def restore(_pid, _backup):
            calls.append("restore")

        async def prepare():
            return False, "prepare failed"

        monkeypatch.setattr(helper, "_PluginHelper__async_backup_plugin", backup)
        monkeypatch.setattr(helper, "_PluginHelper__async_remove_old_plugin", remove)
        monkeypatch.setattr(helper, "_PluginHelper__async_restore_plugin", restore)

        success, message = asyncio.run(helper._PluginHelper__install_flow_async(PLUGIN_ID, False, prepare))

        assert not success
        assert "prepare failed" == message
        assert ["remove", "restore"] == calls

    def test_install_flow_async_restores_backup_when_dependency_install_fails(self, monkeypatch):
        """
        异步依赖安装失败时恢复备份。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        calls = []

        async def backup(_pid):
            return "/backup"

        async def remove(_pid):
            calls.append("remove")

        async def restore(_pid, _backup):
            calls.append("restore")

        async def prepare():
            return True, ""

        async def dependencies(_pid):
            return True, False, "dependency failed"

        monkeypatch.setattr(helper, "_PluginHelper__async_backup_plugin", backup)
        monkeypatch.setattr(helper, "_PluginHelper__async_remove_old_plugin", remove)
        monkeypatch.setattr(helper, "_PluginHelper__async_restore_plugin", restore)
        monkeypatch.setattr(helper, "_PluginHelper__async_install_dependencies_if_required", dependencies)

        success, message = asyncio.run(helper._PluginHelper__install_flow_async(PLUGIN_ID, False, prepare))

        assert not success
        assert "dependency failed" == message
        assert ["remove", "restore"] == calls

    def test_async_install_from_release_reports_missing_asset(self, monkeypatch):
        """
        异步 release tag 存在但缺少规范 zip 资产时返回明确错误。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()

        async def fake_request(*_args, **_kwargs):
            return _FakeResponse(200, {"assets": [{"name": "other.zip", "id": 1}]})

        monkeypatch.setattr(helper, "_PluginHelper__async_request_with_fallback", fake_request)

        success, message = asyncio.run(
            helper._PluginHelper__async_install_from_release(PLUGIN_ID, "demo/repo", "DemoPlugin_v1.2.3")
        )

        assert not success
        assert "未找到资产文件：demoplugin_v1.2.3.zip" == message

    def test_async_install_from_release_reports_missing_tag(self, monkeypatch):
        """
        异步 release tag 不存在时返回获取 release 失败。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()

        async def fake_request(*_args, **_kwargs):
            return _FakeResponse(404)

        monkeypatch.setattr(helper, "_PluginHelper__async_request_with_fallback", fake_request)

        success, message = asyncio.run(
            helper._PluginHelper__async_install_from_release(PLUGIN_ID, "demo/repo", "DemoPlugin_v1.2.3")
        )

        assert not success
        assert "获取 Release 信息失败：404" == message

    def test_async_install_from_release_reports_missing_asset_id(self, monkeypatch):
        """
        异步 release 资产缺少 id 时返回明确错误。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()

        async def fake_request(*_args, **_kwargs):
            return _FakeResponse(200, {"assets": [{"name": "demoplugin_v1.2.3.zip"}]})

        monkeypatch.setattr(helper, "_PluginHelper__async_request_with_fallback", fake_request)

        success, message = asyncio.run(
            helper._PluginHelper__async_install_from_release(PLUGIN_ID, "demo/repo", "DemoPlugin_v1.2.3")
        )

        assert not success
        assert "资产缺少ID信息" == message

    def test_async_install_from_release_reports_asset_download_failure(self, monkeypatch):
        """
        异步 release asset 下载失败时返回下载错误。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        responses = iter([
            _FakeResponse(200, {"assets": [{"name": "demoplugin_v1.2.3.zip", "id": 42}]}),
            _FakeResponse(502),
        ])

        async def fake_request(*_args, **_kwargs):
            return next(responses)

        monkeypatch.setattr(helper, "_PluginHelper__async_request_with_fallback", fake_request)

        success, message = asyncio.run(
            helper._PluginHelper__async_install_from_release(PLUGIN_ID, "demo/repo", "DemoPlugin_v1.2.3")
        )

        assert not success
        assert "下载资产失败：502" == message

    @pytest.mark.parametrize(
        "member_name, symlink",
        [
            ("../evil.py", False),
            ("/tmp/evil.py", False),
            ("..\\evil.py", False),
            ("C:/evil.py", False),
            ("//server/share/evil.py", False),
            ("demoplugin/link.py", True),
        ],
    )
    def test_async_install_from_release_rejects_unsafe_zip_member(self, monkeypatch, tmp_path, member_name, symlink):
        """
        异步 release zip 成员使用同步路径相同的边界与文件类型规则。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        responses = iter([
            _FakeResponse(200, {"assets": [{"name": "demoplugin_v1.2.3.zip", "id": 42}]}),
            _FakeContentResponse(200, _build_release_zip_member(member_name, symlink=symlink)),
        ])

        async def fake_request(*_args, **_kwargs):
            return next(responses)

        _patch_release_install_settings(monkeypatch, tmp_path)
        monkeypatch.setattr(helper, "_PluginHelper__async_request_with_fallback", fake_request)

        success, message = asyncio.run(
            helper._PluginHelper__async_install_from_release(PLUGIN_ID, "demo/repo", "DemoPlugin_v1.2.3")
        )

        assert not success
        assert "非法 Release 压缩包成员" in message
        assert not (tmp_path / "app" / "plugins" / "evil.py").exists()
        assert not (tmp_path / "app" / "plugins" / "demoplugin" / "..\\evil.py").exists()
        assert not (tmp_path / "app" / "plugins" / "demoplugin" / "C:").exists()
        assert not (tmp_path / "app" / "plugins" / "demoplugin" / "link.py").exists()

    def test_async_install_from_release_extracts_zip_with_top_level_directory(self, monkeypatch, tmp_path):
        """
        异步 release zip 带顶层插件目录时剥离该层后写入运行目录。
        """
        try:
            from app.runtime.config import settings
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        responses = iter([
            _FakeResponse(200, {"assets": [{"name": "demoplugin_v1.2.3.zip", "id": 42}]}),
            _FakeContentResponse(200, _build_zip({"demoplugin/__init__.py": b"plugin"})),
        ])

        async def fake_request(*_args, **_kwargs):
            return next(responses)

        monkeypatch.setattr("app.adapters.external.market.settings", SimpleNamespace(
            ROOT_PATH=tmp_path,
            REPO_GITHUB_HEADERS=lambda repo=None: {},
        ))
        monkeypatch.setattr(helper, "_PluginHelper__async_request_with_fallback", fake_request)

        success, message = asyncio.run(
            helper._PluginHelper__async_install_from_release(PLUGIN_ID, "demo/repo", "DemoPlugin_v1.2.3")
        )

        assert success
        assert "" == message
        assert (tmp_path / "app" / "plugins" / "demoplugin" / "__init__.py").read_bytes() == b"plugin"

    def test_async_install_from_release_reports_empty_zip(self, monkeypatch):
        """
        异步 release zip 为空时返回明确错误。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        responses = iter([
            _FakeResponse(200, {"assets": [{"name": "demoplugin_v1.2.3.zip", "id": 42}]}),
            _FakeContentResponse(200, _build_zip({})),
        ])

        async def fake_request(*_args, **_kwargs):
            return next(responses)

        monkeypatch.setattr(helper, "_PluginHelper__async_request_with_fallback", fake_request)

        success, message = asyncio.run(
            helper._PluginHelper__async_install_from_release(PLUGIN_ID, "demo/repo", "DemoPlugin_v1.2.3")
        )

        assert not success
        assert "压缩包内容为空" == message

    def test_async_install_from_release_reports_bad_zip(self, monkeypatch):
        """
        异步 release asset 不是合法 zip 时返回解压错误。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        responses = iter([
            _FakeResponse(200, {"assets": [{"name": "demoplugin_v1.2.3.zip", "id": 42}]}),
            _FakeContentResponse(200, b"not a zip"),
        ])

        async def fake_request(*_args, **_kwargs):
            return next(responses)

        monkeypatch.setattr(helper, "_PluginHelper__async_request_with_fallback", fake_request)

        success, message = asyncio.run(
            helper._PluginHelper__async_install_from_release(PLUGIN_ID, "demo/repo", "DemoPlugin_v1.2.3")
        )

        assert not success
        assert "解压 Release 压缩包失败" in message

    def test_install_local_rejects_mismatched_local_repo_id(self):
        """
        本地插件来源中的插件 ID 必须与安装目标一致。
        """
        try:
            from app.adapters.external.market import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        success, message = PluginHelper().install("DemoPlugin", "local://OtherPlugin?path=/tmp/plugins")

        assert not success
        assert "本地插件来源与插件ID不匹配" == message
