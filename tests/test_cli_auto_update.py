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

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "app" / "cli.py"


class _DummySystemHelper:
    """隔离一次性更新请求的系统状态。"""

    @staticmethod
    def consume_one_shot_dev_update():
        """默认没有用户确认的更新请求。"""
        return False


def load_cli_module():
    """隔离加载 CLI，使用真实布尔配置形状验证启动更新决策。"""
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
            MOVIEPILOT_AUTO_UPDATE=False,
            MOVIEPILOT_UPDATE_DEV=False,
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


def test_resolve_dev_update_target_keeps_dev_branch_tracking():
    """确认的 DEV 更新继续跟踪当前开发分支。"""
    module = load_cli_module()
    with patch.object(module, "_git_current_branch", return_value="v3"):
        assert module._resolve_dev_update_target("dev") == "latest"
    assert module._resolve_dev_update_target("release") is None


def test_one_shot_dev_update_overrides_disabled_default():
    """一次性手动更新不受两个自动开关关闭的影响。"""
    module = load_cli_module()
    module.settings.MOVIEPILOT_AUTO_UPDATE = False

    with patch.object(
        module.SystemHelper, "consume_one_shot_dev_update", return_value=True
    ):
        assert module._requested_update_mode() == "dev"


@pytest.mark.parametrize("auto_update", [True, False])
@pytest.mark.parametrize("update_dev", [True, False])
def test_dev_tracking_is_independent_of_automatic_checks(auto_update, update_dev):
    """配置开关不能替代用户明确发起的更新请求。"""
    module = load_cli_module()
    module.settings.MOVIEPILOT_AUTO_UPDATE = auto_update
    module.settings.MOVIEPILOT_UPDATE_DEV = update_dev
    assert module._requested_update_mode() == "false"


def test_release_mode_does_not_update_during_start():
    """没有一次性 DEV 请求时不执行在线更新。"""
    module = load_cli_module()
    with patch.object(module, "_requested_update_mode", return_value="release"), patch.object(
        module.subprocess, "run"
    ) as run_mock:
        module._apply_requested_update()
    run_mock.assert_not_called()


def test_prepared_release_uses_downloaded_package_before_dev_mode():
    """确认的离线安装包优先于一次性 DEV 更新。"""
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

    with patch.object(module, "_requested_update_mode", return_value="dev") as mode, patch.object(
        module.subprocess, "run", return_value=run_result
    ) as run_mock, patch.object(module.click, "echo"):
        module._apply_requested_update()

    command = run_mock.call_args.args[0]
    assert "--offline-backend" in command
    assert command[command.index("--frontend-archive") + 1] == str(frontend)
    assert not module.PREPARED_UPDATE_MANIFEST.exists()
    mode.assert_not_called()


def test_prepared_release_passes_package_env_and_overrides_proxy():
    """离线更新子进程继承缓存目录和当前代理。"""
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


def test_apply_requested_update_does_not_pass_frontend_version_override():
    """明确 DEV 模式由安装器解析最新前端 Release。"""
    module = load_cli_module()
    run_result = SimpleNamespace(returncode=0, stdout="ok")

    with patch.object(module, "_requested_update_mode", return_value="dev"), patch.object(
        module, "_resolve_dev_update_target", return_value="latest"
    ), patch.object(module.subprocess, "run", return_value=run_result) as run_mock, patch.object(
        module.click, "echo"
    ):
        module._apply_requested_update()

    command = run_mock.call_args.args[0]
    assert command[1:6] == [
        str(module._repo_root() / "scripts" / "local_setup.py"),
        "update",
        "all",
        "--dev",
        "--ref",
    ]
    assert "--frontend-version" not in command
    assert "--dev" in command


def test_apply_requested_update_passes_package_env_and_overrides_proxy():
    """DEV 更新使用配置中的包缓存和代理。"""
    module = load_cli_module()
    module.settings.PROXY_HOST = "http://proxy.example:7890"
    module.settings.PIP_PROXY = "https://mirror.example/simple"
    run_result = SimpleNamespace(returncode=0, stdout="ok")

    with patch.dict(module.os.environ, {"HTTPS_PROXY": "http://old.example:8080"}, clear=True), patch.object(
        module, "_requested_update_mode", return_value="dev"
    ), patch.object(module, "_resolve_dev_update_target", return_value="latest"), patch.object(
        module.subprocess, "run", return_value=run_result
    ) as run_mock, patch.object(module.click, "echo"):
        module._apply_requested_update()

    env = run_mock.call_args.kwargs["env"]
    assert env["HTTPS_PROXY"] == "http://proxy.example:7890"
    assert env["PIP_PROXY"] == "https://mirror.example/simple"
    assert env["PACKAGE_CACHE_ROOT"] == str(module.settings.PACKAGE_CACHE_PATH)
    assert env["UV_CACHE_DIR"] == str(module.settings.PACKAGE_CACHE_PATH / "uv")


