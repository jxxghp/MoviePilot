from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "app" / "cli.py"


class _DummySystemHelper:
    @staticmethod
    def consume_one_shot_update_mode():
        return None

    @staticmethod
    def get_auto_update_mode():
        return "false"


def load_cli_module():
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        settings = SimpleNamespace(
            TEMP_PATH=root / "temp",
            LOG_PATH=root / "logs",
            ROOT_PATH=root,
            FRONTEND_PATH=str(root / "public"),
            CONFIG_PATH=root / "config",
            HOST="127.0.0.1",
            PORT=3001,
            NGINX_PORT=3000,
            PROXY_HOST="",
            GITHUB_TOKEN="",
            PROXY={},
            REPO_GITHUB_HEADERS=lambda _repo: {},
        )

        app_module = ModuleType("app")
        core_module = ModuleType("app.core")
        helper_module = ModuleType("app.helper")
        config_module = ModuleType("app.core.config")
        system_module = ModuleType("app.helper.system")
        version_module = ModuleType("version")
        psutil_module = ModuleType("psutil")

        app_module.__path__ = []
        core_module.__path__ = []
        helper_module.__path__ = []
        config_module.Settings = type("Settings", (), {})
        config_module.settings = settings
        system_module.SystemHelper = _DummySystemHelper
        version_module.APP_VERSION = "v2.10.11"
        psutil_module.STATUS_ZOMBIE = "zombie"
        psutil_module.NoSuchProcess = RuntimeError
        psutil_module.AccessDenied = RuntimeError
        psutil_module.ZombieProcess = RuntimeError
        psutil_module.Process = object

        stub_modules = {
            "app": app_module,
            "app.core": core_module,
            "app.helper": helper_module,
            "app.core.config": config_module,
            "app.helper.system": system_module,
            "version": version_module,
            "psutil": psutil_module,
        }

        module_name = f"moviepilot_app_cli_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader

        with patch.dict(sys.modules, stub_modules):
            spec.loader.exec_module(module)
        return module


class CliAutoUpdateTests(unittest.TestCase):
    def test_resolve_auto_update_targets_only_queries_backend_release(self):
        module = load_cli_module()

        with patch.object(module, "_latest_release_tag", return_value="v2.10.12") as latest_mock:
            backend_ref = module._resolve_auto_update_targets("release")

        latest_mock.assert_called_once_with(
            module.BACKEND_RELEASES_API,
            repo="jxxghp/MoviePilot",
            prefix="v2",
        )
        self.assertEqual(backend_ref, "v2.10.12")

    def test_best_effort_auto_update_does_not_pass_frontend_version_override(self):
        module = load_cli_module()
        run_result = SimpleNamespace(returncode=0, stdout="ok")

        with patch.object(module, "_auto_update_mode", return_value="release"), patch.object(
            module, "_resolve_auto_update_targets", return_value="v2.10.12"
        ), patch.object(module.subprocess, "run", return_value=run_result) as run_mock, patch.object(
            module.click, "echo"
        ):
            module._best_effort_auto_update()

        command = run_mock.call_args.args[0]
        self.assertEqual(command[1:5], [str(module._repo_root() / "scripts" / "local_setup.py"), "update", "all", "--ref"])
        self.assertNotIn("--frontend-version", command)


if __name__ == "__main__":
    unittest.main()
