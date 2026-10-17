"""
验证本地安装目录、解释器、包环境及命令脱敏规则。
"""

from __future__ import annotations

import importlib.util
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "local_setup.py"


def load_local_setup_module():
    """
    按源码路径加载本地安装脚本，避免启动命令行入口。
    """
    module_name = f"moviepilot_local_setup_config_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_setup_prompts_for_config_dir_when_not_provided() -> None:
    """
    安装命令未指定配置目录时提示用户选择。
    """
    module = load_local_setup_module()
    default_dir = Path("/tmp/default-moviepilot-config")
    custom_dir = Path("/tmp/custom-moviepilot-config")

    with (
        patch.object(module, "_is_interactive", return_value=True),
        patch.object(module, "resolve_config_dir", return_value=default_dir),
        patch.object(module, "_prompt_path", return_value=str(custom_dir)),
    ):
        result = module._resolve_interactive_config_dir("setup", None)

    assert result == custom_dir


def test_setup_keeps_default_config_dir_when_user_accepts_default() -> None:
    """
    用户接受默认值时保留默认配置目录。
    """
    module = load_local_setup_module()
    default_dir = Path("/tmp/default-moviepilot-config")

    with (
        patch.object(module, "_is_interactive", return_value=True),
        patch.object(module, "resolve_config_dir", return_value=default_dir),
        patch.object(module, "_prompt_path", return_value=str(default_dir)),
    ):
        result = module._resolve_interactive_config_dir("init", None)

    assert result == default_dir


def test_non_setup_command_does_not_prompt_for_config_dir() -> None:
    """
    非安装命令不弹出配置目录选择。
    """
    module = load_local_setup_module()

    with (
        patch.object(module, "_is_interactive", return_value=True),
        patch.object(module, "_prompt_path") as prompt_mock,
    ):
        result = module._resolve_interactive_config_dir("install-deps", None)

    assert result is None
    prompt_mock.assert_not_called()


def test_supported_python_accepts_versions_newer_than_current_ci() -> None:
    """
    Python 版本高于当前 CI 版本时仍允许安装。
    """
    module = load_local_setup_module()

    with patch.object(module, "get_python_version", return_value=(3, 15, 0)):
        module.ensure_supported_python("python3.15")


def test_supported_python_rejects_versions_below_3_14() -> None:
    """
    拒绝低于 Python 3.14 的解释器。
    """
    module = load_local_setup_module()

    with patch.object(module, "get_python_version", return_value=(3, 13, 9)):
        with pytest.raises(RuntimeError, match="Python 3\\.14\\+"):
            module.ensure_supported_python("python3.13")


def test_install_deps_installs_browser_runtime() -> None:
    """
    安装依赖流程同时安装所需浏览器运行时。
    """
    module = load_local_setup_module()

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        venv_dir = (root / "venv").resolve()
        venv_python = venv_dir / "bin" / "python"
        uv_bin = root / "tools" / "uv"

        with (
            patch.object(module, "ensure_supported_python"),
            patch.object(module, "require_uv", return_value=uv_bin),
            patch.object(module, "expose_uv_to_venv") as expose_uv,
            patch.object(module, "run") as run_mock,
            patch.object(module, "install_browser_runtime") as install_browser,
        ):
            result = module.install_deps(
                python_bin="python3",
                venv_dir=venv_dir,
                recreate=False,
            )

    assert result == venv_python
    command = run_mock.call_args.args[0]
    assert command == [
        str(uv_bin),
        "sync",
        "--project",
        str(module.ROOT),
        "--locked",
        "--no-dev",
        "--no-install-project",
        "--python",
        "python3",
    ]
    assert run_mock.call_args.kwargs["env"]["UV_PROJECT_ENVIRONMENT"] == str(venv_dir)
    expose_uv.assert_called_once_with(uv_bin, venv_dir)
    install_browser.assert_called_once_with(venv_python)


