"""整理待处理表 3.0.4 初始迁移的中断恢复测试。"""

import importlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION = "database.versions.e3d9f4b7c806_3_0_4"


def _bind_migration(monkeypatch, connection):
    """把 3.0.4 迁移绑定到隔离 SQLite 连接。"""
    migration = importlib.import_module(MIGRATION)
    monkeypatch.setattr(
        migration,
        "op",
        Operations(MigrationContext.configure(connection)),
    )
    return migration


def test_upgrade_repairs_missing_and_malformed_identity_index(monkeypatch) -> None:
    """建表后中断或同名错误索引都必须收敛为精确唯一索引。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text(
            "CREATE TABLE transferpending ("
            "id INTEGER PRIMARY KEY, storage VARCHAR NOT NULL, "
            "src_path VARCHAR NOT NULL, created_at VARCHAR)"
        ))
        migration = _bind_migration(monkeypatch, connection)
        migration.upgrade()
        connection.execute(sa.text(
            "DROP INDEX ux_transferpending_storage_path"
        ))
        connection.execute(sa.text(
            "CREATE INDEX ux_transferpending_storage_path "
            "ON transferpending (src_path)"
        ))

        migration.upgrade()

        index = sa.inspect(connection).get_indexes("transferpending")[0]
        assert index["name"] == "ux_transferpending_storage_path"
        assert index["column_names"] == ["storage", "src_path"]
        assert index["unique"] == 1


def test_upgrade_recreates_empty_partial_table(monkeypatch) -> None:
    """中断留下的空残表可以无损重建为完整 3.0.4 结构。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text(
            "CREATE TABLE transferpending (id INTEGER PRIMARY KEY)"
        ))
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()

        assert {
            column["name"]
            for column in sa.inspect(connection).get_columns("transferpending")
        } == {"id", "storage", "src_path", "created_at"}


def test_upgrade_keeps_current_schema_precreated_with_execution_fk(monkeypatch) -> None:
    """首次初始化已由当前 ORM 建表时，旧迁移不得删除被步骤表依赖的父表。"""
    engine = sa.create_engine("sqlite://")
    with engine.connect() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys=ON"))
        connection.commit()
        with connection.begin():
            connection.execute(sa.text(
                "CREATE TABLE transferpending ("
                "id INTEGER PRIMARY KEY, storage VARCHAR NOT NULL, "
                "src_path VARCHAR NOT NULL, created_at VARCHAR, "
                "task_id VARCHAR NOT NULL UNIQUE, execution_state VARCHAR NOT NULL)"
            ))
            connection.execute(sa.text(
                "CREATE TABLE transferexecutionstep ("
                "id INTEGER PRIMARY KEY, task_id VARCHAR NOT NULL, "
                "FOREIGN KEY (task_id) REFERENCES transferpending(task_id) "
                "ON DELETE CASCADE)"
            ))
            migration = _bind_migration(monkeypatch, connection)

            migration.upgrade()

            inspector = sa.inspect(connection)
            assert "transferexecutionstep" in inspector.get_table_names()
            assert "task_id" in {
                column["name"]
                for column in inspector.get_columns("transferpending")
            }
            index = next(
                index
                for index in inspector.get_indexes("transferpending")
                if index["name"] == "ux_transferpending_storage_path"
            )
            assert index["column_names"] == ["storage", "src_path"]
            assert index["unique"] == 1


def test_upgrade_rejects_nonempty_partial_table(monkeypatch) -> None:
    """含数据残表无法可靠推断源身份时必须显式拒绝迁移。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text(
            "CREATE TABLE transferpending (id INTEGER PRIMARY KEY)"
        ))
        connection.execute(sa.text(
            "INSERT INTO transferpending (id) VALUES (1)"
        ))
        migration = _bind_migration(monkeypatch, connection)

        with pytest.raises(RuntimeError, match="含数据的不完整 transferpending"):
            migration.upgrade()


def test_downgrade_tolerates_interrupted_missing_index(monkeypatch) -> None:
    """索引创建前中断时降级仍应安全删除残留表。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text(
            "CREATE TABLE transferpending ("
            "id INTEGER PRIMARY KEY, storage VARCHAR NOT NULL, "
            "src_path VARCHAR NOT NULL, created_at VARCHAR)"
        ))
        migration = _bind_migration(monkeypatch, connection)

        migration.downgrade()

        assert "transferpending" not in sa.inspect(connection).get_table_names()
