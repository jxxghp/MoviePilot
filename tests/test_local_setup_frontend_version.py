from __future__ import annotations

import importlib.util
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "local_setup.py"


def load_local_setup_module():
    module_name = f"moviepilot_local_setup_frontend_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class LocalSetupFrontendVersionTests(unittest.TestCase):
    def test_repo_frontend_version_reads_version_file(self):
        module = load_local_setup_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "version.py").write_text(
                "APP_VERSION = 'v0.0.1'\nFRONTEND_VERSION = 'v9.9.9'\n",
                encoding="utf-8",
            )

            with patch.object(module, "ROOT", root):
                self.assertEqual(module._repo_frontend_version(), "v9.9.9")

    def test_resolve_frontend_release_uses_repo_frontend_version_by_default(self):
        module = load_local_setup_module()
        release = {
            "tag_name": "v9.9.9",
            "assets": [
                {
                    "name": "dist.zip",
                    "browser_download_url": "https://example.com/dist.zip",
                }
            ],
        }

        with patch.object(module, "_repo_frontend_version", return_value="v9.9.9"), patch.object(
            module, "fetch_json", return_value=release
        ) as fetch_mock:
            tag_name, download_url = module._resolve_frontend_release(None)

        fetch_mock.assert_called_once_with(
            module.FRONTEND_TAG_API.format(tag="v9.9.9")
        )
        self.assertEqual(tag_name, "v9.9.9")
        self.assertEqual(download_url, "https://example.com/dist.zip")

    def test_parser_leaves_frontend_version_empty_until_runtime_resolution(self):
        module = load_local_setup_module()
        parser = module.build_parser()

        install_args = parser.parse_args(["install-frontend"])
        setup_args = parser.parse_args(["setup"])
        update_args = parser.parse_args(["update", "frontend"])

        self.assertIsNone(install_args.version)
        self.assertIsNone(setup_args.frontend_version)
        self.assertIsNone(update_args.frontend_version)
