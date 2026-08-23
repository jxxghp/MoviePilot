"""渐进式 mypy 门禁配置与可执行性测试。"""

import configparser
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MYPY_CONFIG = PROJECT_ROOT / "mypy.ini"


def test_mypy_gate_has_explicit_strict_scope_without_global_ignore() -> None:
    """门禁必须列出治理文件，且不能用 app 级 ignore_errors 消音。"""
    parser = configparser.ConfigParser()
    parser.read(MYPY_CONFIG, encoding="utf-8")
    settings = parser["mypy"]
    governed_files = {
        item.strip().rstrip(",")
        for item in settings["files"].splitlines()
        if item.strip()
    }

    assert settings.getboolean("strict") is True
    assert "app/runtime/event/contracts.py" in governed_files
    assert "app/runtime/extensions/module/contracts.py" in governed_files
    assert "app/runtime/extensions/module/dispatcher.py" in governed_files
    assert "app/runtime/extensions/module/quality.py" in governed_files
    assert "app/runtime/event/errors.py" in governed_files
    assert "app/application/scheduling.py" in governed_files
    assert "scripts/architecture/async_blocking.py" in governed_files
    assert "app/startup/composition/context.py" in governed_files
    assert "app/startup/composition/configuration.py" in governed_files
    assert "app/db/adapters/download.py" in governed_files
    assert "app/db/adapters/workflow.py" in governed_files
    assert "app/application/workflow.py" in governed_files
    assert "app/api/context.py" in governed_files
    assert "app/db/base.py" in governed_files
    assert "app/db/uow.py" in governed_files
    assert len(governed_files) >= 37
    assert any(path.startswith("app/domain/") for path in governed_files)
    assert "ignore_errors" not in MYPY_CONFIG.read_text(encoding="utf-8")


def test_mypy_governed_scope_passes() -> None:
    """使用锁定到开发依赖的 mypy 执行当前严格文件清单。"""
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "--config-file", "mypy.ini"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
