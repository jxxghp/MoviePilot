"""
订阅业务活动的升级回放、旧历史与双数据库建表合同。
"""

import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.db.models.subscribe import Subscribe
from app.db.models.subscribeactivity import SubscribeActivity
from app.db.models.subscribehistory import SubscribeHistory


def _migration(monkeypatch, connection):
    """
    把迁移绑定到独立测试数据库。
    """
    migration = importlib.import_module("database.versions.e6a4c9f20813_subscription_activity")
    monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
    return migration


def test_activity_migration_backfills_only_active_identity_and_does_not_invent_events(monkeypatch):
    """
    已有整数 ID 和业务内容保持不变，升级前历史不会关联新的同 ID 订阅。
    """
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE subscribe (id INTEGER PRIMARY KEY, name TEXT)"))
        connection.execute(sa.text("CREATE TABLE subscribehistory (id INTEGER PRIMARY KEY, name TEXT)"))
        connection.execute(sa.text("INSERT INTO subscribe VALUES (7, 'Active')"))
        connection.execute(sa.text("INSERT INTO subscribehistory VALUES (7, 'Old history')"))
        migration = _migration(monkeypatch, connection)
        migration.upgrade()
        key = connection.execute(sa.text("SELECT activity_key FROM subscribe WHERE id=7")).scalar_one()
        assert len(key) == 32
        assert connection.execute(sa.text("SELECT activity_key FROM subscribehistory WHERE id=7")).scalar_one() is None
        assert connection.execute(sa.select(sa.func.count()).select_from(SubscribeActivity)).scalar_one() == 0
        migration.upgrade()
        assert connection.execute(sa.text("SELECT activity_key FROM subscribe WHERE id=7")).scalar_one() == key
        assert {column["name"] for column in sa.inspect(connection).get_columns("subscribeactivity")} == set(SubscribeActivity.__table__.columns.keys())
        migration.downgrade()
        migration.downgrade()
        assert connection.execute(sa.text("SELECT name FROM subscribe WHERE id=7")).scalar_one() == "Active"
    engine.dispose()


def test_activity_migration_accepts_fresh_model_tables(monkeypatch):
    """
    全新模型建表之后再次执行升级不会重复创建列或索引。
    """
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        for model in (Subscribe, SubscribeHistory, SubscribeActivity):
            model.__table__.create(connection)
        _migration(monkeypatch, connection).upgrade()
        assert "subscribeactivity" in sa.inspect(connection).get_table_names()
    engine.dispose()


def test_activity_model_compiles_for_postgresql():
    """
    事件身份和 JSON 明细使用 PostgreSQL 支持的稳定列类型。
    """
    ddl = str(CreateTable(SubscribeActivity.__table__).compile(dialect=postgresql.dialect()))
    assert "tracking JSON NOT NULL" in ddl
    assert "event_id VARCHAR(64) NOT NULL" in ddl
    assert "UNIQUE (event_id)" in ddl
