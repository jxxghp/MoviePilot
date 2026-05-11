import sys
import tempfile
import threading
import time
from pathlib import Path
from types import ModuleType
from unittest import TestCase
from unittest.mock import patch

from packaging.version import Version


class PluginHelperTest(TestCase):

    def test_sanitize_repo_url_for_statistic_keeps_remote_url(self):
        try:
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            self.skipTest(f"missing dependency: {exc}")
        repo_url = "https://github.com/InfinityPacer/MoviePilot-Plugins"
        self.assertEqual(repo_url, PluginHelper.sanitize_repo_url_for_statistic(repo_url))

    def test_sanitize_repo_url_for_statistic_strips_local_path(self):
        try:
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            self.skipTest(f"missing dependency: {exc}")
        repo_url = "local://TestPlugin?path=/Users/InfinityPacer/GitHub/MoviePilot/MoviePilot-Plugins&version=v2"
        self.assertEqual(
            "local://TestPlugin?version=v2",
            PluginHelper.sanitize_repo_url_for_statistic(repo_url)
        )

    def test_pip_install_keeps_modules_imported_during_install(self):
        """
        验证依赖安装窗口内被其他任务导入的运行态模块不会被误删。
        """
        try:
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            self.skipTest(f"missing dependency: {exc}")

        module_names = ["app.plugins.dynamicwechat.helper", "Crypto.Cipher._mode_cbc"]
        previous_modules = {name: sys.modules.get(name) for name in module_names}

        def fake_execute(_cmd):
            for module_name in module_names:
                sys.modules[module_name] = ModuleType(module_name)
            return True, "ok"

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                requirements_file = Path(temp_dir) / "requirements.txt"
                requirements_file.write_text("demo-package\n", encoding="utf-8")
                with patch("app.helper.plugin.SystemUtils.execute_with_subprocess", side_effect=fake_execute):
                    success, message = PluginHelper.pip_install_with_fallback(requirements_file)

            self.assertTrue(success)
            self.assertEqual("ok", message)
            for module_name in module_names:
                self.assertIn(module_name, sys.modules)
        finally:
            for module_name, previous_module in previous_modules.items():
                if previous_module is None:
                    sys.modules.pop(module_name, None)
                else:
                    sys.modules[module_name] = previous_module

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
                    "_PluginHelper__get_installed_packages",
                    return_value={"fastapi": Version("0.115.14")}
            ):
                success, message = PluginHelper.pip_install_with_fallback(requirements_file)

        self.assertFalse(success)
        self.assertIn("主程序核心依赖", message)
        self.assertIn("fastapi", message)

    def test_pip_install_uses_runtime_constraints_file(self):
        """
        验证插件依赖安装会固定当前运行环境已安装版本，防止共享 venv 被升级或降级。
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
                    "_PluginHelper__get_installed_packages",
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

        def fake_execute(cmd):
            nonlocal healthcheck_failed
            if cmd[:4] == [sys.executable, "-m", "pip", "install"]:
                if "-c" not in cmd:
                    repair_commands.append(cmd)
                    return True, "repaired"
                return True, "installed"
            if cmd[:4] == [sys.executable, "-m", "pip", "check"]:
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
                    "_PluginHelper__get_installed_packages",
                    return_value={"fastapi": Version("0.115.14")}
            ):
                with patch("app.helper.plugin.SystemUtils.execute_with_subprocess", side_effect=fake_execute):
                    success, message = PluginHelper.pip_install_with_fallback(requirements_file)

        self.assertFalse(success)
        self.assertIn("已自动恢复主程序依赖", message)
        self.assertEqual(1, len(repair_commands))
        self.assertIn("runtime-constraints-", repair_commands[0][-1])
