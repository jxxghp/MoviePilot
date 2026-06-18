import asyncio
import io
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

from packaging.requirements import Requirement
from packaging.version import Version


PLUGIN_ID = "DemoPlugin"
REPO_URL = "https://github.com/demo/MoviePilot-Plugins"


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
    monkeypatch.setattr("app.helper.plugin.asyncio.to_thread", fake_to_thread)
    return calls


class TestPluginHelper:

    def test_sanitize_plugin_repo_url_keeps_remote_url(self):
        """
        插件安装统计脱敏保留远端仓库地址。
        """
        try:
            from app.helper.server import MoviePilotServerHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")
        repo_url = "https://github.com/InfinityPacer/MoviePilot-Plugins"
        assert repo_url == MoviePilotServerHelper.sanitize_plugin_repo_url(repo_url)

    def test_sanitize_plugin_repo_url_strips_local_path(self):
        """
        插件安装统计脱敏移除本地仓库绝对路径。
        """
        try:
            from app.helper.server import MoviePilotServerHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")
        repo_url = "local://TestPlugin?path=/Users/InfinityPacer/GitHub/MoviePilot/MoviePilot-Plugins&version=v2"
        assert "local://TestPlugin?version=v2" == MoviePilotServerHelper.sanitize_plugin_repo_url(repo_url)

    def test_append_cache_buster_only_during_fresh_context(self):
        """
        插件库强制刷新时远端索引 URL 也要变化，避免命中镜像或代理缓存。
        """
        try:
            from app.core.cache import fresh
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        url = "https://raw.githubusercontent.com/user/repo/main/package.json"

        assert url == PluginHelper._PluginHelper__append_cache_buster(url)
        with patch("app.helper.plugin.time.time_ns", return_value=1234567890):
            with fresh(True):
                refreshed_url = PluginHelper._PluginHelper__append_cache_buster(url)

        assert "https://raw.githubusercontent.com/user/repo/main/package.json?_refresh=1234567890" == refreshed_url

    def test_check_plugin_system_version_allows_missing_field(self):
        """
        未声明主系统版本范围时保持旧插件兼容，不做额外限制。
        """
        try:
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
        monkeypatch.setattr(helper, "_PluginHelper__request_with_fallback", lambda *_args, **_kwargs: _FakeTextResponse(200, payload))

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
            from app.core.cache import fresh
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        requested_urls = []

        def fake_request(url, **_kwargs):
            requested_urls.append(url)
            return _FakeTextResponse(200, [])

        helper = PluginHelper()
        helper.get_plugin_release_versions.cache_clear()
        monkeypatch.setattr(helper, "_PluginHelper__request_with_fallback", fake_request)

        with patch("app.helper.plugin.time.time_ns", return_value=1234567890):
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
            from app.helper.plugin import PluginHelper
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

    def test_get_online_plugins_force_clears_release_cache(self, monkeypatch):
        """
        插件市场缓存刷新会一并清理 Release 列表缓存，覆盖定时刷新服务入口。
        """
        try:
            from app.core.plugin import PluginManager
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        clear_calls = []
        fake_release_method = SimpleNamespace(cache_clear=lambda: clear_calls.append("clear"))
        fake_helper = SimpleNamespace(get_plugin_release_versions=fake_release_method)
        monkeypatch.setattr("app.core.plugin.settings.PLUGIN_MARKET", "https://github.com/demo/plugins")
        monkeypatch.setattr("app.core.plugin.PluginHelper", lambda: fake_helper)
        monkeypatch.setattr(PluginManager, "get_plugins_from_market", lambda *_args, **_kwargs: [])

        PluginManager().get_online_plugins(force=True)

        assert clear_calls == ["clear"]

    def test_annotate_plugin_system_version_marks_incompatible(self):
        """
        插件市场列表会带出系统版本兼容状态，供前端禁用安装入口。
        """
        try:
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        plugin_info = {"system_version": ">=2.13.0"}
        with patch.object(PluginHelper, "get_current_system_version", return_value=Version("2.12.2")):
            annotated = PluginHelper.annotate_plugin_system_version(plugin_info)

        assert not annotated["system_version_compatible"]
        assert "当前版本" in annotated["system_version_message"]

    def test_pip_install_keeps_modules_imported_during_install(self):
        """
        验证依赖安装窗口内被其他任务导入的运行态模块不会被误删。
        """
        try:
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        module_names = ["app.plugins.dynamicwechat.helper", "Crypto.Cipher._mode_cbc"]

        def fake_execute(_cmd):
            for module_name in module_names:
                sys.modules[module_name] = ModuleType(module_name)
            return True, "ok"

        # patch.dict 进入时快照 sys.modules、退出时整体还原，替代手写逐项 save/restore；
        # 保证 fake_execute 在安装窗口注入的运行态模块在用例结束后被清理、不污染其他用例
        with patch.dict(sys.modules):
            with tempfile.TemporaryDirectory() as temp_dir:
                requirements_file = Path(temp_dir) / "requirements.txt"
                requirements_file.write_text("demo-package\n", encoding="utf-8")
                with patch("app.helper.plugin.SystemUtils.execute_with_subprocess", side_effect=fake_execute):
                    success, message = PluginHelper.pip_install_with_fallback(requirements_file)

            assert success
            assert "ok" == message
            for module_name in module_names:
                assert module_name in sys.modules

    def test_pip_install_serializes_concurrent_calls(self):
        """
        验证多个依赖安装请求会复用同一把锁串行执行 pip。
        """
        try:
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        thread_count = 2
        active_installs = 0
        max_active_installs = 0
        state_lock = threading.Lock()
        start_event = threading.Event()
        errors = []

        def fake_execute(_cmd):
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
                PluginHelper.pip_install_with_fallback(requirements_file)
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
            with patch("app.helper.plugin.SystemUtils.execute_with_subprocess", side_effect=fake_execute):
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
            from app.helper.plugin import PluginHelper
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

    def test_pip_install_rejects_conflicting_runtime_dependency(self):
        """
        验证插件如果试图覆盖主程序核心依赖，会在真正执行 pip 前被直接拒绝。
        """
        try:
            from app.helper.plugin import PluginHelper
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
                success, message = PluginHelper.pip_install_with_fallback(requirements_file)

        assert not success
        assert "主程序核心依赖" in message
        assert "fastapi" in message

    def test_pip_install_allows_changing_non_runtime_dependency(self):
        """
        验证非主程序依赖即便已安装，插件后续仍可调整其版本约束。
        """
        try:
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        seen_install_commands = []

        def fake_execute(cmd):
            if cmd[:4] == [sys.executable, "-m", "pip", "install"]:
                seen_install_commands.append(cmd)
                assert "-c" not in cmd
                return True, "ok"
            return True, "ok"

        with tempfile.TemporaryDirectory() as temp_dir:
            requirements_file = Path(temp_dir) / "requirements.txt"
            requirements_file.write_text("demo-package>=2\n", encoding="utf-8")
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
                    with patch("app.helper.plugin.SystemUtils.execute_with_subprocess", side_effect=fake_execute):
                        success, message = PluginHelper.pip_install_with_fallback(requirements_file)

        assert success
        assert "ok" == message
        assert 1 == len(seen_install_commands)

    def test_pip_install_uses_runtime_constraints_file(self):
        """
        验证插件依赖安装会固定主程序依赖的当前版本，防止共享 venv 被改写。
        """
        try:
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        seen_constraints = []

        def fake_execute(cmd):
            if cmd[:4] == [sys.executable, "-m", "pip", "install"]:
                constraint_index = cmd.index("-c") + 1
                constraint_file = Path(cmd[constraint_index])
                seen_constraints.append(constraint_file)
                assert constraint_file.exists()
                assert "fastapi==0.115.14" in constraint_file.read_text(encoding="utf-8")
                return True, "ok"
            return True, "ok"

        with tempfile.TemporaryDirectory() as temp_dir:
            requirements_file = Path(temp_dir) / "requirements.txt"
            requirements_file.write_text("demo-package\n", encoding="utf-8")
            with patch.object(
                    PluginHelper,
                    "_PluginHelper__get_protected_runtime_packages",
                    return_value={"fastapi": Version("0.115.14")}
            ):
                with patch("app.helper.plugin.SystemUtils.execute_with_subprocess", side_effect=fake_execute):
                    success, message = PluginHelper.pip_install_with_fallback(requirements_file)

        assert success
        assert "ok" == message
        assert 1 == len(seen_constraints)
        assert not seen_constraints[0].exists()

    def test_pip_install_repairs_runtime_when_healthcheck_fails(self):
        """
        验证插件依赖安装后若破坏运行环境，会先恢复主程序依赖，再向上层返回失败。
        """
        try:
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        repair_commands = []
        healthcheck_failed = False
        pip_check_cmd = PluginHelper._PluginHelper__build_runtime_pip_command("check")

        def fake_execute(cmd):
            nonlocal healthcheck_failed
            if cmd[:4] == [sys.executable, "-m", "pip", "install"]:
                if "-c" not in cmd:
                    repair_commands.append(cmd)
                    return True, "repaired"
                return True, "installed"
            if cmd == pip_check_cmd:
                if not healthcheck_failed:
                    healthcheck_failed = True
                    return False, "broken"
                return True, "healthy"
            if len(cmd) >= 3 and cmd[1] == "-c":
                return True, "probe ok"
            raise AssertionError(f"unexpected command: {cmd}")

        with tempfile.TemporaryDirectory() as temp_dir:
            requirements_file = Path(temp_dir) / "requirements.txt"
            requirements_file.write_text("demo-package\n", encoding="utf-8")
            with patch.object(
                    PluginHelper,
                    "_PluginHelper__get_protected_runtime_packages",
                    return_value={"fastapi": Version("0.115.14")}
            ):
                with patch("app.helper.plugin.SystemUtils.execute_with_subprocess", side_effect=fake_execute):
                    success, message = PluginHelper.pip_install_with_fallback(requirements_file)

        assert not success
        assert "已自动恢复主程序依赖" in message
        assert 1 == len(repair_commands)
        assert "runtime-constraints-" in repair_commands[0][-1]

    def test_async_pip_install_runs_in_threadpool(self):
        """
        验证异步安装路径会把同步 pip 安装派发到线程池，避免阻塞事件循环。
        """
        try:
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        requirements_file = Path("/tmp/demo-requirements.txt")
        find_links_dirs = [Path("/tmp/demo-wheels")]
        calls = []

        async def run_install():
            return await helper._PluginHelper__async_pip_install_with_fallback(
                requirements_file,
                find_links_dirs
            )

        async def fake_to_thread(func, *args, **kwargs):
            calls.append((func, args, kwargs))
            return True, "ok"

        with patch("app.helper.plugin.asyncio.to_thread", side_effect=fake_to_thread):
            success, message = asyncio.run(run_install())

        assert success
        assert "ok" == message
        assert 1 == len(calls)
        assert helper.pip_install_with_fallback == calls[0][0]
        assert (requirements_file, find_links_dirs) == calls[0][1]
        assert {} == calls[0][2]

    def test_install_uses_release_package_when_asset_is_available(self, monkeypatch):
        """
        release 包可用时优先使用 zip 安装，不再额外访问文件列表。
        """
        try:
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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

    def test_install_local_delegates_local_repo_url(self, monkeypatch):
        """
        local:// 来源由本地插件安装路径处理，不访问远端仓库。
        """
        try:
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        monkeypatch.setattr(helper, "install_local", lambda pid, repo_url, force_install=False: (True, f"{pid}:{repo_url}"))

        success, message = helper.install(PLUGIN_ID, f"local://{PLUGIN_ID}?path=/tmp/plugins")

        assert success
        assert message.startswith(f"{PLUGIN_ID}:local://{PLUGIN_ID}")

    def test_install_release_download_failure_falls_back_to_filelist(self, monkeypatch):
        """
        release tag 存在但 zip 下载失败时清理可能残留的目录，再回退文件列表安装。
        """
        try:
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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

    def test_install_from_release_extracts_zip_with_top_level_directory(self, monkeypatch, tmp_path):
        """
        release zip 带顶层插件目录时剥离该层后写入运行目录。
        """
        try:
            from app.helper.plugin import PluginHelper
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
        monkeypatch.setattr("app.helper.plugin.settings", SimpleNamespace(
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
            from app.helper.plugin import PluginHelper
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
        monkeypatch.setattr("app.helper.plugin.settings", SimpleNamespace(
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
        monkeypatch.setattr("app.helper.plugin.settings", SimpleNamespace(
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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

    def test_prepare_content_via_filelist_sync_preinstalls_requirements_and_downloads(self, monkeypatch):
        """
        文件列表安装会先尝试 requirements 预安装，再下载插件文件。
        """
        try:
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        calls = []
        requirements = {"name": "requirements.txt", "download_url": "https://example.com/requirements.txt"}
        file_list = [requirements, {"name": "__init__.py", "download_url": "https://example.com/__init__.py"}]
        monkeypatch.setattr(helper, "_PluginHelper__get_file_list", lambda *_args: (file_list, ""))
        monkeypatch.setattr(
            helper,
            "_PluginHelper__download_and_install_requirements",
            lambda *_args: calls.append("requirements") or (True, ""),
        )
        monkeypatch.setattr(
            helper,
            "_PluginHelper__download_files",
            lambda *_args: calls.append("download") or (True, ""),
        )

        success, message = helper._PluginHelper__prepare_content_via_filelist_sync("demoplugin", "demo/repo", "v2")

        assert success
        assert "" == message
        assert ["requirements", "download"] == calls

    def test_prepare_content_via_filelist_sync_continues_when_requirements_preinstall_fails(self, monkeypatch):
        """
        requirements 预安装失败不阻断文件下载，最终依赖安装由统一流程兜底。
        """
        try:
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        calls = []
        file_list = [{"name": "requirements.txt"}, {"name": "__init__.py"}]
        monkeypatch.setattr(helper, "_PluginHelper__get_file_list", lambda *_args: (file_list, ""))
        monkeypatch.setattr(
            helper,
            "_PluginHelper__download_and_install_requirements",
            lambda *_args: calls.append("requirements") or (False, "preinstall failed"),
        )
        monkeypatch.setattr(
            helper,
            "_PluginHelper__download_files",
            lambda *_args: calls.append("download") or (True, ""),
        )

        success, message = helper._PluginHelper__prepare_content_via_filelist_sync("demoplugin", "demo/repo", "v2")

        assert success
        assert "" == message
        assert ["requirements", "download"] == calls

    def test_prepare_content_via_filelist_sync_reports_missing_file_list(self, monkeypatch):
        """
        文件列表为空时直接返回列表获取错误。
        """
        try:
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        monkeypatch.setattr(helper, "_PluginHelper__get_file_list", lambda *_args: ([{"name": "__init__.py"}], ""))
        monkeypatch.setattr(helper, "_PluginHelper__download_files", lambda *_args: (False, "download failed"))

        success, message = helper._PluginHelper__prepare_content_via_filelist_sync("demoplugin", "demo/repo", "v2")

        assert not success
        assert "download failed" == message

    def test_async_prepare_content_via_filelist_preinstalls_requirements_and_downloads(self, monkeypatch):
        """
        异步文件列表安装会先尝试 requirements 预安装，再下载插件文件。
        """
        try:
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        calls = []
        requirements = {"name": "requirements.txt", "download_url": "https://example.com/requirements.txt"}
        file_list = [requirements, {"name": "__init__.py", "download_url": "https://example.com/__init__.py"}]

        async def fake_file_list(*_args):
            return file_list, ""

        async def fake_requirements(*_args):
            calls.append("requirements")
            return True, ""

        async def fake_download(*_args):
            calls.append("download")
            return True, ""

        monkeypatch.setattr(helper, "_PluginHelper__async_get_file_list", fake_file_list)
        monkeypatch.setattr(helper, "_PluginHelper__async_download_and_install_requirements", fake_requirements)
        monkeypatch.setattr(helper, "_PluginHelper__async_download_files", fake_download)

        success, message = asyncio.run(
            helper._PluginHelper__prepare_content_via_filelist_async("demoplugin", "demo/repo", "v2")
        )

        assert success
        assert "" == message
        assert ["requirements", "download"] == calls

    def test_async_prepare_content_via_filelist_reports_missing_file_list(self, monkeypatch):
        """
        异步文件列表为空时直接返回列表获取错误。
        """
        try:
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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

    def test_async_install_from_release_extracts_zip_with_top_level_directory(self, monkeypatch, tmp_path):
        """
        异步 release zip 带顶层插件目录时剥离该层后写入运行目录。
        """
        try:
            from app.core.config import settings
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        helper = PluginHelper()
        responses = iter([
            _FakeResponse(200, {"assets": [{"name": "demoplugin_v1.2.3.zip", "id": 42}]}),
            _FakeContentResponse(200, _build_zip({"demoplugin/__init__.py": b"plugin"})),
        ])

        async def fake_request(*_args, **_kwargs):
            return next(responses)

        monkeypatch.setattr("app.helper.plugin.settings", SimpleNamespace(
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
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
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            pytest.skip(f"missing dependency: {exc}")

        success, message = PluginHelper().install("DemoPlugin", "local://OtherPlugin?path=/tmp/plugins")

        assert not success
        assert "本地插件来源与插件ID不匹配" == message
