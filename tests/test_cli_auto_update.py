from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import uuid
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "app" / "cli.py"


class _DummySystemHelper:
    @staticmethod
    def consume_one_shot_dev_update():
        return False


def load_cli_module():
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        settings = SimpleNamespace(
            TEMP_PATH=root / "temp",
            LOG_PATH=root / "logs",
            ROOT_PATH=root,
            FRONTEND_PATH=str(root / "public"),
            CONFIG_PATH=root / "config",
            PACKAGE_CACHE_PATH=root / "custom-package-cache",
            HOST="127.0.0.1",
            PORT=3001,
            NGINX_PORT=3000,
            PROXY_HOST="",
            PIP_PROXY="",
            GITHUB_TOKEN="",
            MOVIEPILOT_AUTO_UPDATE="false",
            PROXY={},
            REPO_GITHUB_HEADERS=lambda _repo: {},
        )

        app_module = ModuleType("app")
        core_module = ModuleType("app.core")
        helper_module = ModuleType("app.helper")
        config_module = ModuleType("app.runtime.config")
        system_module = ModuleType("app.runtime.state")
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
            "app.runtime.config": config_module,
            "app.runtime.state": system_module,
            "version": version_module,
            "psutil": psutil_module,
        }

        module_name = f"moviepilot_app_cli_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader

        with patch.dict(sys.modules, stub_modules):
            spec.loader.exec_module(module)
        # CLI 生产代码只依赖读取端口；这个动态加载器仍提供旧字段 patch 点，
        # 让历史更新流程测试可以独立于全局测试配置运行。
        module.settings = settings
        module.get_runtime_setting = lambda key, default=None: getattr(
            settings, key, default
        )
        return module


def test_resolve_auto_update_targets_keeps_dev_branch_tracking():
    module = load_cli_module()
    with patch.object(module, "_git_current_branch", return_value="v3"):
        assert module._resolve_auto_update_targets("dev") == "latest"
    assert module._resolve_auto_update_targets("release") is None


def test_one_shot_dev_update_overrides_disabled_default():
    module = load_cli_module()
    module.settings.MOVIEPILOT_AUTO_UPDATE = "false"

    with patch.object(
        module.SystemHelper, "consume_one_shot_dev_update", return_value=True
    ):
        assert module._auto_update_mode() == "dev"


def test_release_mode_does_not_update_during_start():
    module = load_cli_module()
    with patch.object(module, "_auto_update_mode", return_value="release"), patch.object(
        module.subprocess, "run"
    ) as run_mock:
        module._best_effort_auto_update()
    run_mock.assert_not_called()


