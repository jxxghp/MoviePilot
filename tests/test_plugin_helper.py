import asyncio
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import ModuleType
from unittest import TestCase
from unittest.mock import patch

from packaging.requirements import Requirement
from packaging.version import Version


class PluginHelperTest(TestCase):

    def test_sanitize_plugin_repo_url_keeps_remote_url(self):
        """
        插件安装统计脱敏保留远端仓库地址。
        """
        try:
            from app.helper.server import MoviePilotServerHelper
        except ModuleNotFoundError as exc:
            self.skipTest(f"missing dependency: {exc}")
        repo_url = "https://github.com/InfinityPacer/MoviePilot-Plugins"
        self.assertEqual(repo_url, MoviePilotServerHelper.sanitize_plugin_repo_url(repo_url))

    def test_sanitize_plugin_repo_url_strips_local_path(self):
        """
        插件安装统计脱敏移除本地仓库绝对路径。
        """
        try:
            from app.helper.server import MoviePilotServerHelper
        except ModuleNotFoundError as exc:
            self.skipTest(f"missing dependency: {exc}")
        repo_url = "local://TestPlugin?path=/Users/InfinityPacer/GitHub/MoviePilot/MoviePilot-Plugins&version=v2"
        self.assertEqual(
            "local://TestPlugin?version=v2",
            MoviePilotServerHelper.sanitize_plugin_repo_url(repo_url)
        )

    def test_append_cache_buster_only_during_fresh_context(self):
        """
        插件库强制刷新时远端索引 URL 也要变化，避免命中镜像或代理缓存。
        """
        try:
            from app.core.cache import fresh
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            self.skipTest(f"missing dependency: {exc}")

        url = "https://raw.githubusercontent.com/user/repo/main/package.json"

        self.assertEqual(url, PluginHelper._PluginHelper__append_cache_buster(url))
        with patch("app.helper.plugin.time.time_ns", return_value=1234567890):
            with fresh(True):
                refreshed_url = PluginHelper._PluginHelper__append_cache_buster(url)

        self.assertEqual(
            "https://raw.githubusercontent.com/user/repo/main/package.json?_refresh=1234567890",
            refreshed_url,
        )

    def test_check_plugin_system_version_allows_missing_field(self):
        """
        未声明主系统版本范围时保持旧插件兼容，不做额外限制。
        """
        try:
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            self.skipTest(f"missing dependency: {exc}")

        success, message = PluginHelper.check_plugin_system_version({"version": "1.0.0"})

        self.assertTrue(success)
        self.assertEqual("", message)

    def test_check_plugin_system_version_rejects_out_of_range(self):
        """
        插件声明的主系统版本范围不满足当前版本时拒绝安装。
        """
        try:
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            self.skipTest(f"missing dependency: {exc}")

        with patch.object(PluginHelper, "get_current_system_version", return_value=Version("2.12.2")):
            success, message = PluginHelper.check_plugin_system_version({"system_version": ">=2.13.0"})

        self.assertFalse(success)
        self.assertIn("MoviePilot 版本 >=2.13.0", message)

    def test_check_plugin_system_version_accepts_v_prefix_specifier(self):
        """
        兼容带 v 前缀的版本范围，降低插件索引维护成本。
        """
        try:
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            self.skipTest(f"missing dependency: {exc}")

        with patch.object(PluginHelper, "get_current_system_version", return_value=Version("2.12.2")):
            success, message = PluginHelper.check_plugin_system_version({"system_version": ">=v2.12.0"})

        self.assertTrue(success)
        self.assertEqual("", message)

    def test_annotate_plugin_system_version_marks_incompatible(self):
        """
        插件市场列表会带出系统版本兼容状态，供前端禁用安装入口。
        """
        try:
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            self.skipTest(f"missing dependency: {exc}")

        plugin_info = {"system_version": ">=2.13.0"}
        with patch.object(PluginHelper, "get_current_system_version", return_value=Version("2.12.2")):
            annotated = PluginHelper.annotate_plugin_system_version(plugin_info)

        self.assertFalse(annotated["system_version_compatible"])
        self.assertIn("当前版本", annotated["system_version_message"])

    def test_pip_install_keeps_modules_imported_during_install(self):
        """
        验证依赖安装窗口内被其他任务导入的运行态模块不会被误删。
        """
        try:
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            self.skipTest(f"missing dependency: {exc}")

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

            self.assertTrue(success)
            self.assertEqual("ok", message)
            for module_name in module_names:
                self.assertIn(module_name, sys.modules)

    def test_pip_install_serializes_concurrent_calls(self):
        """
        验证多个依赖安装请求会复用同一把锁串行执行 pip。
        """
        try:
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            self.skipTest(f"missing dependency: {exc}")

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

        self.assertEqual([], errors)
        self.assertEqual(1, max_active_installs)

    def test_get_protected_runtime_packages_only_keeps_main_dependency_graph(self):
        """
        验证仅主程序依赖链上的包会被纳入保护集合。
        """
        try:
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            self.skipTest(f"missing dependency: {exc}")

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

        self.assertEqual({
            "passlib": Version("1.7.4"),
            "bcrypt": Version("4.0.1"),
        }, protected_packages)

    def test_pip_install_rejects_conflicting_runtime_dependency(self):
        """
        验证插件如果试图覆盖主程序核心依赖，会在真正执行 pip 前被直接拒绝。
        """
        try:
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            self.skipTest(f"missing dependency: {exc}")

        with tempfile.TemporaryDirectory() as temp_dir:
            requirements_file = Path(temp_dir) / "requirements.txt"
            requirements_file.write_text("fastapi<0.1\n", encoding="utf-8")
            with patch.object(
                    PluginHelper,
                    "_PluginHelper__get_protected_runtime_packages",
                    return_value={"fastapi": Version("0.115.14")}
            ):
                success, message = PluginHelper.pip_install_with_fallback(requirements_file)

        self.assertFalse(success)
        self.assertIn("主程序核心依赖", message)
        self.assertIn("fastapi", message)

    def test_pip_install_allows_changing_non_runtime_dependency(self):
        """
        验证非主程序依赖即便已安装，插件后续仍可调整其版本约束。
        """
        try:
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            self.skipTest(f"missing dependency: {exc}")

        seen_install_commands = []

        def fake_execute(cmd):
            if cmd[:4] == [sys.executable, "-m", "pip", "install"]:
                seen_install_commands.append(cmd)
                self.assertNotIn("-c", cmd)
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

        self.assertTrue(success)
        self.assertEqual("ok", message)
        self.assertEqual(1, len(seen_install_commands))

    def test_pip_install_uses_runtime_constraints_file(self):
        """
        验证插件依赖安装会固定主程序依赖的当前版本，防止共享 venv 被改写。
        """
        try:
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            self.skipTest(f"missing dependency: {exc}")

        seen_constraints = []

        def fake_execute(cmd):
            if cmd[:4] == [sys.executable, "-m", "pip", "install"]:
                constraint_index = cmd.index("-c") + 1
                constraint_file = Path(cmd[constraint_index])
                seen_constraints.append(constraint_file)
                self.assertTrue(constraint_file.exists())
                self.assertIn("fastapi==0.115.14", constraint_file.read_text(encoding="utf-8"))
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

        self.assertTrue(success)
        self.assertEqual("ok", message)
        self.assertEqual(1, len(seen_constraints))
        self.assertFalse(seen_constraints[0].exists())

    def test_pip_install_repairs_runtime_when_healthcheck_fails(self):
        """
        验证插件依赖安装后若破坏运行环境，会先恢复主程序依赖，再向上层返回失败。
        """
        try:
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            self.skipTest(f"missing dependency: {exc}")

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

        self.assertFalse(success)
        self.assertIn("已自动恢复主程序依赖", message)
        self.assertEqual(1, len(repair_commands))
        self.assertIn("runtime-constraints-", repair_commands[0][-1])

    def test_async_pip_install_runs_in_threadpool(self):
        """
        验证异步安装路径会把同步 pip 安装派发到线程池，避免阻塞事件循环。
        """
        try:
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            self.skipTest(f"missing dependency: {exc}")

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

        self.assertTrue(success)
        self.assertEqual("ok", message)
        self.assertEqual(1, len(calls))
        self.assertEqual(helper.pip_install_with_fallback, calls[0][0])
        self.assertEqual((requirements_file, find_links_dirs), calls[0][1])
        self.assertEqual({}, calls[0][2])
