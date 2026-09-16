"""订阅搜索业务阶段字段的 Alembic 可逆迁移测试。"""

import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION = "database.versions.f3c8a1d6b2e9_3_0_22"


def _bind_migration(monkeypatch, connection):
    """把 3.0.22 迁移绑定到隔离 SQLite 连接。"""
    migration = importlib.import_module(MIGRATION)
    monkeypatch.setattr(
        migration,
        "op",
        Operations(MigrationContext.configure(connection)),
    )
    return migration


def _create_legacy_search_task(connection) -> None:
    """创建迁移前的最小订阅搜索任务表和一条排队记录。"""
    metadata = sa.MetaData()
    task = sa.Table(
        "subscriptionsearchtask",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
    )
    metadata.create_all(connection)
    connection.execute(task.insert(), {"id": 1, "task_id": "task-1", "state": "queued"})


def test_subscription_status_migration_upgrade_downgrade_reupgrade(monkeypatch) -> None:
    """存量任务应获得默认阶段，迁移可重复执行并完整回滚。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _create_legacy_search_task(connection)
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()
        migration.upgrade()

        columns = {
            column["name"]
            for column in sa.inspect(connection).get_columns("subscriptionsearchtask")
        }
        assert {"phase", "current_site_id"}.issubset(columns)
        row = connection.execute(
            sa.text(
                "SELECT phase, current_site_id FROM subscriptionsearchtask WHERE id = 1"
            )
        ).mappings().one()
        assert dict(row) == {"phase": "queued", "current_site_id": None}

        migration.downgrade()
        downgraded = {
            column["name"]
            for column in sa.inspect(connection).get_columns("subscriptionsearchtask")
        }
        assert "phase" not in downgraded
        assert "current_site_id" not in downgraded

        migration.upgrade()
        assert connection.execute(
            sa.text("SELECT phase FROM subscriptionsearchtask WHERE id = 1")
        ).scalar_one() == "queued"
    engine.dispose()
