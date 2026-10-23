"""
验证本体启用快照持久化、崩溃恢复及旧journal未知证据处理。
"""

import importlib
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import sessionmaker

from app.application.plugin.recovery import PluginInstallationRecoveryService
from app.application.plugin.transaction import PluginInstallationPhase, PluginPersistenceService
from app.db.adapters.plugininstallation import TransactionalPluginInstallationStore
from app.db.models.plugininstallation import PluginInstallation
from app.db.models.plugininstance import PluginInstance
from app.db.models.systemconfig import SystemConfig
from tests.test_plugin_install_transaction import _AtomicSystemConfig, _record
from tests.test_plugin_installation_recovery import _packages, _Persistence


@pytest.mark.parametrize("enabled", [True, False])
def test_host_enabled_snapshot_survives_new_repository_instance(tmp_path, enabled):
    """
    新建仓储重新读取后仍能取得安装前的真实启用位，不依赖进程内finally状态。
    """
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'journal.db'}")
    for model in (SystemConfig, PluginInstallation):
        model.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    config = _AtomicSystemConfig(factory)
    try:
        first = TransactionalPluginInstallationStore(factory, config.update_atomically)
        first.create(replace(_record(), host_enabled_before=enabled))
        restored = TransactionalPluginInstallationStore(factory, config.update_atomically).get("txn-demo-1")
        assert restored.host_enabled_before is enabled
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_startup_restores_enabled_snapshot_before_deleting_journal(enabled):
    """
    模拟安装后崩溃，启动恢复先还原文件和启用位，再删journal与恢复材料。
    """
    record = replace(_record(), host_enabled_before=enabled)
    persistence = _Persistence([record])
    order = []
    packages = _packages(async_restore=AsyncMock(side_effect=lambda _: order.append("files")))

    async def restore_host(plugin_id, previous):
        """
        journal仍在时恢复其持久启用位。
        """
        assert persistence.records
        assert plugin_id == record.plugin_id and previous is enabled
        order.append("enabled")

    service = PluginInstallationRecoveryService(
        persistence=persistence, packages=packages, restore_host_enabled=restore_host,
    )
    result = await service.replay()
    assert result.restored == 1 and result.failed == 0
    assert order == ["files", "enabled"]
    assert not persistence.records


@pytest.mark.asyncio
async def test_legacy_unknown_host_enabled_journal_is_retained():
    """
    旧journal缺少被提前覆盖的启用位，不能猜测并删除唯一恢复证据。
    """
    record = _record()
    persistence = _Persistence([record])
    packages = _packages()
    restorer = AsyncMock()
    service = PluginInstallationRecoveryService(
        persistence=persistence, packages=packages, restore_host_enabled=restorer,
    )
    result = await service.replay()
    assert result.failed == 1 and result.restored == 0
    assert persistence.records[record.transaction_id].phase is PluginInstallationPhase.PREPARED
    packages.async_restore.assert_awaited_once()
    packages.async_cleanup.assert_not_awaited()
    restorer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_known_enabled_snapshot_without_restorer_retains_real_journal(tmp_path, enabled):
    """
    真实journal已知旧启用位但当前状态相反时，缺少恢复端口必须保留唯一恢复证据。
    """
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'missing-restorer.db'}")
    for model in (SystemConfig, PluginInstallation, PluginInstance):
        model.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    config = _AtomicSystemConfig(factory)
    store = TransactionalPluginInstallationStore(factory, config.update_atomically)
    record = replace(_record(), host_enabled_before=enabled)
    store.create(record)
    with factory.begin() as session:
        session.add(PluginInstance(instance_id=record.plugin_id, source_plugin_id=record.plugin_id,
            is_enabled=not enabled, created_at=record.created_at.isoformat(), updated_at=record.updated_at.isoformat()))

    async def run(operation):
        """
        在测试线程执行独占临时数据库的持久化操作。
        """
        return operation()

    packages = _packages()
    persistence = PluginPersistenceService(executor=SimpleNamespace(run=run), identities=Mock(), installations=store)
    service = PluginInstallationRecoveryService(persistence=persistence, packages=packages)
    try:
        result = await service.replay()
        assert result.failed == 1 and result.restored == 0
        retained = TransactionalPluginInstallationStore(factory, config.update_atomically).get(record.transaction_id)
        assert retained is not None and retained.host_enabled_before is enabled
        with factory() as session:
            current = session.execute(sa.select(PluginInstance)).scalar_one()
            assert current.is_enabled is not enabled
        packages.async_restore.assert_awaited_once()
        packages.async_cleanup.assert_not_awaited()
    finally:
        engine.dispose()


def test_journal_enabled_migration_preserves_unknown_and_round_trips(monkeypatch):
    """
    独立升级将旧事务启用位保留NULL，两次升级幂等，降级不改变原journal字段。
    """
    migration = importlib.import_module("database.versions.b8d2f5a01962_plugin_data_identity")
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    data = sa.Table("plugindata", metadata, sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("plugin_id", sa.String), sa.Column("key", sa.String))
    sa.Index("ix_plugindata_plugin_id_key", data.c.plugin_id, data.c.key)
    journal = sa.Table("plugininstallation", metadata, sa.Column("id", sa.Integer, primary_key=True),
                       sa.Column("transaction_id", sa.String))
    with engine.begin() as connection:
        metadata.create_all(connection)
        connection.execute(journal.insert().values(id=1, transaction_id="old"))
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        migration.upgrade()
        assert connection.execute(sa.text("SELECT host_enabled_before FROM plugininstallation")).scalar_one() is None
        migration.downgrade()
        assert "host_enabled_before" not in {column["name"] for column in sa.inspect(connection).get_columns("plugininstallation")}
        assert connection.execute(sa.select(journal.c.transaction_id)).scalar_one() == "old"
    engine.dispose()
