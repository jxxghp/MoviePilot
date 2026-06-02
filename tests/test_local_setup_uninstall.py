from __future__ import annotations

import importlib.util
import tempfile
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "local_setup.py"


def load_local_setup_module():
    module_name = f"moviepilot_local_setup_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class LocalSetupUninstallTests(unittest.TestCase):
    def prepare_install_tree(self, *, legacy_config: bool = False):
        module = load_local_setup_module()
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)

        temp_path = Path(temp_dir.name)
        root_dir = temp_path / "MoviePilot"
        helper_dir = root_dir / "app" / "helper"
        runtime_dir = root_dir / ".runtime"
        public_dir = root_dir / "public"
        venv_dir = root_dir / "venv"
        install_env_file = root_dir / ".moviepilot.env"
        config_dir = root_dir / "config" if legacy_config else temp_path / "moviepilot-config"
        temp_config_dir = config_dir / "temp"

        helper_dir.mkdir(parents=True)
        runtime_dir.mkdir(parents=True)
        public_dir.mkdir(parents=True)
        venv_dir.mkdir(parents=True)
        temp_config_dir.mkdir(parents=True)
        install_env_file.write_text("CONFIG_DIR=/tmp/moviepilot-config\n", encoding="utf-8")
        (root_dir / "moviepilot").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        (helper_dir / "sites.py").write_text("generated\n", encoding="utf-8")
        (helper_dir / "user.sites.v2.bin").write_bytes(b"binary")
        (temp_config_dir / "moviepilot.runtime.json").write_text("{}", encoding="utf-8")
        (temp_config_dir / "moviepilot.frontend.runtime.json").write_text(
            "{}", encoding="utf-8"
        )

        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(module, "ROOT", root_dir))
        stack.enter_context(patch.object(module, "HELPER_DIR", helper_dir))
        stack.enter_context(patch.object(module, "RUNTIME_DIR", runtime_dir))
        stack.enter_context(patch.object(module, "PUBLIC_DIR", public_dir))
        stack.enter_context(patch.object(module, "INSTALL_ENV_FILE", install_env_file))
        stack.enter_context(patch.object(module, "LEGACY_CONFIG_DIR", root_dir / "config"))
        stack.enter_context(patch.object(module, "CONFIG_DIR", config_dir))
        stack.enter_context(patch.object(module, "TEMP_DIR", temp_config_dir))

        return module, root_dir, config_dir, venv_dir, install_env_file

    def test_remove_config_data_deletes_legacy_config_directory(self):
        module, _, config_dir, _, _ = self.prepare_install_tree(legacy_config=True)
        category_file = config_dir / "category.yaml"
        category_file.write_text("seed\n", encoding="utf-8")
        (config_dir / "logs").mkdir(exist_ok=True)
        (config_dir / "user.db").write_text("db\n", encoding="utf-8")

        removed = module._remove_config_data(config_dir)

        self.assertFalse(config_dir.exists())
        self.assertFalse(category_file.exists())
        self.assertIn(
            str(config_dir.resolve()),
            {str(path.resolve()) for path in removed},
        )

    def test_uninstall_keeps_config_by_default(self):
        module, root_dir, config_dir, venv_dir, install_env_file = self.prepare_install_tree()
        cli_dir = root_dir.parent / "bin"
        cli_dir.mkdir()
        cli_link = cli_dir / "moviepilot"
        cli_link.symlink_to(root_dir / "moviepilot")

        yes_no_answers = iter([False, True])
        with patch.object(module, "_is_interactive", return_value=True), patch.object(
            module,
            "_prompt_yes_no",
            side_effect=lambda label, default=False: next(yes_no_answers),
        ), patch.object(
            module,
            "_prompt_text",
            side_effect=lambda label, default=None, allow_empty=False, secret=False: module.UNINSTALL_CONFIRM_TEXT,
        ), patch.object(module, "_stop_managed_services", return_value=None):
            result = module.uninstall_local(
                venv_dir=venv_dir,
                config_dir=config_dir,
                launch_path=str(cli_link),
            )

        self.assertFalse(result["cancelled"])
        self.assertTrue(config_dir.exists())
        self.assertTrue(install_env_file.exists())
        self.assertFalse(venv_dir.exists())
        self.assertFalse((root_dir / ".runtime").exists())
        self.assertFalse((root_dir / "public").exists())
        self.assertFalse((root_dir / "app" / "helper" / "sites.py").exists())
        self.assertFalse((root_dir / "app" / "helper" / "user.sites.v2.bin").exists())
        self.assertFalse(cli_link.exists())

    def test_uninstall_deletes_external_config_when_requested(self):
        module, _, config_dir, venv_dir, install_env_file = self.prepare_install_tree()
        yes_no_answers = iter([True, True])

        with patch.object(module, "_is_interactive", return_value=True), patch.object(
            module,
            "_prompt_yes_no",
            side_effect=lambda label, default=False: next(yes_no_answers),
        ), patch.object(
            module,
            "_prompt_text",
            side_effect=lambda label, default=None, allow_empty=False, secret=False: module.UNINSTALL_CONFIRM_TEXT,
        ), patch.object(module, "_stop_managed_services", return_value=None):
            result = module.uninstall_local(
                venv_dir=venv_dir,
                config_dir=config_dir,
            )

        self.assertFalse(result["cancelled"])
        self.assertTrue(result["config_deleted"])
        self.assertFalse(config_dir.exists())
        self.assertFalse(install_env_file.exists())

    def test_uninstall_deletes_legacy_config_when_requested(self):
        module, _, config_dir, venv_dir, install_env_file = self.prepare_install_tree(
            legacy_config=True
        )
        (config_dir / "category.yaml").write_text("seed\n", encoding="utf-8")
        yes_no_answers = iter([True, True])

        with patch.object(module, "_is_interactive", return_value=True), patch.object(
            module,
            "_prompt_yes_no",
            side_effect=lambda label, default=False: next(yes_no_answers),
        ), patch.object(
            module,
            "_prompt_text",
            side_effect=lambda label, default=None, allow_empty=False, secret=False: module.UNINSTALL_CONFIRM_TEXT,
        ), patch.object(module, "_stop_managed_services", return_value=None):
            result = module.uninstall_local(
                venv_dir=venv_dir,
                config_dir=config_dir,
            )

        self.assertFalse(result["cancelled"])
        self.assertTrue(result["config_deleted"])
        self.assertFalse(config_dir.exists())
        self.assertFalse(install_env_file.exists())
