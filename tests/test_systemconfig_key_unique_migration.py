"""系统配置键唯一性 Alembic 迁移测试。"""

import importlib
from typing import Any, Protocol, cast

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from app.db.models.systemconfig import SystemConfig

_MIGRATION = "database.versions.b6e1f8a3c9d2_3_0_26"
_TABLE = "systemconfig"
_UNIQUE_INDEX = "ux_systemconfig_key"
_LEGACY_INDEX = "ix_systemconfig_key"


class _MigrationModule(Protocol):
    """声明测试所需的迁移模块接口。"""

    revision: str
    down_revision: str
    op: Any

    def upgrade(self) -> None:
        """执行升级迁移。"""

    def downgrade(self) -> None:
        """执行降级迁移。"""


def _bind_migration(
    monkeypatch: pytest.MonkeyPatch,
    connection: Connection,
) -> _MigrationModule:
    """把目标迁移绑定到隔离数据库连接。"""
    migration = cast(_MigrationModule, importlib.import_module(_MIGRATION))
    monkeypatch.setattr(
        migration,
        "op",
        Operations(MigrationContext.configure(connection)),
    )
    return migration


def _create_legacy_table(connection: Connection) -> sa.Table:
    """创建带重复键、空键和普通索引的旧版系统配置表。"""
    metadata = sa.MetaData()
    table = sa.Table(
        _TABLE,
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(), nullable=True),
        sa.Column("value", sa.JSON(), nullable=True),
    )
    sa.Index(_LEGACY_INDEX, table.c.key, unique=False)
    metadata.create_all(connection)
    connection.execute(
        table.insert(),
        [
            {"id": 1, "key": "Alpha", "value": {"version": 1}},
            {"id": 2, "key": "Beta", "value": {"version": 1}},
            {"id": 3, "key": "Alpha", "value": {"version": 2}},
            {"id": 4, "key": None, "value": {"invalid": True}},
            {"id": 5, "key": "", "value": {"invalid": True}},
            {"id": 6, "key": "   ", "value": {"invalid": True}},
        ],
    )
    return table


def _load_table(connection: Connection) -> sa.Table:
    """反射当前系统配置表结构。"""
    return sa.Table(
        _TABLE,
        sa.MetaData(),
        autoload_with=connection,
    )


def _rows(connection: Connection) -> list[dict[str, Any]]:
    """按 ID 返回当前系统配置数据。"""
    table = _load_table(connection)
    return [
        dict(row)
        for row in connection.execute(
            sa.select(table).order_by(table.c.id)
        ).mappings().all()
    ]


def _index_definitions(connection: Connection) -> dict[str, dict[str, Any]]:
    """按名称返回当前系统配置索引定义。"""
    return {
        str(index["name"]): index
        for index in sa.inspect(connection).get_indexes(_TABLE)
        if index.get("name")
    }


def _key_nullable(connection: Connection) -> bool:
    """返回当前系统配置键列的可空状态。"""
    column = next(
        item
        for item in sa.inspect(connection).get_columns(_TABLE)
        if item["name"] == "key"
    )
    return bool(column["nullable"])


def test_systemconfig_key_migration_upgrade_downgrade_reupgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """迁移应保留最新重复项、清理空键并支持完整往返。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _create_legacy_table(connection)
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()

        assert _rows(connection) == [
            {"id": 2, "key": "Beta", "value": {"version": 1}},
            {"id": 3, "key": "Alpha", "value": {"version": 2}},
        ]
        assert _key_nullable(connection) is False
        upgraded_indexes = _index_definitions(connection)
        assert set(upgraded_indexes) == {_UNIQUE_INDEX}
        assert upgraded_indexes[_UNIQUE_INDEX]["column_names"] == ["key"]
        assert bool(upgraded_indexes[_UNIQUE_INDEX]["unique"]) is True

        table = _load_table(connection)
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    table.insert().values(
                        id=7,
                        key="Alpha",
                        value={"version": 3},
                    )
                )
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    table.insert().values(
                        id=8,
                        key=None,
                        value={"invalid": True},
                    )
                )

        migration.downgrade()

        assert _key_nullable(connection) is True
        downgraded_indexes = _index_definitions(connection)
        assert set(downgraded_indexes) == {_LEGACY_INDEX}
        assert bool(downgraded_indexes[_LEGACY_INDEX]["unique"]) is False

        legacy_table = _load_table(connection)
        connection.execute(
            legacy_table.insert(),
            [
                {"id": 7, "key": "Alpha", "value": {"version": 3}},
                {"id": 8, "key": None, "value": {"invalid": True}},
            ],
        )

        migration.upgrade()

        assert _rows(connection) == [
            {"id": 2, "key": "Beta", "value": {"version": 1}},
            {"id": 7, "key": "Alpha", "value": {"version": 3}},
        ]
        assert _key_nullable(connection) is False
        assert set(_index_definitions(connection)) == {_UNIQUE_INDEX}


def test_systemconfig_model_matches_unique_key_schema() -> None:
    """ORM 模型应声明非空键和命名唯一索引。"""
    key_column = SystemConfig.__table__.c.key
    indexes = {index.name: index for index in SystemConfig.__table__.indexes}

    assert key_column.nullable is False
    assert set(indexes) == {_UNIQUE_INDEX}
    assert indexes[_UNIQUE_INDEX].unique is True
    assert tuple(column.name for column in indexes[_UNIQUE_INDEX].columns) == (
        "key",
    )


def test_systemconfig_key_migration_revision_chain() -> None:
    """迁移版本号应接在订阅治理 3.0.25 revision 之后。"""
    migration = cast(_MigrationModule, importlib.import_module(_MIGRATION))

    assert migration.revision == "b6e1f8a3c9d2"
    assert migration.down_revision == "c8f2e6a1d4b9"