def test_prepared_release_uses_downloaded_package_before_dev_mode():
    module = load_cli_module()
    module.PREPARED_UPDATE_ROOT.mkdir(parents=True)
    backend = module.PREPARED_UPDATE_ROOT / "backend.zip"
    frontend = module.PREPARED_UPDATE_ROOT / "frontend.zip"
    backend.write_bytes(b"backend")
    frontend.write_bytes(b"frontend")
    module.PREPARED_UPDATE_MANIFEST.write_text(
        json.dumps(
            {
                "version": "v3.1.0",
                "frontend_version": "v3.1.0",
                "backend_archive": str(backend),
                "frontend_archive": str(frontend),
                "backend_sha256": hashlib.sha256(backend.read_bytes()).hexdigest(),
                "frontend_sha256": hashlib.sha256(frontend.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    run_result = SimpleNamespace(returncode=0, stdout="ok")

    with patch.object(module, "_auto_update_mode", return_value="dev") as mode, patch.object(
        module.subprocess, "run", return_value=run_result
    ) as run_mock, patch.object(module.click, "echo"):
        module._best_effort_auto_update()

    command = run_mock.call_args.args[0]
    assert "--offline-backend" in command
    assert command[command.index("--frontend-archive") + 1] == str(frontend)
    assert not module.PREPARED_UPDATE_MANIFEST.exists()
    mode.assert_not_called()


def test_prepared_release_passes_package_env_and_overrides_proxy():
    module = load_cli_module()
    module.settings.PROXY_HOST = "http://proxy.example:7890"
    module.settings.PIP_PROXY = "https://mirror.example/simple"
    module.PREPARED_UPDATE_ROOT.mkdir(parents=True, exist_ok=True)
    backend = module.PREPARED_UPDATE_ROOT / "backend.zip"
    frontend = module.PREPARED_UPDATE_ROOT / "frontend.zip"
    backend.write_bytes(b"backend")
    frontend.write_bytes(b"frontend")
    module.PREPARED_UPDATE_MANIFEST.write_text(
        json.dumps(
            {
                "version": "v3.1.0",
                "frontend_version": "v3.1.0",
                "backend_archive": str(backend),
                "frontend_archive": str(frontend),
                "backend_sha256": hashlib.sha256(backend.read_bytes()).hexdigest(),
                "frontend_sha256": hashlib.sha256(frontend.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    run_result = SimpleNamespace(returncode=0, stdout="ok")

    with patch.dict(
        module.os.environ,
        {"HTTPS_PROXY": "http://old.example:8080"},
        clear=True,
    ), patch.object(
        module.subprocess, "run", return_value=run_result
    ) as run_mock, patch.object(module.click, "echo"):
        assert module._apply_prepared_release_update() is True

    env = run_mock.call_args.kwargs["env"]
    assert env["HTTPS_PROXY"] == "http://proxy.example:7890"
    assert env["PIP_PROXY"] == "https://mirror.example/simple"
    assert env["PACKAGE_CACHE_ROOT"] == str(module.settings.PACKAGE_CACHE_PATH)
    assert env["UV_CACHE_DIR"] == str(module.settings.PACKAGE_CACHE_PATH / "uv")


def test_best_effort_auto_update_does_not_pass_frontend_version_override():
    module = load_cli_module()
    run_result = SimpleNamespace(returncode=0, stdout="ok")

    with patch.object(module, "_auto_update_mode", return_value="dev"), patch.object(
        module, "_resolve_auto_update_targets", return_value="latest"
    ), patch.object(module.subprocess, "run", return_value=run_result) as run_mock, patch.object(
        module.click, "echo"
    ):
        module._best_effort_auto_update()

    command = run_mock.call_args.args[0]
    assert command[1:5] == [
        str(module._repo_root() / "scripts" / "local_setup.py"),
        "update",
        "all",
        "--ref",
    ]
    assert "--frontend-version" not in command


def test_best_effort_auto_update_passes_package_env_and_overrides_proxy():
    module = load_cli_module()
    module.settings.PROXY_HOST = "http://proxy.example:7890"
    module.settings.PIP_PROXY = "https://mirror.example/simple"
    run_result = SimpleNamespace(returncode=0, stdout="ok")

    with patch.dict(module.os.environ, {"HTTPS_PROXY": "http://old.example:8080"}, clear=True), patch.object(
        module, "_auto_update_mode", return_value="dev"
    ), patch.object(module, "_resolve_auto_update_targets", return_value="latest"), patch.object(
        module.subprocess, "run", return_value=run_result
    ) as run_mock, patch.object(module.click, "echo"):
        module._best_effort_auto_update()

    env = run_mock.call_args.kwargs["env"]
    assert env["HTTPS_PROXY"] == "http://proxy.example:7890"
    assert env["PIP_PROXY"] == "https://mirror.example/simple"
    assert env["PACKAGE_CACHE_ROOT"] == str(module.settings.PACKAGE_CACHE_PATH)
    assert env["UV_CACHE_DIR"] == str(module.settings.PACKAGE_CACHE_PATH / "uv")


def test_best_effort_auto_update_derives_tool_cache_from_existing_root():
    module = load_cli_module()
    run_result = SimpleNamespace(returncode=0, stdout="ok")
    package_cache_root = Path("/custom/package-cache-root")

    with patch.dict(
        module.os.environ,
        {"PACKAGE_CACHE_ROOT": str(package_cache_root)},
        clear=True,
    ), patch.object(module, "_auto_update_mode", return_value="dev"), patch.object(
        module, "_resolve_auto_update_targets", return_value="latest"
    ), patch.object(module.subprocess, "run", return_value=run_result) as run_mock, patch.object(
        module.click, "echo"
    ):
        module._best_effort_auto_update()

    env = run_mock.call_args.kwargs["env"]
    assert env["PACKAGE_CACHE_ROOT"] == str(package_cache_root)
    assert env["UV_CACHE_DIR"] == str(package_cache_root / "uv")


def _prepared_resource_files(module):
    resource_dir = module.PREPARED_UPDATE_ROOT / "resources"
    resource_dir.mkdir(parents=True, exist_ok=True)
    resources = []
    for name, content in (
        ("user.sites.v3.bin", b"index"),
        ("sites.cpython-test.so", b"auth"),
    ):
        path = resource_dir / name
        path.write_bytes(content)
        resources.append(
            {
                "name": name,
                "path": str(path),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    return resources


def test_prepared_resource_update_uses_offline_resource_install_only():
    module = load_cli_module()
    module.PREPARED_UPDATE_ROOT.mkdir(parents=True, exist_ok=True)
    module.PREPARED_UPDATE_MANIFEST.write_text(
        json.dumps({"targets": ["resources"], "resource_files": _prepared_resource_files(module)}),
        encoding="utf-8",
    )
    run_result = SimpleNamespace(returncode=0, stdout="ok")

    with patch.object(module.subprocess, "run", return_value=run_result) as run_mock, patch.object(
        module.click, "echo"
    ):
        assert module._apply_prepared_release_update() is True

    assert len(run_mock.call_args_list) == 1
    command = run_mock.call_args.args[0]
    assert command[1:4] == [str(module._repo_root() / "scripts" / "local_setup.py"), "install-resources", "--resource-dir"]
    assert "update" not in command
    assert not module.PREPARED_UPDATE_MANIFEST.exists()


def test_prepared_application_and_resources_install_in_order():
    module = load_cli_module()
    module.PREPARED_UPDATE_ROOT.mkdir(parents=True, exist_ok=True)
    backend = module.PREPARED_UPDATE_ROOT / "backend.zip"
    frontend = module.PREPARED_UPDATE_ROOT / "frontend.zip"
    backend.write_bytes(b"backend")
    frontend.write_bytes(b"frontend")
    module.PREPARED_UPDATE_MANIFEST.write_text(
        json.dumps(
            {
                "targets": ["application", "resources"],
                "version": "v3.1.0",
                "frontend_version": "v3.1.0",
                "backend_archive": str(backend),
                "frontend_archive": str(frontend),
                "backend_sha256": hashlib.sha256(backend.read_bytes()).hexdigest(),
                "frontend_sha256": hashlib.sha256(frontend.read_bytes()).hexdigest(),
                "resource_files": _prepared_resource_files(module),
            }
        ),
        encoding="utf-8",
    )
    run_result = SimpleNamespace(returncode=0, stdout="ok")

    with patch.object(module.subprocess, "run", return_value=run_result) as run_mock, patch.object(
        module.click, "echo"
    ):
        assert module._apply_prepared_release_update() is True

    commands = [call.args[0] for call in run_mock.call_args_list]
    assert len(commands) == 2
    assert "update" in commands[0]
    assert "install-resources" in commands[1]
    assert not module.PREPARED_UPDATE_MANIFEST.exists()
