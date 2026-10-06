"""搜索页快照 Alembic 在旧库和基础建表路径上的可逆性。"""

import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_search_session_migration_is_idempotent_and_reversible(monkeypatch):
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = importlib.import_module("database.versions.d9e2a6b4c803_3_1_1")
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        migration.upgrade()
        columns = {value["name"] for value in sa.inspect(connection).get_columns("searchsession")}
        assert columns == {"id", "task_id", "version", "payload", "updated_at"}
        connection.execute(sa.text("INSERT INTO searchsession (task_id, version, payload, updated_at) VALUES ('t',0,'{}','now')"))
        assert connection.execute(sa.text("SELECT COUNT(*) FROM searchsession")).scalar_one() == 1
        migration.downgrade()
        assert "searchsession" not in sa.inspect(connection).get_table_names()
        migration.upgrade()
        assert connection.execute(sa.text("SELECT COUNT(*) FROM searchsession")).scalar_one() == 0
    engine.dispose()