def test_package_install_env_maps_proxy_cache_and_index() -> None:
    """
    包安装环境正确映射代理、缓存与镜像配置。
    """
    module = load_local_setup_module()

    with (
        tempfile.TemporaryDirectory() as temp_dir,
        patch.dict(
            module.os.environ,
            {
                "PROXY_HOST": "http://proxy.example:7890",
                "PIP_PROXY": "https://user:pass@mirror.example/simple",
                "PACKAGE_CACHE_ROOT": str(Path(temp_dir) / "custom-package-cache"),
            },
            clear=True,
        ),
    ):
        module.CONFIG_DIR = Path(temp_dir)
        env = module.build_package_install_env()

    assert env["HTTPS_PROXY"] == "http://proxy.example:7890"
    assert env["PACKAGE_CACHE_ROOT"] == str(Path(temp_dir) / "custom-package-cache")
    assert env["UV_CACHE_DIR"] == str(Path(temp_dir) / "custom-package-cache" / "uv")
    assert env["UV_DEFAULT_INDEX"] == "https://user:pass@mirror.example/simple"
    assert "PIP_CACHE_DIR" not in env
    assert "PIP_INDEX_URL" not in env


def test_package_install_env_defaults_cache_to_config_dir() -> None:
    """
    未显式配置缓存时使用配置目录下的缓存路径。
    """
    module = load_local_setup_module()

    with (
        tempfile.TemporaryDirectory() as temp_dir,
        patch.dict(
            module.os.environ,
            {},
            clear=True,
        ),
    ):
        module.CONFIG_DIR = Path(temp_dir)
        env = module.build_package_install_env()

    assert env["PACKAGE_CACHE_ROOT"] == str(Path(temp_dir) / ".cache")
    assert env["UV_CACHE_DIR"] == str(Path(temp_dir) / ".cache" / "uv")
    assert "PIP_CACHE_DIR" not in env


def test_package_install_env_preserves_explicit_uv_cache_dir() -> None:
    """
    显式指定的 uv 缓存目录不被默认值覆盖。
    """
    module = load_local_setup_module()

    with (
        tempfile.TemporaryDirectory() as temp_dir,
        patch.dict(
            module.os.environ,
            {
                "UV_CACHE_DIR": "/custom/uv-cache",
                "PACKAGE_CACHE_ROOT": "/custom/custom-package-cache",
            },
            clear=True,
        ),
    ):
        module.CONFIG_DIR = Path(temp_dir)
        env = module.build_package_install_env()

    assert env["PACKAGE_CACHE_ROOT"] == "/custom/custom-package-cache"
    assert env["UV_CACHE_DIR"] == "/custom/uv-cache"


def test_run_redacts_safe_command() -> None:
    """
    命令日志隐藏安全展示参数中的凭据。
    """
    module = load_local_setup_module()

    with patch.object(module.subprocess, "run"), patch("builtins.print") as print_mock:
        module.run(
            [
                "uv",
                "sync",
                "--default-index",
                "https://user:pass@mirror.example/simple",
            ],
            safe_command=[
                "uv",
                "sync",
                "--default-index",
                "https://mirror.example/simple",
            ],
        )

    printed = " ".join(str(call.args[0]) for call in print_mock.call_args_list)
    assert "https://mirror.example/simple" in printed
    assert "user:pass" not in printed


def test_redact_command_handles_inline_index_url() -> None:
    """
    内联镜像 URL 中的凭据被命令脱敏覆盖。
    """
    module = load_local_setup_module()

    command = [
        "uv",
        "sync",
        "--default-index=https://user:pass@mirror.example/simple",
    ]

    redacted = module.redact_command(command)

    assert "--default-index=https://mirror.example/simple" in redacted
    assert "user:pass" not in " ".join(redacted)


def test_redact_command_handles_url_query_equals() -> None:
    """
    URL 查询参数包含等号时仍正确脱敏。
    """
    module = load_local_setup_module()

    command = [
        "uv",
        "sync",
        "https://user:pass@mirror.example/simple?token=abc",
    ]

    redacted = module.redact_command(command)

    assert "https://mirror.example/simple?token=abc" in redacted
    assert "user:pass" not in " ".join(redacted)


def test_require_uv_accepts_minimum_version() -> None:
    """
    允许满足最低版本要求的 uv。
    """
    module = load_local_setup_module()
    uv_bin = Path("/opt/moviepilot/bin/uv")

    with (
        patch.object(module.shutil, "which", return_value=str(uv_bin)),
        patch.object(module, "capture", return_value=f"uv {module.MIN_UV_VERSION_TEXT} (test-target)"),
    ):
        result = module.require_uv()

    assert result == uv_bin.resolve()


