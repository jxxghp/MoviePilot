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

    def test_install_deps_installs_browser_runtime(self):
        module = load_local_setup_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            venv_dir = (Path(temp_dir) / "venv").resolve()
            venv_python = venv_dir / "bin" / "python"
            venv_pip = venv_dir / "bin" / "pip"

            with patch.object(module, "ensure_supported_python"), \
                    patch.object(
                        module,
                        "configure_venv_pip_compat",
                        return_value=venv_pip,
                    ), \
                    patch.object(module, "run") as run_mock, \
                    patch.object(module, "install_browser_runtime") as install_browser:
                result = module.install_deps(
                    python_bin="python3",
                    venv_dir=venv_dir,
                    recreate=False,
                )

        self.assertEqual(result, venv_python)
        run_mock.assert_any_call(["python3", "-m", "venv", str(venv_dir)])
        self.assertTrue(
            any(
                call.args[0] == [str(venv_pip), "install", "-r", str(module.ROOT / "requirements.txt")]
                for call in run_mock.call_args_list
            )
        )
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
                clear=False,
        ):
            module.CONFIG_DIR = Path(temp_dir)
            env = module.build_package_install_env()

        self.assertEqual(env["HTTPS_PROXY"], "http://proxy.example:7890")
        self.assertEqual(env["PACKAGE_CACHE_ROOT"], str(Path(temp_dir) / "custom-package-cache"))
        self.assertEqual(env["PIP_CACHE_DIR"], str(Path(temp_dir) / "custom-package-cache" / "pip"))
        self.assertEqual(env["UV_CACHE_DIR"], str(Path(temp_dir) / "custom-package-cache" / "uv"))
        self.assertEqual(env["PIP_INDEX_URL"], "https://user:pass@mirror.example/simple")
        self.assertEqual(env["UV_DEFAULT_INDEX"], "https://user:pass@mirror.example/simple")

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
        self.assertEqual(env["PIP_CACHE_DIR"], str(Path(temp_dir) / ".cache" / "pip"))
        self.assertEqual(env["UV_CACHE_DIR"], str(Path(temp_dir) / ".cache" / "uv"))

    def test_package_install_env_preserves_explicit_cache_dirs(self):
        module = load_local_setup_module()

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
                module.os.environ,
                {
                    "PIP_CACHE_DIR": "/custom/pip-cache",
                    "UV_CACHE_DIR": "/custom/uv-cache",
                    "PACKAGE_CACHE_ROOT": "/custom/custom-package-cache",
                },
                clear=False,
        ):
            module.CONFIG_DIR = Path(temp_dir)
            env = module.build_package_install_env()

        self.assertEqual(env["PACKAGE_CACHE_ROOT"], "/custom/custom-package-cache")
        self.assertEqual(env["PIP_CACHE_DIR"], "/custom/pip-cache")
        self.assertEqual(env["UV_CACHE_DIR"], "/custom/uv-cache")

    def test_run_redacts_safe_command(self):
        module = load_local_setup_module()

        with patch.object(module.subprocess, "run"), patch("builtins.print") as print_mock:
            module.run(
                [
                    "python",
                    "-m",
                    "pip",
                    "install",
                    "-i",
                    "https://user:pass@mirror.example/simple",
                ],
                safe_command=[
                    "python",
                    "-m",
                    "pip",
                    "install",
                    "-i",
                    "https://mirror.example/simple",
                ],
            )

        printed = " ".join(str(call.args[0]) for call in print_mock.call_args_list)
        self.assertIn("https://mirror.example/simple", printed)
        self.assertNotIn("user:pass", printed)

    def test_redact_command_handles_inline_index_url(self):
        module = load_local_setup_module()

        command = [
            "pip",
            "install",
            "--index-url=https://user:pass@mirror.example/simple",
        ]

        redacted = module.redact_command(command)

        self.assertIn("--index-url=https://mirror.example/simple", redacted)
        self.assertNotIn("user:pass", " ".join(redacted))

    def test_redact_command_handles_url_query_equals(self):
        module = load_local_setup_module()

        command = [
            "pip",
            "install",
            "https://user:pass@mirror.example/simple?token=abc",
        ]

        redacted = module.redact_command(command)

        self.assertIn("https://mirror.example/simple?token=abc", redacted)
        self.assertNotIn("user:pass", " ".join(redacted))

    def test_uv_bootstrap_uses_package_env_and_index_without_visible_secret(self):
        module = load_local_setup_module()
        calls = []

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
                module.os.environ,
                {
                    "PROXY_HOST": "http://proxy.example:7890",
                    "PIP_PROXY": "https://user:pass@mirror.example/simple",
                    "PACKAGE_CACHE_ROOT": str(Path(temp_dir) / "custom-package-cache"),
                },
                clear=False,
        ):
            venv_dir = Path(temp_dir) / "venv"
            venv_python = venv_dir / "bin" / "python"
            uv_bin = venv_dir / "bin" / "uv"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("", encoding="utf-8")
            module.CONFIG_DIR = Path(temp_dir) / "config"

            def fake_run(command, cwd=None, env=None, safe_command=None):
                calls.append((command, env, safe_command))
                uv_bin.write_text("", encoding="utf-8")

            with patch.object(module.shutil, "which", return_value=None), \
                    patch.object(module, "run", side_effect=fake_run):
                module._ensure_uv_available_for_venv(venv_dir, venv_python)

        command, env, safe_command = calls[0]
        self.assertEqual(command, [str(venv_python), "-m", "pip", "install", "--upgrade", "pip", "uv"])
        self.assertEqual(env["PIP_INDEX_URL"], "https://user:pass@mirror.example/simple")
        self.assertEqual(env["UV_DEFAULT_INDEX"], "https://user:pass@mirror.example/simple")
        self.assertEqual(env["HTTPS_PROXY"], "http://proxy.example:7890")
        self.assertEqual(env["PACKAGE_CACHE_ROOT"], str(Path(temp_dir) / "custom-package-cache"))
        self.assertEqual(env["PIP_CACHE_DIR"], str(Path(temp_dir) / "custom-package-cache" / "pip"))
        self.assertEqual(env["UV_CACHE_DIR"], str(Path(temp_dir) / "custom-package-cache" / "uv"))
        self.assertNotIn("user:pass", " ".join(safe_command or command))

    def test_windows_pip_upgrade_uses_package_env(self):
        module = load_local_setup_module()
        calls = []

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
                module.os.environ,
                {
                    "PROXY_HOST": "http://proxy.example:7890",
                    "PIP_PROXY": "https://user:pass@mirror.example/simple",
                    "PACKAGE_CACHE_ROOT": str(Path(temp_dir) / "custom-package-cache"),
                },
                clear=False,
        ):
            root = Path(temp_dir)
            venv_dir = root / "venv"
            venv_python = venv_dir / "Scripts" / "python.exe"
            venv_pip = venv_dir / "Scripts" / "pip.exe"
            venv_pip.parent.mkdir(parents=True)
            venv_python.write_text("", encoding="utf-8")
            venv_pip.write_text("", encoding="utf-8")
            module.CONFIG_DIR = root / "config"

            def fake_run(command, cwd=None, env=None, safe_command=None):
                calls.append((command, env, safe_command))

            with patch.object(module.os, "name", "nt"), \
                    patch.object(module, "ensure_supported_python"), \
                    patch.object(module, "install_browser_runtime"), \
                    patch.object(module, "run", side_effect=fake_run):
                module.install_deps(python_bin="python", venv_dir=venv_dir, recreate=False)

        pip_upgrade = [
            item for item in calls
            if item[0][1:] == ["-m", "pip", "install", "--upgrade", "pip"]
        ][0]
        self.assertEqual(pip_upgrade[1]["PIP_INDEX_URL"], "https://user:pass@mirror.example/simple")
        self.assertEqual(pip_upgrade[1]["UV_DEFAULT_INDEX"], "https://user:pass@mirror.example/simple")
        self.assertEqual(pip_upgrade[1]["HTTPS_PROXY"], "http://proxy.example:7890")
        self.assertEqual(pip_upgrade[1]["PACKAGE_CACHE_ROOT"], str(Path(temp_dir) / "custom-package-cache"))
        self.assertEqual(pip_upgrade[1]["PIP_CACHE_DIR"], str(Path(temp_dir) / "custom-package-cache" / "pip"))
        self.assertEqual(pip_upgrade[1]["UV_CACHE_DIR"], str(Path(temp_dir) / "custom-package-cache" / "uv"))
        self.assertNotIn("user:pass", " ".join(pip_upgrade[2] or pip_upgrade[0]))

    def test_install_deps_uses_package_env_for_project_requirements(self):
        module = load_local_setup_module()
        calls = []

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
                module.os.environ,
                {"PIP_PROXY": "https://user:pass@mirror.example/simple"},
                clear=False,
        ):
            root = Path(temp_dir)
            venv_dir = root / "venv"
            venv_python = venv_dir / "bin" / "python"
            venv_pip = venv_dir / "bin" / "pip"
            venv_pip.parent.mkdir(parents=True)
            venv_python.write_text("", encoding="utf-8")
            venv_pip.write_text("", encoding="utf-8")
            module.CONFIG_DIR = root / "config"

            def fake_run(command, cwd=None, env=None, safe_command=None):
                calls.append((command, env, safe_command))

            with patch.object(module, "ensure_supported_python"), \
                    patch.object(module, "configure_venv_pip_compat", return_value=venv_pip), \
                    patch.object(module, "install_browser_runtime"), \
                    patch.object(module, "run", side_effect=fake_run):
                module.install_deps(python_bin="python3", venv_dir=venv_dir, recreate=False)

        project_install = [
            item for item in calls
            if item[0][:2] == [str(venv_pip), "install"] and "-r" in item[0]
        ][0]
        self.assertEqual(project_install[1]["PIP_INDEX_URL"], "https://user:pass@mirror.example/simple")
        self.assertEqual(project_install[1]["UV_DEFAULT_INDEX"], "https://user:pass@mirror.example/simple")
        self.assertNotIn("user:pass", " ".join(project_install[2] or project_install[0]))
