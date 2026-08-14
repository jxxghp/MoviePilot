import importlib.util
from pathlib import Path
import sys
import uuid

import pytest

from app.startup import database_initializer as db_init


LOCAL_SETUP_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "local_setup.py"
)


def _load_local_setup_module():
    """加载隔离的本地安装脚本实例，避免测试间共享模块状态。"""
    module_name = f"moviepilot_local_setup_migration_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, LOCAL_SETUP_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_update_db_preserves_migration_error_and_traceback(monkeypatch) -> None:
    """迁移失败日志应保留堆栈，同时向调用方传播原始异常。"""
    migration_error = RuntimeError("migration failed")
    logged_errors: list[str] = []

    def fail_upgrade(*_args, **_kwargs) -> None:
        raise migration_error

    monkeypatch.setattr(db_init, "upgrade", fail_upgrade)
    monkeypatch.setattr(db_init.logger, "error", logged_errors.append)

    with pytest.raises(RuntimeError) as raised:
        db_init.update_db()

    assert raised.value is migration_error
    assert len(logged_errors) == 1
    assert "数据库更新失败：migration failed" in logged_errors[0]
    assert "RuntimeError: migration failed" in logged_errors[0]


def test_local_setup_returns_failure_when_database_migration_fails(
        monkeypatch,
        capsys,
) -> None:
    """本地维护命令不得在迁移失败后继续访问业务表。"""
    module = _load_local_setup_module()
    migration_error = RuntimeError("migration failed")

    def fail_sync() -> None:
        raise migration_error

    monkeypatch.setattr(sys, "argv", [str(LOCAL_SETUP_PATH), "sync-superuser"])
    monkeypatch.setattr(module, "_resolve_interactive_config_dir", lambda *_: None)
    monkeypatch.setattr(module, "configure_config_dir", lambda **_: Path("config"))
    monkeypatch.setattr(module, "_sync_superuser_account_inner", fail_sync)

    assert module.main() == 1
    assert "migration failed" in capsys.readouterr().err
