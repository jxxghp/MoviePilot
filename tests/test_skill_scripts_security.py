"""
验证技能脚本的路径、凭据与外部调用安全边界。
"""

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MP_DB_SCRIPT = PROJECT_ROOT / "skills" / "database-operation" / "scripts" / "mp-db.py"


def _load_script(path: Path, module_name: str) -> ModuleType:
    """
    按文件路径加载 skill 脚本模块。
    """
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_mp_db_rejects_write_statement_without_write_flag() -> None:
    """
    数据库脚本默认必须拒绝写操作。
    """
    module = _load_script(MP_DB_SCRIPT, "mp_db_script")

    assert module._is_supported_statement("SELECT COUNT(*) FROM downloadhistory", False)
    assert not module._is_supported_statement("UPDATE subscribe SET state='S' WHERE id=1", False)
    assert not module._is_supported_statement(
        "SELECT COUNT(*) FROM downloadhistory; DELETE FROM downloadhistory WHERE id=1",
        False,
    )


def test_mp_db_returns_sensitive_columns_without_masking(monkeypatch, capsys) -> None:
    """
    数据库脚本应原样返回敏感字段供 Agent 内部使用。
    """
    module = _load_script(MP_DB_SCRIPT, "mp_db_script_unmasked")

    class FakeRow:
        """
        模拟 SQLAlchemy 查询行。
        """

        _mapping = {"token": "raw-token", "password": "raw-password"}

    class FakeResult:
        """
        模拟返回查询结果的 SQLAlchemy Result。
        """

        returns_rows = True

        def fetchall(self) -> list[FakeRow]:
            """
            返回测试查询行。
            """
            return [FakeRow()]

    class FakeConnection:
        """
        模拟数据库连接。
        """

        def execute(self, statement: Any) -> FakeResult:
            """
            返回测试结果。
            """
            return FakeResult()

    class FakeTransaction:
        """
        模拟 engine.begin() 上下文。
        """

        def __enter__(self) -> FakeConnection:
            """
            进入上下文时返回连接。
            """
            return FakeConnection()

        def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
            """
            退出上下文。
            """
            return False

    class FakeEngine:
        """
        模拟数据库引擎。
        """

        def begin(self) -> FakeTransaction:
            """
            返回事务上下文。
            """
            return FakeTransaction()

    monkeypatch.setattr(module, "_build_engine", lambda: FakeEngine())

    assert module.run_query("SELECT token, password FROM site LIMIT 1") == 0

    output = json.loads(capsys.readouterr().out)
    assert output["rows"] == [{"token": "raw-token", "password": "raw-password"}]


