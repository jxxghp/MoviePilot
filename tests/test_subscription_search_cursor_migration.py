"""搜索站点游标与旧排期升级回归。"""

import importlib
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_search_cursor_migration_preserves_cooldown_and_running_tasks(monkeypatch):
    """只提前未开始的自动排期，冷却、停止、新增保护期和在途任务保持原状。"""
    migration = importlib.import_module("database.versions.a9c3e7f1b5d8_3_0_31")
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    tasks = sa.Table(
        "subscriptionsearchtask", metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String()), sa.Column("state", sa.String()),
        sa.Column("phase", sa.String()), sa.Column("attempt_count", sa.Integer()),
        sa.Column("cancel_requested", sa.Integer()), sa.Column("available_at", sa.String()),
    )
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(timespec="seconds")
    with engine.begin() as connection:
        metadata.create_all(connection)
        rows = [dict(id=index, source="fallback", state="queued", phase="queued", attempt_count=0,
                     cancel_requested=0, available_at=future) for index in range(5)]
        rows[1].update(phase="waiting_site_budget", attempt_count=1)
        rows[2].update(cancel_requested=1)
        rows[3].update(source="new", phase="scheduled")
        rows[4].update(state="running", attempt_count=1)
        connection.execute(tasks.insert(), rows)
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        migration.upgrade()
        stored = connection.execute(sa.select(tasks.c.id, tasks.c.available_at).order_by(tasks.c.id)).all()
        assert stored[0].available_at < future
        assert all(row.available_at == future for row in stored[1:])
        assert "pending_site_ids" in {col["name"] for col in sa.inspect(connection).get_columns(tasks.name)}
        migration.downgrade()
        assert "pending_site_ids" not in {col["name"] for col in sa.inspect(connection).get_columns(tasks.name)}
        assert connection.scalar(sa.select(sa.func.count()).select_from(tasks)) == 5
    engine.dispose()