def test_require_uv_accepts_newer_version() -> None:
    """
    允许高于最低要求的 uv 版本。
    """
    module = load_local_setup_module()
    uv_bin = Path("/opt/moviepilot/bin/uv")

    with (
        patch.object(module.shutil, "which", return_value=str(uv_bin)),
        patch.object(module, "capture", return_value="uv 1.0.0 (test-target)"),
    ):
        result = module.require_uv()

    assert result == uv_bin.resolve()


def test_require_uv_rejects_older_version() -> None:
    """
    拒绝低于最低要求的 uv 版本。
    """
    module = load_local_setup_module()
    uv_bin = Path("/opt/moviepilot/bin/uv")

    with (
        patch.object(module.shutil, "which", return_value=str(uv_bin)),
        patch.object(module, "capture", return_value="uv 0.12.4 (test-target)"),
        pytest.raises(RuntimeError, match="需要 uv 0.12.5\\+"),
    ):
        module.require_uv()


def test_windows_expose_uv_keeps_existing_source_when_target_is_same() -> None:
    """
    Windows 暴露 uv 时源和目标相同则保留原文件。
    """
    module = load_local_setup_module()

    with tempfile.TemporaryDirectory() as temp_dir:
        venv_dir = Path(temp_dir) / "venv"
        uv_bin = venv_dir / "Scripts" / "uv.exe"
        uv_bin.parent.mkdir(parents=True)
        uv_bin.write_bytes(b"uv-binary")

        with patch.object(module.os, "name", "nt"):
            result = module.expose_uv_to_venv(uv_bin, venv_dir)

        assert result == uv_bin
        assert uv_bin.read_bytes() == b"uv-binary"


def test_recreate_preserves_uv_located_inside_old_venv() -> None:
    """
    重建环境前保存旧环境中的 uv 可执行文件。
    """
    module = load_local_setup_module()
    commands = []

    with tempfile.TemporaryDirectory() as temp_dir:
        venv_dir = (Path(temp_dir) / "venv").resolve()
        uv_bin = venv_dir / "bin" / "uv"
        uv_bin.parent.mkdir(parents=True)
        uv_bin.write_bytes(b"uv-binary")

        def fake_run(command, **_kwargs):
            """
            确认待执行的 uv 已保留到有效路径，并记录命令而不运行外部进程。
            """
            assert Path(command[0]).is_file()
            commands.append(command)

        with (
            patch.object(module, "ensure_supported_python"),
            patch.object(module, "require_uv", return_value=uv_bin),
            patch.object(module, "install_browser_runtime"),
            patch.object(module, "run", side_effect=fake_run),
        ):
            module.install_deps(
                python_bin="python.exe",
                venv_dir=venv_dir,
                recreate=True,
            )

        assert len(commands) == 1
        assert Path(commands[0][0]) != uv_bin
        assert "--inexact" not in commands[0]
        assert uv_bin.read_bytes() == b"uv-binary"


def test_recreate_rejects_python_from_target_venv() -> None:
    """
    拒绝用待重建环境内的 Python 重建该环境。
    """
    module = load_local_setup_module()

    with tempfile.TemporaryDirectory() as temp_dir:
        venv_dir = (Path(temp_dir) / "venv").resolve()
        python_bin = venv_dir / "bin" / "python"
        python_bin.parent.mkdir(parents=True)
        python_bin.touch()

        with patch.object(module, "ensure_supported_python"), pytest.raises(RuntimeError, match="venv 外部"):
            module.install_deps(
                python_bin=str(python_bin),
                venv_dir=venv_dir,
                recreate=True,
            )


def test_recreate_rejects_current_python_inside_target_venv() -> None:
    """
    当前进程解释器属于目标环境时拒绝重建。
    """
    module = load_local_setup_module()

    with tempfile.TemporaryDirectory() as temp_dir:
        venv_dir = (Path(temp_dir) / "venv").resolve()
        running_python = venv_dir / "bin" / "python"
        running_python.parent.mkdir(parents=True)
        running_python.touch()

        with (
            patch.object(module, "ensure_supported_python"),
            patch.object(module.sys, "executable", str(running_python)),
            pytest.raises(RuntimeError, match="venv 外部"),
        ):
            module.install_deps(
                python_bin="/usr/bin/python3",
                venv_dir=venv_dir,
                recreate=True,
            )