def test_apply_requested_update_derives_tool_cache_from_existing_root():
    """已有包缓存根目录优先于配置默认值。"""
    module = load_cli_module()
    run_result = SimpleNamespace(returncode=0, stdout="ok")
    package_cache_root = Path("/custom/package-cache-root")

    with patch.dict(
        module.os.environ,
        {"PACKAGE_CACHE_ROOT": str(package_cache_root)},
        clear=True,
    ), patch.object(module, "_requested_update_mode", return_value="dev"), patch.object(
        module, "_resolve_dev_update_target", return_value="latest"
    ), patch.object(module.subprocess, "run", return_value=run_result) as run_mock, patch.object(
        module.click, "echo"
    ):
        module._apply_requested_update()

    env = run_mock.call_args.kwargs["env"]
    assert env["PACKAGE_CACHE_ROOT"] == str(package_cache_root)
    assert env["UV_CACHE_DIR"] == str(package_cache_root / "uv")


def _prepared_resource_files(module):
    """构造带摘要的离线资源包清单。"""
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
    """资源更新仅应用已校验的本地资源包。"""
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
    """同时更新时先安装主程序再安装资源包。"""
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


@pytest.mark.parametrize("command", ["start", "restart"])
@pytest.mark.parametrize("dev", [False, True])
def test_service_start_never_applies_updates(command, dev):
    """普通启动和重启均不能读取更新请求或执行安装，即使开启 DEV。"""
    from click.testing import CliRunner

    module = load_cli_module()
    module.settings.MOVIEPILOT_UPDATE_DEV = dev
    result = {
        "runtime": {"port": 3001}, "process": SimpleNamespace(pid=123),
        "started": True, "health": {},
    }
    with patch.object(module, "_ensure_frontend_not_running_alone"), patch.object(
        module, "_start_backend_service", return_value=result
    ), patch.object(module, "_start_frontend_service", return_value=result), patch.object(
        module, "_stop_backend_service"
    ), patch.object(module, "_stop_frontend_service"), patch.object(
        module, "_apply_requested_update"
    ) as update:
        response = CliRunner().invoke(module.cli, [command])
    assert response.exit_code == 0, response.output
    update.assert_not_called()


def test_confirmed_update_runs_between_stop_and_start():
    """系统确认的升级通过显式内部入口执行，安装完成后再启动服务。"""
    from click.testing import CliRunner

    module = load_cli_module()
    calls = []
    with patch.object(module, "_stop_frontend_service", side_effect=lambda **_: calls.append("stop_frontend")), patch.object(
        module, "_stop_backend_service", side_effect=lambda **_: calls.append("stop_backend")
    ), patch.object(module, "_apply_requested_update", side_effect=lambda: calls.append("update")), patch.object(
        module, "_start_backend_service", side_effect=lambda **_: calls.append("start_backend") or {"runtime": {"port": 3001}}
    ), patch.object(module, "_start_frontend_service", return_value={"runtime": {"port": 3000}}):
        response = CliRunner().invoke(module.cli, ["restart", "--apply-update"])
    assert response.exit_code == 0, response.output
    assert calls == ["stop_frontend", "stop_backend", "update", "start_backend"]


@pytest.mark.parametrize("pending", [None, "release", "dev"])
def test_restart_helper_only_applies_confirmed_updates(monkeypatch, tmp_path, pending):
    """内部重启助手只在明确的升级清单或一次性请求存在时传递更新选项。"""
    import subprocess
    import time

    from app.runtime.state import SystemHelper

    for attribute, name in (
        ("_SystemHelper__prepared_update_manifest", "release"),
        ("_SystemHelper__one_shot_dev_update_flag_file", "dev"),
        ("_SystemHelper__local_restart_log_file", "restart.log"),
    ):
        path = tmp_path / name
        monkeypatch.setattr(SystemHelper, attribute, path)
        if pending == name:
            path.touch()
    with patch.object(subprocess, "Popen", return_value=SimpleNamespace(pid=123)) as spawn:
        SystemHelper._spawn_local_restart_helper()
    helper_code = spawn.call_args.args[0][2]
    with patch.object(time, "sleep"), patch.object(subprocess, "run") as run:
        exec(helper_code, {})
    assert ("--apply-update" in run.call_args.args[0]) is (pending is not None)
