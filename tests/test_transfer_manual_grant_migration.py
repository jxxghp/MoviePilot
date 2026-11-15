"""验证任务授权状态槽迁移不影响已有整理任务。"""

import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_manual_grant_migration_preserves_existing_pending_rows(monkeypatch):
    """
    升级增加空授权槽，降级只移除该列并保留原任务数据。
    """
    migration = importlib.import_module("database.versions.b7c1e3d5f9a2_transfer_manual_grant")
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text(
            "CREATE TABLE transferpending (id INTEGER PRIMARY KEY, task_id TEXT NOT NULL)"
        ))
        connection.execute(sa.text("INSERT INTO transferpending (id, task_id) VALUES (1, 'task-1')"))
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))

        migration.upgrade()
        columns = {column["name"]: column for column in sa.inspect(connection).get_columns("transferpending")}
        assert columns["manual_identity_authorization"]["nullable"] is True
        assert connection.execute(sa.text(
            "SELECT task_id, manual_identity_authorization FROM transferpending"
        )).all() == [("task-1", None)]
        migration.upgrade()
        assert len(sa.inspect(connection).get_columns("transferpending")) == len(columns)

        migration.downgrade()
        migration.downgrade()
        assert "manual_identity_authorization" not in {
            column["name"] for column in sa.inspect(connection).get_columns("transferpending")
        }
        assert connection.execute(sa.text("SELECT task_id FROM transferpending")).all() == [("task-1",)]
    engine.dispose()