def test_mp_db_write_command_allows_write_statement(monkeypatch) -> None:
    """
    数据库脚本 write 子命令应直接允许写操作。
    """
    module = _load_script(MP_DB_SCRIPT, "mp_db_script_write")
    calls = []

    def fake_run_query(
        sql: str,
        *,
        limit: int = 100,
        allow_write: bool = False,
        show_secrets: bool = False,
    ) -> int:
        """
        记录 write 子命令传入的执行参数。
        """
        calls.append(
            {"sql": sql, "limit": limit, "allow_write": allow_write, "show_secrets": show_secrets}
        )
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
            "show_secrets": False,
        }
    ]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM systemconfig",
        'SELECT * FROM "systemconfig"',
        "SELECT * FROM public.systemconfig",
        "SELECT * FROM `systemconfig`",
        "SELECT * FROM [systemconfig]",
        "WITH leak AS (SELECT value FROM systemconfig) SELECT * FROM leak",
        "SELECT (SELECT value FROM systemconfig LIMIT 1) AS any_value",
    ],
)
def test_mp_db_blocks_configuration_tables_without_show_secrets(monkeypatch, sql) -> None:
    """
    引用凭据配置表的语句默认拒绝，引号、模式限定、子查询与写入模式都不能绕过。
    """
    module = _load_script(MP_DB_SCRIPT, "mp_db_script_secret_blocks")

    assert not module._is_supported_statement(sql, False)
    assert module._is_supported_statement(sql, False, show_secrets=True)
    assert not module._is_supported_statement(sql, True)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'systemconfig' AS name FROM subscribe",
        "-- systemconfig\nSELECT 1",
        "SELECT 1 /* systemconfig */",
        "SELECT name FROM subscribe WHERE name = 'systemconfig'",
        "SELECT * FROM userconfig",
        "SELECT plugin_id, key FROM plugindata",
    ],
)
def test_mp_db_configuration_gate_ignores_literals_and_unrelated_tables(
    monkeypatch,
    sql,
) -> None:
    """
    字面值与注释中的表名不触发凭据门禁；门禁只覆盖集中存放实例凭据的 systemconfig，
    其它表的普通读取保持原样。
    """
    module = _load_script(MP_DB_SCRIPT, "mp_db_script_secret_allows")

    assert module._is_supported_statement(sql, False)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT value FROM 'systemconfig'",
        "SELECT value FROM main.'systemconfig'",
        "SELECT value FROM ('systemconfig')",
        "SELECT secrets.value FROM subscribe, 'systemconfig' AS secrets",
        "WITH leak AS (SELECT value FROM 'systemconfig') SELECT * FROM leak",
        "SELECT value FROM config_view",
        "SELECT COUNT(*) FROM config_view",
        "DROP TABLE 'systemconfig'",
        "ALTER TABLE 'systemconfig' RENAME TO config_copy",
        "UPDATE 'systemconfig' SET value = 'changed'",
        "DELETE FROM 'systemconfig'",
        "INSERT INTO 'systemconfig' VALUES (2, 'OTHER_KEY', 'changed')",
    ],
)
def test_mp_db_sqlite_gate_checks_actual_configuration_access(tmp_path, monkeypatch, capsys, sql):
    """
    实际 SQLite 授权边界阻断单引号表名、连接、子查询、视图及写入绕过。
    """
    module = _load_script(MP_DB_SCRIPT, "mp_db_script_sqlite_secret_exec")
    engine = create_engine("sqlite:///" + str(tmp_path / "secret.db"))
    monkeypatch.setattr(module, "_build_engine", lambda: engine)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE systemconfig (id INTEGER, key TEXT, value TEXT)"))
            connection.execute(text("INSERT INTO systemconfig VALUES (1, 'TMDB_API_KEY', 'plain-secret')"))
            connection.execute(text("CREATE TABLE subscribe (id INTEGER)"))
            connection.execute(text("INSERT INTO subscribe VALUES (1)"))
            connection.execute(text("CREATE VIEW config_view AS SELECT value FROM systemconfig"))
        assert module.run_query(sql, allow_write=not sql.startswith(("SELECT", "WITH"))) == 1
        blocked = capsys.readouterr()
        assert "--show-secrets" in blocked.err
        assert "plain-secret" not in blocked.out
        with engine.begin() as connection:
            assert connection.execute(text("SELECT value FROM systemconfig")).scalars().all() == ["plain-secret"]
        assert module.run_query("SELECT value FROM 'systemconfig'", show_secrets=True) == 0
        assert json.loads(capsys.readouterr().out)["rows"] == [{"value": "plain-secret"}]
        assert module.run_query("SELECT 'systemconfig' AS name FROM subscribe") == 0
        assert json.loads(capsys.readouterr().out)["rows"] == [{"name": "systemconfig"}]
    finally:
        engine.dispose()


def test_mp_db_configuration_table_access_reports_and_requires_explicit_flag(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    """
    凭据配置表的读取与写入在未显式开启时被拒绝并给出可执行提示，开启后按原值返回。
    """
    module = _load_script(MP_DB_SCRIPT, "mp_db_script_secret_exec")
    engine = create_engine("sqlite:///" + str(tmp_path / "secret.db"))
    module.run_query.__globals__["_build_engine"] = lambda: engine
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE systemconfig (id INTEGER, key TEXT, value JSON)"))
            connection.execute(
                text("INSERT INTO systemconfig VALUES (1, 'TMDB_API_KEY', 'plain-secret')")
            )
        assert module.run_query("SELECT value FROM systemconfig") == 1
        assert "require --show-secrets" in capsys.readouterr().err
        assert module.run_query("UPDATE systemconfig SET value = 'x' WHERE id = 1", allow_write=True) == 1
        assert "require --show-secrets" in capsys.readouterr().err
        assert module.run_query("SELECT value FROM systemconfig", show_secrets=True) == 0
        assert json.loads(capsys.readouterr().out)["rows"] == [{"value": "plain-secret"}]
        assert (
            module.run_query(
                "UPDATE systemconfig SET value = 'fixed' WHERE id = 1",
                allow_write=True,
                show_secrets=True,
            )
            == 0
        )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("command", "argv", "expected"),
    [
        ("query", ["query", "SELECT 1", "--show-secrets"], True),
        ("query", ["query", "SELECT 1"], False),
        ("write", ["write", "UPDATE systemconfig SET value = 'x' WHERE id = 1", "--show-secrets"], True),
    ],
)
def test_mp_db_show_secrets_option_reaches_execution(monkeypatch, command, argv, expected) -> None:
    """
    两个子命令的 --show-secrets 选项都必须映射到执行参数，缺省保持关闭。
    """
    module = _load_script(MP_DB_SCRIPT, "mp_db_script_secret_option")
    calls = []

    def fake_run_query(
        sql: str,
        *,
        limit: int = 100,
        allow_write: bool = False,
        show_secrets: bool = False,
    ) -> int:
        """
        记录子命令解析出的凭据表开关。
        """
        calls.append(show_secrets)
        return 0

    monkeypatch.setattr(module, "run_query", fake_run_query)
    monkeypatch.setattr(module.sys, "argv", ["mp-db.py", *argv])

    assert module.main() == 0
    assert calls == [expected]
