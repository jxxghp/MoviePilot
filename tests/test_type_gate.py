"""
全量宿主 mypy 门禁配置与可执行性测试。
"""

import configparser
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MYPY_CONFIG = PROJECT_ROOT / "mypy.ini"


def test_mypy_gate_has_explicit_strict_scope_without_global_ignore() -> None:
    """
    门禁默认覆盖全部宿主和治理脚本，且不能用 app 级 ignore_errors 消音。
    """
    parser = configparser.ConfigParser()
    parser.read(MYPY_CONFIG, encoding="utf-8")
    settings = parser["mypy"]
    governed_files = {
        item.strip().rstrip(",")
        for item in settings["files"].splitlines()
        if item.strip()
    }

    assert settings.getboolean("strict") is True
    # app 目录递归治理，新文件无需逐个添加，保留既有脚本并检查新门禁自身。
    assert governed_files == {
        "app",
        "scripts/architecture/async_blocking.py",
        "scripts/architecture/mypy_policy.py",
    }
    assert settings.getboolean("ignore_missing_imports") is False
    assert settings["exclude"] == r"^app/plugins(?:/|$)"
    assert "ignore_errors" not in MYPY_CONFIG.read_text(encoding="utf-8")


def test_mypy_governed_scope_passes() -> None:
    """
    使用锁定到开发依赖的 mypy 执行全部宿主与治理脚本。
    """
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "--config-file", "mypy.ini"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
