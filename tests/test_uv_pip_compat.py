from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "uv-pip-compat.sh"


class UvPipCompatTests(unittest.TestCase):
    def run_wrapper(self, link_name: str, *args: str) -> list[str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            venv_bin = Path(temp_dir) / "venv" / "bin"
            venv_bin.mkdir(parents=True)
            (venv_bin / "python").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (venv_bin / "python").chmod(0o755)

            argv_file = Path(temp_dir) / "argv.txt"
            uv_bin = venv_bin / "uv"
            uv_bin.write_text(
                "#!/bin/sh\n"
                # 测试只关心兼容层传给 uv 的参数，逐行记录可以避免 shell 转义差异干扰断言。
                f"for arg in \"$@\"; do printf '%s\\n' \"$arg\" >> '{argv_file}'; done\n",
                encoding="utf-8",
            )
            uv_bin.chmod(0o755)

            wrapper_path = venv_bin / "uv-pip-compat"
            shutil.copy2(WRAPPER, wrapper_path)
            wrapper_path.chmod(0o755)

            link_path = venv_bin / link_name
            link_path.symlink_to(wrapper_path.name)

            subprocess.run(
                [str(link_path), *args],
                check=True,
                env={
                    **os.environ,
                    "PATH": f"{venv_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                },
            )
            return argv_file.read_text(encoding="utf-8").splitlines()

    def test_pip_install_binds_venv_python(self):
        argv = self.run_wrapper("pip", "install", "-r", "requirements.txt")

        self.assertEqual(
            [
                "pip",
                "install",
                "--python",
                argv[3],
                "-r",
                "requirements.txt",
            ],
            argv,
        )
        self.assertTrue(argv[3].endswith("/venv/bin/python"))

    def test_pip_install_keeps_explicit_environment(self):
        argv = self.run_wrapper("pip", "install", "--system", "demo-package")

        self.assertEqual(["pip", "install", "--system", "demo-package"], argv)

    def test_pip_sync_binds_venv_python(self):
        argv = self.run_wrapper("pip-sync", "requirements.txt")

        self.assertEqual(["pip", "sync", "--python", argv[3], "requirements.txt"], argv)
        self.assertTrue(argv[3].endswith("/venv/bin/python"))
