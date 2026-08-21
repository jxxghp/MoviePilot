from __future__ import annotations

import importlib.util
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "local_setup.py"


def load_local_setup_module():
    module_name = f"moviepilot_local_setup_config_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class LocalSetupConfigDirTests(unittest.TestCase):
    def test_setup_prompts_for_config_dir_when_not_provided(self):
        module = load_local_setup_module()
        default_dir = Path("/tmp/default-moviepilot-config")
        custom_dir = Path("/tmp/custom-moviepilot-config")

        with patch.object(module, "_is_interactive", return_value=True), patch.object(
            module, "resolve_config_dir", return_value=default_dir
        ), patch.object(
            module, "_prompt_path", return_value=str(custom_dir)
        ):
            result = module._resolve_interactive_config_dir("setup", None)

        self.assertEqual(result, custom_dir)

    def test_setup_keeps_default_config_dir_when_user_accepts_default(self):
        module = load_local_setup_module()
        default_dir = Path("/tmp/default-moviepilot-config")

        with patch.object(module, "_is_interactive", return_value=True), patch.object(
            module, "resolve_config_dir", return_value=default_dir
        ), patch.object(
            module, "_prompt_path", return_value=str(default_dir)
        ):
            result = module._resolve_interactive_config_dir("init", None)

        self.assertEqual(result, default_dir)

    def test_non_setup_command_does_not_prompt_for_config_dir(self):
        module = load_local_setup_module()

        with patch.object(module, "_is_interactive", return_value=True), patch.object(
            module, "_prompt_path"
        ) as prompt_mock:
            result = module._resolve_interactive_config_dir("install-deps", None)

        self.assertIsNone(result)
        prompt_mock.assert_not_called()

    def test_supported_python_accepts_versions_newer_than_current_ci(self):
        module = load_local_setup_module()

        with patch.object(module, "get_python_version", return_value=(3, 15, 0)):
            module.ensure_supported_python("python3.15")

    def test_supported_python_rejects_versions_below_3_14(self):
        module = load_local_setup_module()

        with patch.object(module, "get_python_version", return_value=(3, 13, 9)):
            with self.assertRaisesRegex(RuntimeError, r"Python 3\.14\+"):
                module.ensure_supported_python("python3.13")

    def test_install_deps_installs_browser_runtime(self):
        module = load_local_setup_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            venv_dir = (root / "venv").resolve()
            venv_python = venv_dir / "bin" / "python"
            uv_bin = root / "tools" / "uv"

            with patch.object(module, "ensure_supported_python"), \
                    patch.object(module, "require_uv", return_value=uv_bin), \
                    patch.object(module, "expose_uv_to_venv") as expose_uv, \
                    patch.object(module, "run") as run_mock, \
                    patch.object(module, "install_browser_runtime") as install_browser:
                result = module.install_deps(
                    python_bin="python3",
                    venv_dir=venv_dir,
                    recreate=False,
                )

        self.assertEqual(result, venv_python)
        command = run_mock.call_args.args[0]
        self.assertEqual(
            command,
            [
                str(uv_bin),
                "sync",
                "--project",
                str(module.ROOT),
                "--locked",
                "--no-dev",
                "--no-install-project",
                "--python",
                "python3",
            ],
        )
        self.assertEqual(run_mock.call_args.kwargs["env"]["UV_PROJECT_ENVIRONMENT"], str(venv_dir))
        expose_uv.assert_called_once_with(uv_bin, venv_dir)
        install_browser.assert_called_once_with(venv_python)

    def test_package_install_env_maps_proxy_cache_and_index(self):
        module = load_local_setup_module()

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
                module.os.environ,
                {
                    "PROXY_HOST": "http://proxy.example:7890",
                    "PIP_PROXY": "https://user:pass@mirror.example/simple",
                    "PACKAGE_CACHE_ROOT": str(Path(temp_dir) / "custom-package-cache"),
                },
                clear=True,
        ):
            module.CONFIG_DIR = Path(temp_dir)
            env = module.build_package_install_env()

        self.assertEqual(env["HTTPS_PROXY"], "http://proxy.example:7890")
        self.assertEqual(env["PACKAGE_CACHE_ROOT"], str(Path(temp_dir) / "custom-package-cache"))
        self.assertEqual(env["UV_CACHE_DIR"], str(Path(temp_dir) / "custom-package-cache" / "uv"))
        self.assertEqual(env["UV_DEFAULT_INDEX"], "https://user:pass@mirror.example/simple")
        self.assertNotIn("PIP_CACHE_DIR", env)
        self.assertNotIn("PIP_INDEX_URL", env)

    def test_package_install_env_defaults_cache_to_config_dir(self):
        module = load_local_setup_module()

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
                module.os.environ,
                {},
                clear=True,
        ):
            module.CONFIG_DIR = Path(temp_dir)
            env = module.build_package_install_env()

        self.assertEqual(env["PACKAGE_CACHE_ROOT"], str(Path(temp_dir) / ".cache"))
        self.assertEqual(env["UV_CACHE_DIR"], str(Path(temp_dir) / ".cache" / "uv"))
        self.assertNotIn("PIP_CACHE_DIR", env)

    def test_package_install_env_preserves_explicit_uv_cache_dir(self):
        module = load_local_setup_module()

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
                module.os.environ,
                {
                    "UV_CACHE_DIR": "/custom/uv-cache",
                    "PACKAGE_CACHE_ROOT": "/custom/custom-package-cache",
                },
                clear=True,
        ):
            module.CONFIG_DIR = Path(temp_dir)
            env = module.build_package_install_env()

        self.assertEqual(env["PACKAGE_CACHE_ROOT"], "/custom/custom-package-cache")
        self.assertEqual(env["UV_CACHE_DIR"], "/custom/uv-cache")

    def test_run_redacts_safe_command(self):
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
        self.assertIn("https://mirror.example/simple", printed)
        self.assertNotIn("user:pass", printed)

    def test_redact_command_handles_inline_index_url(self):
        module = load_local_setup_module()

        command = [
            "uv",
            "sync",
            "--default-index=https://user:pass@mirror.example/simple",
        ]

        redacted = module.redact_command(command)

        self.assertIn("--default-index=https://mirror.example/simple", redacted)
        self.assertNotIn("user:pass", " ".join(redacted))

    def test_redact_command_handles_url_query_equals(self):
        module = load_local_setup_module()

        command = [
            "uv",
            "sync",
            "https://user:pass@mirror.example/simple?token=abc",
        ]

        redacted = module.redact_command(command)

        self.assertIn("https://mirror.example/simple?token=abc", redacted)
        self.assertNotIn("user:pass", " ".join(redacted))

    def test_require_uv_accepts_repository_version(self):
        module = load_local_setup_module()
        uv_bin = Path("/opt/moviepilot/bin/uv")

        with patch.object(module.shutil, "which", return_value=str(uv_bin)), patch.object(
            module, "capture", return_value=f"uv {module.UV_VERSION} (test-target)"
        ):
            result = module.require_uv()

        self.assertEqual(result, uv_bin.resolve())

    def test_windows_expose_uv_keeps_existing_source_when_target_is_same(self):
        module = load_local_setup_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            venv_dir = Path(temp_dir) / "venv"
            uv_bin = venv_dir / "Scripts" / "uv.exe"
            uv_bin.parent.mkdir(parents=True)
            uv_bin.write_bytes(b"uv-binary")

            with patch.object(module.os, "name", "nt"):
                result = module.expose_uv_to_venv(uv_bin, venv_dir)

            self.assertEqual(result, uv_bin)
            self.assertEqual(uv_bin.read_bytes(), b"uv-binary")

    def test_recreate_preserves_uv_located_inside_old_venv(self):
        module = load_local_setup_module()
        commands = []

        with tempfile.TemporaryDirectory() as temp_dir:
            venv_dir = (Path(temp_dir) / "venv").resolve()
            uv_bin = venv_dir / "bin" / "uv"
            uv_bin.parent.mkdir(parents=True)
            uv_bin.write_bytes(b"uv-binary")

            def fake_run(command, **_kwargs):
                self.assertTrue(Path(command[0]).is_file())
                commands.append(command)

            with patch.object(module, "ensure_supported_python"), \
                    patch.object(module, "require_uv", return_value=uv_bin), \
                    patch.object(module, "install_browser_runtime"), \
                    patch.object(module, "run", side_effect=fake_run):
                module.install_deps(
                    python_bin="python.exe",
                    venv_dir=venv_dir,
                    recreate=True,
                )

            self.assertEqual(len(commands), 1)
            self.assertNotEqual(Path(commands[0][0]), uv_bin)
            self.assertNotIn("--inexact", commands[0])
            self.assertEqual(uv_bin.read_bytes(), b"uv-binary")

    def test_recreate_rejects_python_from_target_venv(self):
        module = load_local_setup_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            venv_dir = (Path(temp_dir) / "venv").resolve()
            python_bin = venv_dir / "bin" / "python"
            python_bin.parent.mkdir(parents=True)
            python_bin.touch()

            with patch.object(module, "ensure_supported_python"), \
                    self.assertRaisesRegex(RuntimeError, "venv 外部"):
                module.install_deps(
                    python_bin=str(python_bin),
                    venv_dir=venv_dir,
                    recreate=True,
                )

    def test_recreate_rejects_current_python_inside_target_venv(self):
        module = load_local_setup_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            venv_dir = (Path(temp_dir) / "venv").resolve()
            running_python = venv_dir / "bin" / "python"
            running_python.parent.mkdir(parents=True)
            running_python.touch()

            with patch.object(module, "ensure_supported_python"), patch.object(
                module.sys, "executable", str(running_python)
            ), self.assertRaisesRegex(RuntimeError, "venv 外部"):
                module.install_deps(
                    python_bin="/usr/bin/python3",
                    venv_dir=venv_dir,
                    recreate=True,
                )

    def test_recreate_resolves_python_command_through_path(self):
        module = load_local_setup_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            venv_dir = (Path(temp_dir) / "venv").resolve()
            path_python = venv_dir / "bin" / "python"
            path_python.parent.mkdir(parents=True)
            path_python.touch()

            with patch.object(module, "ensure_supported_python"), patch.object(
                module.shutil, "which", return_value=str(path_python)
            ), self.assertRaisesRegex(RuntimeError, "venv 外部"):
                module.install_deps(
                    python_bin="python3",
                    venv_dir=venv_dir,
                    recreate=True,
                )

    def test_windows_install_deps_uses_uv_without_pip_bootstrap(self):
        module = load_local_setup_module()
        calls = []

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
                module.os.environ,
                {
                    "PROXY_HOST": "http://proxy.example:7890",
                    "PIP_PROXY": "https://user:pass@mirror.example/simple",
                    "PACKAGE_CACHE_ROOT": str(Path(temp_dir) / "custom-package-cache"),
                },
                clear=True,
        ):
            root = Path(temp_dir)
            venv_dir = root / "venv"
            venv_python = venv_dir / "Scripts" / "python.exe"
            uv_bin = root / "tools" / "uv.exe"
            module.CONFIG_DIR = root / "config"

            def fake_run(command, cwd=None, env=None, safe_command=None):
                calls.append((command, env, safe_command))

            with patch.object(module.os, "name", "nt"), \
                    patch.object(module, "ensure_supported_python"), \
                    patch.object(module, "require_uv", return_value=uv_bin), \
                    patch.object(module, "expose_uv_to_venv"), \
                    patch.object(module, "install_browser_runtime"), \
                    patch.object(module, "run", side_effect=fake_run):
                module.install_deps(python_bin="python", venv_dir=venv_dir, recreate=False)

        self.assertEqual(len(calls), 1)
        command, env, safe_command = calls[0]
        self.assertEqual(command[:2], [str(uv_bin), "sync"])
        self.assertNotIn("pip", command)
        self.assertEqual(env["UV_PROJECT_ENVIRONMENT"], str(venv_dir.resolve()))
        self.assertEqual(env["UV_DEFAULT_INDEX"], "https://user:pass@mirror.example/simple")
        self.assertEqual(env["HTTPS_PROXY"], "http://proxy.example:7890")
        self.assertEqual(env["PACKAGE_CACHE_ROOT"], str(Path(temp_dir) / "custom-package-cache"))
        self.assertEqual(env["UV_CACHE_DIR"], str(Path(temp_dir) / "custom-package-cache" / "uv"))
        self.assertNotIn("user:pass", " ".join(safe_command or command))

    def test_install_deps_uses_package_env_for_project_lock(self):
        module = load_local_setup_module()
        calls = []

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
                module.os.environ,
                {"PIP_PROXY": "https://user:pass@mirror.example/simple"},
                clear=False,
        ):
            root = Path(temp_dir)
            venv_dir = root / "venv"
            uv_bin = root / "tools" / "uv"
            module.CONFIG_DIR = root / "config"

            def fake_run(command, cwd=None, env=None, safe_command=None):
                calls.append((command, env, safe_command))

            with patch.object(module, "ensure_supported_python"), \
                    patch.object(module, "require_uv", return_value=uv_bin), \
                    patch.object(module, "expose_uv_to_venv"), \
                    patch.object(module, "install_browser_runtime"), \
                    patch.object(module, "run", side_effect=fake_run):
                module.install_deps(python_bin="python3", venv_dir=venv_dir, recreate=False)

        project_sync = calls[0]
        self.assertEqual(project_sync[0][:2], [str(uv_bin), "sync"])
        self.assertIn("--locked", project_sync[0])
        self.assertEqual(project_sync[1]["UV_DEFAULT_INDEX"], "https://user:pass@mirror.example/simple")
        self.assertNotIn("user:pass", " ".join(project_sync[2] or project_sync[0]))
