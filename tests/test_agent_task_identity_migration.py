"""
验证计划任务执行账号迁移保留旧任务与历史，兼容重复升级、空库和 ORM 已建表。
"""

import importlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.db.models.agenttask import AgentTask
from app.db.models.agenttaskrun import AgentTaskRun


def _migration(monkeypatch, connection):
    """
    把身份迁移绑定到临时连接，不执行真实应用数据库升级。
    """
    migration = importlib.import_module("database.versions.97ad7239c1e0_agent_task_identity")
    monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
    return migration


def test_task_identity_migration_preserves_legacy_records_and_requires_confirmation(monkeypatch):
    """
    旧记录增加空身份列，不根据数字用户名猜账号；回滚也保留历史内容。
    """
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        metadata = sa.MetaData()
        for name in ("agenttask", "agenttaskrun"):
            table = sa.Table(
                name, metadata, sa.Column("id", sa.Integer(), primary_key=True),
                sa.Column("username", sa.String()), sa.Column("result", sa.String()),
            )
            table.create(connection)
            connection.execute(table.insert().values(id=1, username="123456", result="旧执行结果"))
        migration = _migration(monkeypatch, connection)
        migration.upgrade()
        migration.upgrade()
        for name in ("agenttask", "agenttaskrun"):
            table = sa.Table(name, sa.MetaData(), autoload_with=connection)
            record = connection.execute(sa.select(table)).mappings().one()
            assert record["execution_user_id"] is None
            assert record["execution_user_identity"] is None
            assert record["username"] == "123456" and record["result"] == "旧执行结果"
        migration.downgrade()
        migration.downgrade()
        for name in ("agenttask", "agenttaskrun"):
            table = sa.Table(name, sa.MetaData(), autoload_with=connection)
            assert set(table.columns.keys()) == {"id", "username", "result"}
            assert connection.execute(sa.select(table.c.result)).scalar_one() == "旧执行结果"


@pytest.mark.parametrize("fresh_schema", [False, True])
def test_task_identity_migration_accepts_empty_and_current_schema(monkeypatch, fresh_schema):
    """
    空库和 create_all 已包含新字段的库都能幂等升级。
    """
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        if fresh_schema:
            AgentTask.__table__.create(connection)
            AgentTaskRun.__table__.create(connection)
        migration = _migration(monkeypatch, connection)
        migration.upgrade()
        migration.upgrade()
        for model in (AgentTask, AgentTaskRun) if fresh_schema else ():
            columns = sa.inspect(connection).get_columns(model.__tablename__)
            assert {column["name"] for column in columns} == set(model.__table__.columns.keys())
