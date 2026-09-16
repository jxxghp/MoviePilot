"""逐订阅搜索周期的到期、写入和数据库迁移合同。"""

import importlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import ValidationError

from app.application.subscription.contract import SubscriptionSnapshot
from app.application.subscription.query import subscription_search_due
from app.chain.subscribe.facade import SubscribeChain
from app.db.adapters.subscription import TransactionalSubscriptionRepository
from app.db.models.subscribe import Subscribe
from app.db.session import SessionFactory, async_session_scope
from app.scheduler.catalog import _subscription_search_job_specs
from app.schemas.subscribe import Subscribe as SubscribeSchema

NOW = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("interval,elapsed,due", [
    (None, 23, False), (None, 24, True), (1, 0.99, False), (1, 1, True),
    (48, 24, False), (48, 48, True), (1, -1, False),
])
def test_search_due_uses_custom_or_system_interval(interval, elapsed, due):
    """独立周期可短于或长于系统周期，并在精确到期时放行。"""
    subscribe = SubscriptionSnapshot(
        id=1, name="周期测试", state="R", search_interval=interval,
        last_search=(NOW - timedelta(hours=elapsed)).isoformat(),
    )
    assert subscription_search_due(subscribe, 24, NOW) is due
    assert subscription_search_due(replace(subscribe, state="P"), 24, NOW) is due
    assert not subscription_search_due(replace(subscribe, state="S"), 24, NOW)
    assert not subscription_search_due(replace(subscribe, state="N"), 24, NOW)


def test_legacy_creation_time_and_missing_timestamp():
    """旧本地时间按本地时区解析，缺失或损坏时间允许恢复搜索。"""
    recent = (NOW - timedelta(hours=1)).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    subscribe = SubscriptionSnapshot(id=1, name="旧订阅", state="R", date=recent)
    assert not subscription_search_due(subscribe, 24, NOW)
    for value in (None, "invalid"):
        assert subscription_search_due(replace(subscribe, date=value), 24, NOW)


def test_targeted_and_manual_selection_bypasses_schedule():
    """自动调度仅选到期记录，手动及指定目标保持原有立即搜索语义。"""
    recent = datetime.now(timezone.utc).isoformat()
    subscribe = SubscriptionSnapshot(id=1, name="测试", state="R", last_search=recent)
    chain = object.__new__(SubscribeChain)
    chain.subscription_repository = SimpleNamespace(
        list=Mock(return_value=[subscribe]), get=Mock(return_value=subscribe),
    )
    assert chain._load_search_subscriptions(None, None, "R", scheduled_interval=24) == []
    assert chain._load_search_subscriptions(None, None, "R") == [subscribe]
    assert chain._load_search_subscriptions(1, None, "R", scheduled_interval=24) == [subscribe]
    assert chain._load_search_subscriptions(None, (1,), "R", scheduled_interval=24) == [subscribe]


def test_scheduled_scan_does_not_create_empty_batches():
    """尚未到期的订阅不产生空批次，也不触发队列消费。"""
    subscribe = SubscriptionSnapshot(
        id=1, name="测试", state="R", last_search=datetime.now(timezone.utc).isoformat(),
    )
    chain = object.__new__(SubscribeChain)
    chain.subscription_repository = SimpleNamespace(list=Mock(return_value=[subscribe]))
    chain.subscription_search_repository = Mock()
    progress = Mock()
    assert chain.search(state="R", scheduled_interval=24, progress_callback=progress) is None
    chain.subscription_search_repository.enqueue.assert_not_called()
    chain.subscription_search_repository.claim_next.assert_not_called()
    assert progress.call_args.kwargs["value"] == 100


@pytest.mark.parametrize("interval", [0, -1, 1.5, 8761])
def test_invalid_search_interval_is_rejected(interval):
    """接口拒绝零值、负数、小数和超出范围的独立周期。"""
    with pytest.raises(ValidationError):
        SubscribeSchema(search_interval=interval)


def test_public_write_can_clear_interval_but_cannot_forge_search_time():
    """恢复系统周期必须保留 null，客户端不能覆盖内部搜索时钟。"""
    for value in (None, ""):
        payload = SubscribeSchema(search_interval=value, last_search=NOW.isoformat())
        assert payload.to_public_write_payload(exclude_unset=True) == {"search_interval": None}
    assert SubscribeSchema().to_public_write_payload(exclude_unset=True) == {}


def test_schedule_persists_across_repository_recreation(db):
    """周期和搜索时间在 Session 关闭及仓储重建后仍可用于到期判断。"""
    row = db.add(Subscribe(
        name="持久周期", state="R", search_interval=48, last_search=NOW.isoformat(),
    ))
    for _ in range(2):
        repository = TransactionalSubscriptionRepository(
            sync_session=SessionFactory, async_session=async_session_scope,
        )
        snapshot = repository.get(row.id)
        assert snapshot.search_interval == 48
        assert snapshot.last_search == NOW.isoformat()
        assert not subscription_search_due(snapshot, 24, NOW + timedelta(hours=24))


def test_scheduler_passes_system_interval_only_to_periodic_search():
    """调度目录传递可热重载的系统间隔，新增订阅保持首次立即搜索。"""
    specs = {spec.job_id: spec for spec in _subscription_search_job_specs(Mock(), 12)}
    assert specs["subscribe_search"].kwargs == {"state": "R", "scheduled_interval": 12}
    assert specs["new_subscribe_search"].kwargs == {"state": "N"}


def test_schedule_migration_preserves_existing_rows_and_is_reversible(monkeypatch):
    """旧数据默认跟随系统，重复升级与回滚不损坏已有订阅。"""
    migration = importlib.import_module("database.versions.f8a2c6e9b1d4_3_0_30")
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        for table in ("subscribe", "subscribehistory"):
            connection.execute(sa.text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, name TEXT)"))
            connection.execute(sa.text(f"INSERT INTO {table} VALUES (1, '旧订阅')"))
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        migration.upgrade()
        for table in ("subscribe", "subscribehistory"):
            row = connection.execute(sa.text(f"SELECT name, search_interval FROM {table}")).one()
            assert row == ("旧订阅", None)
        assert connection.execute(sa.text("SELECT last_search FROM subscribe")).scalar_one() is None
        migration.downgrade()
        migration.upgrade()
        assert connection.execute(sa.text("SELECT name FROM subscribe")).scalar_one() == "旧订阅"
    engine.dispose()
