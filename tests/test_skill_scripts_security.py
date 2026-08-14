import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MP_API_SCRIPT = PROJECT_ROOT / "skills" / "moviepilot-api" / "scripts" / "mp-api.py"
MP_DB_SCRIPT = PROJECT_ROOT / "skills" / "database-operation" / "scripts" / "mp-db.py"


def _load_script(path: Path, module_name: str) -> ModuleType:
    """按文件路径加载 skill 脚本模块。"""
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_mp_api_uses_settings_without_prompt_token(monkeypatch, tmp_path) -> None:
    """API 脚本应直接读取 settings，而不是要求提示词提供 token。"""
    module = _load_script(MP_API_SCRIPT, "mp_api_script")
    runtime_dir = tmp_path / "temp"
    runtime_dir.mkdir()

    class FakeSettings:
        """提供 API 脚本本地配置所需字段。"""

        TEMP_PATH = runtime_dir
        HOST = "0.0.0.0"
        PORT = 3001
        API_TOKEN = "settings-token"

    monkeypatch.setattr(module, "_ensure_project_import", lambda: None)
    monkeypatch.setattr(module, "read_config", lambda: ("http://file-host", "file-token"))
    monkeypatch.setattr(
        "app.runtime.config.settings",
        FakeSettings,
        raising=False,
    )
    monkeypatch.delenv("MP_HOST", raising=False)
    monkeypatch.delenv("MP_API_KEY", raising=False)

    host, key = module.resolve_config()

    assert host == "http://127.0.0.1:3001"
    assert key == "settings-token"


def test_mp_db_rejects_write_statement_without_write_flag() -> None:
    """数据库脚本默认必须拒绝写操作。"""
    module = _load_script(MP_DB_SCRIPT, "mp_db_script")

    assert module._is_supported_statement("SELECT COUNT(*) FROM downloadhistory", False)
    assert not module._is_supported_statement("UPDATE subscribe SET state='S' WHERE id=1", False)
    assert not module._is_supported_statement(
        "SELECT COUNT(*) FROM downloadhistory; DELETE FROM downloadhistory WHERE id=1",
        False,
    )


def test_mp_db_returns_sensitive_columns_without_masking(monkeypatch, capsys) -> None:
    """数据库脚本应原样返回敏感字段供 Agent 内部使用。"""
    module = _load_script(MP_DB_SCRIPT, "mp_db_script_unmasked")

    class FakeRow:
        """模拟 SQLAlchemy 查询行。"""

        _mapping = {"token": "raw-token", "password": "raw-password"}

    class FakeResult:
        """模拟返回查询结果的 SQLAlchemy Result。"""

        returns_rows = True

        def fetchall(self) -> list[FakeRow]:
            """返回测试查询行。"""
            return [FakeRow()]

    class FakeConnection:
        """模拟数据库连接。"""

        def execute(self, statement: Any) -> FakeResult:
            """返回测试结果。"""
            return FakeResult()

    class FakeTransaction:
        """模拟 engine.begin() 上下文。"""

        def __enter__(self) -> FakeConnection:
            """进入上下文时返回连接。"""
            return FakeConnection()

        def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
            """退出上下文。"""
            return False

    class FakeEngine:
        """模拟数据库引擎。"""

        def begin(self) -> FakeTransaction:
            """返回事务上下文。"""
            return FakeTransaction()

    monkeypatch.setattr(module, "_build_engine", lambda: FakeEngine())

    assert module.run_query("SELECT token, password FROM site LIMIT 1") == 0

    output = json.loads(capsys.readouterr().out)
    assert output["rows"] == [{"token": "raw-token", "password": "raw-password"}]


def test_mp_db_write_command_allows_write_statement(monkeypatch) -> None:
    """数据库脚本 write 子命令应直接允许写操作。"""
    module = _load_script(MP_DB_SCRIPT, "mp_db_script_write")
    calls = []

    def fake_run_query(sql: str, *, limit: int = 100, allow_write: bool = False) -> int:
        """记录 write 子命令传入的执行参数。"""
        calls.append({"sql": sql, "limit": limit, "allow_write": allow_write})
        return 0

    monkeypatch.setattr(module, "run_query", fake_run_query)
    monkeypatch.setattr(
        module.sys,
        "argv",
        [
            "mp-db.py",
            "write",
            "UPDATE subscribe SET state = 'S' WHERE id = 123",
        ],
    )

    assert module.main() == 0
    assert calls == [
        {
            "sql": "UPDATE subscribe SET state = 'S' WHERE id = 123",
            "limit": 0,
            "allow_write": True,
        }
    ]