def test_recreate_resolves_python_command_through_path() -> None:
    """
    重建环境时通过 PATH 解析用户指定的 Python 命令。
    """
    module = load_local_setup_module()

    with tempfile.TemporaryDirectory() as temp_dir:
        venv_dir = (Path(temp_dir) / "venv").resolve()
        path_python = venv_dir / "bin" / "python"
        path_python.parent.mkdir(parents=True)
        path_python.touch()

        with (
            patch.object(module, "ensure_supported_python"),
            patch.object(module.shutil, "which", return_value=str(path_python)),
            pytest.raises(RuntimeError, match="venv 外部"),
        ):
            module.install_deps(
                python_bin="python3",
                venv_dir=venv_dir,
                recreate=True,
            )


def test_windows_install_deps_uses_uv_without_pip_bootstrap() -> None:
    """
    Windows 安装依赖直接使用 uv，无需引导安装 pip。
    """
    module = load_local_setup_module()
    calls = []

    with (
        tempfile.TemporaryDirectory() as temp_dir,
        patch.dict(
            module.os.environ,
            {
                "PROXY_HOST": "http://proxy.example:7890",
                "PIP_PROXY": "https://user:pass@mirror.example/simple",
                "PACKAGE_CACHE_ROOT": str(Path(temp_dir) / "custom-package-cache"),
            },
            clear=True,
        ),
    ):
        root = Path(temp_dir)
        venv_dir = root / "venv"
        venv_python = venv_dir / "Scripts" / "python.exe"
        uv_bin = root / "tools" / "uv.exe"
        module.CONFIG_DIR = root / "config"

        def fake_run(command, cwd=None, env=None, safe_command=None):
            """
            记录包安装命令、环境与脱敏展示参数，不执行外部安装。
            """
            calls.append((command, env, safe_command))

        with (
            patch.object(module.os, "name", "nt"),
            patch.object(module, "ensure_supported_python"),
            patch.object(module, "require_uv", return_value=uv_bin),
            patch.object(module, "expose_uv_to_venv"),
            patch.object(module, "install_browser_runtime"),
            patch.object(module, "run", side_effect=fake_run),
        ):
            module.install_deps(python_bin="python", venv_dir=venv_dir, recreate=False)

    assert len(calls) == 1
    command, env, safe_command = calls[0]
    assert command[:2] == [str(uv_bin), "sync"]
    assert "pip" not in command
    assert env["UV_PROJECT_ENVIRONMENT"] == str(venv_dir.resolve())
    assert env["UV_DEFAULT_INDEX"] == "https://user:pass@mirror.example/simple"
    assert env["HTTPS_PROXY"] == "http://proxy.example:7890"
    assert env["PACKAGE_CACHE_ROOT"] == str(Path(temp_dir) / "custom-package-cache")
    assert env["UV_CACHE_DIR"] == str(Path(temp_dir) / "custom-package-cache" / "uv")
    assert "user:pass" not in " ".join(safe_command or command)


def test_install_deps_uses_package_env_for_project_lock() -> None:
    """
    项目锁文件相关安装命令使用包安装环境配置。
    """
    module = load_local_setup_module()
    calls = []

    with (
        tempfile.TemporaryDirectory() as temp_dir,
        patch.dict(
            module.os.environ,
            {"PIP_PROXY": "https://user:pass@mirror.example/simple"},
            clear=False,
        ),
    ):
        root = Path(temp_dir)
        venv_dir = root / "venv"
        uv_bin = root / "tools" / "uv"
        module.CONFIG_DIR = root / "config"

        def fake_run(command, cwd=None, env=None, safe_command=None):
            """
            记录包安装命令、环境与脱敏展示参数，不执行外部安装。
            """
            calls.append((command, env, safe_command))

        with (
            patch.object(module, "ensure_supported_python"),
            patch.object(module, "require_uv", return_value=uv_bin),
            patch.object(module, "expose_uv_to_venv"),
            patch.object(module, "install_browser_runtime"),
            patch.object(module, "run", side_effect=fake_run),
        ):
            module.install_deps(python_bin="python3", venv_dir=venv_dir, recreate=False)

    project_sync = calls[0]
    assert project_sync[0][:2] == [str(uv_bin), "sync"]
    assert "--locked" in project_sync[0]
    assert project_sync[1]["UV_DEFAULT_INDEX"] == "https://user:pass@mirror.example/simple"
    assert "user:pass" not in " ".join(project_sync[2] or project_sync[0])
