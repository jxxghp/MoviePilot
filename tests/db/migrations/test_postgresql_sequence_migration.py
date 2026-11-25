"""
验证 PostgreSQL 2.2.0 序列迁移跳过非整数主键并以子事务隔离单表异常。
"""

import importlib.util
from pathlib import Path
from typing import Any

import pytest

MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "database"
    / "versions"
    / "5b3355c964bb_2_2_0.py"
)


def _load_migration_module():
    """
    动态加载 5b3355c964bb 迁移模块，避免受数字开头文件名限制。
    """
    spec = importlib.util.spec_from_file_location(
        "moviepilot_migration_5b3355c964bb",
        MIGRATION_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeNestedTransaction:
    """
    记录子事务（SAVEPOINT）提交与回滚次数的轻量替身。
    """

    def __init__(self, events: list[str], depth: int) -> None:
        """
        初始化子事务记录器。

        :param events: 共享的事务事件日志
        :param depth: 当前子事务嵌套深度
        """
        self._events = events
        self._depth = depth

    def commit(self) -> None:
        """
        记录子事务提交。
        """
        self._events.append(f"nested_commit:{self._depth}")

    def rollback(self) -> None:
        """
        记录子事务回滚。
        """
        self._events.append(f"nested_rollback:{self._depth}")


class _FakeResult:
    """
    返回单行查询结果的 SQLAlchemy Result 替身。
    """

    def __init__(self, row: tuple[Any, ...] | None) -> None:
        """
        初始化查询结果替身。

        :param row: ``fetchone()`` 返回的单行元组或 None
        """
        self._row = row

    def fetchone(self) -> tuple[Any, ...] | None:
        """
        返回预设的单行结果。
        """
        return self._row


class _FakePostgresConnection:
    """
    模拟 PostgreSQL 迁移连接，用于验证元数据查询、DDL 与事务边界。
    """

    def __init__(
        self,
        *,
        columns: dict[str, tuple[str, str | None, str] | None],
        next_values: dict[str, int] | None = None,
        alter_errors: dict[str, Exception] | None = None,
    ) -> None:
        """
        初始化伪造的 PostgreSQL 连接。

        :param columns: 表名到 ``(is_identity, column_default, data_type)`` 的映射
        :param next_values: 表名到 ``COALESCE(MAX(id), 0) + 1`` 返回值的映射
        :param alter_errors: 表名到执行 ``ALTER TABLE`` 时抛出异常的映射
        """
        self._columns = columns
        self._next_values = next_values or {}
        self._alter_errors = alter_errors or {}
        self.executed: list[tuple[str, dict[str, Any] | None]] = []
        self.events: list[str] = []
        self.outer_rollbacks = 0
        self._nested_depth = 0

    def begin_nested(self) -> _FakeNestedTransaction:
        """
        开启一个模拟 SAVEPOINT 子事务。
        """
        self._nested_depth += 1
        self.events.append(f"nested_begin:{self._nested_depth}")
        return _FakeNestedTransaction(self.events, self._nested_depth)

    def rollback(self) -> None:
        """
        记录外层连接事务回滚，供测试断言外层事务未被破坏。
        """
        self.outer_rollbacks += 1
        self.events.append("outer_rollback")

    def execute(
        self,
        statement: Any,
        params: dict[str, Any] | None = None,
    ) -> _FakeResult:
        """
        拦截并响应迁移脚本执行的 SQL 语句。

        :param statement: SQLAlchemy 文本语句
        :param params: 可选绑定参数字典
        :return: 对应 SQL 的模拟查询结果
        """
        sql = str(statement)
        self.executed.append((sql, params))
        if "FROM information_schema.columns" in sql:
            assert "table_schema = 'public'" in sql
            assert params is not None and "table_name" in params
            return _FakeResult(self._columns.get(params["table_name"]))
        if "SELECT COALESCE(MAX(id), 0) + 1" in sql:
            table_name = sql.split('FROM "', 1)[1].split('"', 1)[0]
            return _FakeResult((self._next_values.get(table_name, 1),))
        if "ALTER TABLE" in sql:
            table_name = sql.split('ALTER TABLE "', 1)[1].split('"', 1)[0]
            if table_name in self._alter_errors:
                raise self._alter_errors[table_name]
            return _FakeResult(None)
        raise AssertionError(f"未预期的 SQL 语句: {sql}")


@pytest.mark.parametrize(
    ("table_name", "data_type"),
    [
        ("channelauthorization", "character varying"),
        ("channelbinding", "character varying"),
        ("custom_text_id", "text"),
        ("custom_uuid_id", "uuid"),
    ],
)
def test_fix_table_sequence_skips_non_integer_id_columns(
    table_name: str,
    data_type: str,
) -> None:
    """
    字符串等非整数主键表应被跳过，不得执行 MAX(id) 聚合或 Identity 转换。
    """
    migration = _load_migration_module()
    connection = _FakePostgresConnection(
        columns={table_name: ("NO", None, data_type)},
    )

    migration.fix_table_sequence(connection, table_name)

    assert len(connection.executed) == 1
    assert "FROM information_schema.columns" in connection.executed[0][0]
    assert connection.executed[0][1] == {"table_name": table_name}
    assert connection.outer_rollbacks == 0
    assert connection.events == ["nested_begin:1", "nested_commit:1"]


@pytest.mark.parametrize("data_type", ["smallint", "integer", "bigint"])
def test_fix_table_sequence_converts_integer_id_without_identity(
    data_type: str,
) -> None:
    """
    未启用 Identity 的整数主键表应按当前最大值加一转换为 Identity。
    """
    migration = _load_migration_module()
    connection = _FakePostgresConnection(
        columns={"subscribe": ("NO", "nextval('subscribe_id_seq'::regclass)", data_type)},
        next_values={"subscribe": 42},
    )

    migration.fix_table_sequence(connection, "subscribe")

    assert len(connection.executed) == 3
    assert "SELECT COALESCE(MAX(id), 0) + 1 as next_value" in connection.executed[1][0]
    assert 'FROM "subscribe"' in connection.executed[1][0]
    assert (
        "ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (START WITH 42)"
        in connection.executed[2][0]
    )
    assert connection.outer_rollbacks == 0
    assert connection.events == [
        "nested_begin:1",
        "nested_begin:2",
        "nested_commit:2",
        "nested_commit:1",
    ]


def test_fix_table_sequence_skips_version_table_missing_id_and_existing_identity() -> None:
    """
    alembic_version、无 id 列的表以及已有 Identity 的表应跳过转换。
    """
    migration = _load_migration_module()
    connection = _FakePostgresConnection(
        columns={
            "no_id_table": None,
            "already_identity_flag": ("YES", None, "integer"),
            "already_identity_default": (
                "NO",
                "GENERATED BY DEFAULT AS IDENTITY",
                "bigint",
            ),
        },
    )

    for table_name in (
        "alembic_version",
        "no_id_table",
        "already_identity_flag",
        "already_identity_default",
    ):
        migration.fix_table_sequence(connection, table_name)

    assert all("ALTER TABLE" not in sql for sql, _ in connection.executed)
    assert all("COALESCE(MAX(id)" not in sql for sql, _ in connection.executed)
    assert connection.outer_rollbacks == 0


def test_fix_table_sequence_isolates_table_failure_without_rolling_back_outer_tx() -> None:
    """
    单表转换报错或命中已存在 Identity 时，只回滚子事务，不能回滚 Alembic 外层事务。
    """
    migration = _load_migration_module()
    connection = _FakePostgresConnection(
        columns={
            "already_identity_err": ("NO", None, "integer"),
            "broken_table": ("NO", None, "integer"),
            "healthy_table": ("NO", None, "integer"),
        },
        next_values={
            "already_identity_err": 5,
            "broken_table": 9,
            "healthy_table": 12,
        },
        alter_errors={
            "already_identity_err": RuntimeError(
                'column "id" of relation "already_identity_err" is already an identity column'
            ),
            "broken_table": RuntimeError("column already has a default value"),
        },
    )

    migration.fix_table_sequence(connection, "already_identity_err")
    migration.fix_table_sequence(connection, "broken_table")
    migration.fix_table_sequence(connection, "healthy_table")

    assert connection.outer_rollbacks == 0
    assert any(
        'ALTER TABLE "healthy_table"' in sql and "START WITH 12" in sql
        for sql, _ in connection.executed
    )
    assert "nested_rollback:2" in connection.events
    assert "nested_rollback:4" in connection.events
    assert "nested_rollback:3" in connection.events
