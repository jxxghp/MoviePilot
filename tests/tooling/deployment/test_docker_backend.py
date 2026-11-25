"""
验证容器后端在应用导入前启用原生崩溃诊断。
"""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("start_nogosu", ["true", "false"])
def test_backend_enables_fault_handler_before_application_import(
    tmp_path: Path, start_nogosu: str
) -> None:
    """
    两种权限启动分支都在应用入口执行前启用诊断，且不改变应用参数。
    """
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "main.py").write_text(
        "import faulthandler, json, sys\n"
        "print(json.dumps({'enabled': faulthandler.is_enabled(), 'argv': sys.argv}))\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python3").symlink_to(sys.executable)
    gosu_log = tmp_path / "gosu.log"
    gosu = bin_dir / "gosu"
    gosu.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$1" > "$GOSU_LOG"\nshift\nexec "$@"\n',
        encoding="utf-8",
    )
    gosu.chmod(0o755)
    script = (ROOT / "docker" / "backend.sh").read_text(encoding="utf-8")
    # 只将容器工作目录映射到临时目录，保留真实解释器调用和权限分支。
    script = script.replace("cd /app\n", f"cd {shlex.quote(str(tmp_path))}\n", 1)
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "START_NOGOSU": start_nogosu,
        "VENV_PATH": str(bin_dir.parent),
        "GOSU_LOG": str(gosu_log),
    }
    environment.pop("PYTHONFAULTHANDLER", None)

    result = subprocess.run(
        ["bash", "-c", script],
        env=environment,
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    )

    assert json.loads(result.stdout) == {"enabled": True, "argv": ["app/main.py"]}
    assert result.stderr == ""
    if start_nogosu == "false":
        assert gosu_log.read_text(encoding="utf-8").strip() == "moviepilot:moviepilot"
    else:
        assert not gosu_log.exists()
