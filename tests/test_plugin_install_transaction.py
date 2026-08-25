"""插件安装事务记录、SQLite CAS 和 membership 测试。"""

import copy
import importlib
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import sessionmaker

try:
    import psycopg2 as postgres_driver
    from psycopg2 import sql

    POSTGRESQL_DIALECT = "postgresql+psycopg2"
except ModuleNotFoundError:
    import psycopg as postgres_driver
    from psycopg import sql

    POSTGRESQL_DIALECT = "postgresql+psycopg"

from app.application.plugin.identity import (
    PluginBindingBasis,
    PluginIdentity,
    PluginPayloadSourceType,
    TrustedPluginSourceType,
)
from app.application.plugin.transaction import (
    PluginInstallationConflictError,
    PluginInstallationPhase,
    PluginInstallationRecord,
    PluginInstallationRecordError,
)
from app.db.adapters.plugininstallation import TransactionalPluginInstallationStore
from app.db.models.pluginidentity import PluginIdentity as PluginIdentityModel
from app.db.models.plugininstallation import PluginInstallation
from app.db.models.systemconfig import SystemConfig

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def _identity(
    *,
    plugin_id: str = "DemoPlugin",
    revision: int = 1,
    version: str = "1.0.0",
) -> PluginIdentity:
    """构造一份满足来源身份合同的测试身份。"""
    return PluginIdentity(
        plugin_id=plugin_id,
        normalized_plugin_id=plugin_id.lower(),
        trusted_source_type=TrustedPluginSourceType.OFFICIAL,
        trusted_source_key="github:jxxghp/moviepilot-plugins",
        binding_basis=PluginBindingBasis.OFFICIAL_DEFAULT,
        payload_source_type=PluginPayloadSourceType.OFFICIAL,
        payload_source_key="github:jxxghp/moviepilot-plugins",
        declared_version=version,
        package_generation="v3",
        system_version=None,
        supports_v3=True,
        supports_v3t=False,
        payload_receipt="sha256:" + "0" * 64,
        revision=revision,
        created_at=NOW,
        updated_at=NOW,
        bound_at=NOW,
        payload_applied_at=NOW,
    )


def _record(**overrides) -> PluginInstallationRecord:
    """构造可跨进程恢复的安装事务记录。"""
    values = {
        "transaction_id": "txn-demo-1",
        "plugin_id": "DemoPlugin",
        "phase": PluginInstallationPhase.PREPARED,
        "membership_before": True,
        "membership_target": None,
        "identity_before_revision": 1,
        "identity_target_revision": None,
        "package_existed": True,
        "persistent_backup_existed": True,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return PluginInstallationRecord(**values)


def test_record_keeps_plugin_level_recovery_contract() -> None:
    """事务只记录目标插件 membership、CAS revision 和备份存在性。"""
    record = _record(
        phase="committed",
        membership_target=True,
        identity_target_revision=2,
    )

    assert record.phase is PluginInstallationPhase.COMMITTED
    assert record.membership_before is True
    assert record.membership_target is True
    assert record.identity_before_revision == 1
    assert record.identity_target_revision == 2
    assert record.package_existed is True
    assert record.persistent_backup_existed is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"transaction_id": "bad id"},
        {"plugin_id": " DemoPlugin"},
        {"membership_before": 1},
        {"membership_target": 1},
        {"identity_before_revision": 0},
        {"identity_target_revision": True},
        {"package_existed": 1},
        {"created_at": NOW.replace(tzinfo=None)},
        {"updated_at": NOW.replace(year=2025)},
        {"phase": "committed"},
    ],
)
def test_record_rejects_invalid_recovery_invariants(overrides: dict) -> None:
    """事务记录必须拒绝不能用于 CAS 或补偿恢复的状态。"""
    with pytest.raises(PluginInstallationRecordError):
        _record(**overrides)


def test_committed_record_requires_target_membership() -> None:
    """COMMITTED 不能指向尚未登记的业务目标。"""
    with pytest.raises(PluginInstallationRecordError):
        _record(phase=PluginInstallationPhase.COMMITTED)


def test_record_schema_version_is_explicit() -> None:
    """恢复读取必须拒绝未知 schema version。"""
    with pytest.raises(PluginInstallationRecordError):
        _record(schema_version=2)


def test_record_is_immutable() -> None:
    """事务记录提交后不能被调用方原地修改。"""
    record = _record()
    with pytest.raises(AttributeError):
        record.membership_before = False  # type: ignore[misc]

    assert replace(record, membership_before=False).membership_before is False


class _AtomicSystemConfig:
    """用测试 Session 模拟 SystemConfigOper 的配置锁和原子提交。"""

    def __init__(self, factory) -> None:
        self._factory = factory
        self._lock = threading.RLock()

    def update_atomically(self, key, mutation):
        """在测试数据库事务中锁定配置并执行关联写入。"""
        with self._lock:
            session = self._factory()
            try:
                with session.begin():
                    config = session.execute(
                        sa.select(SystemConfig)
                        .where(SystemConfig.key == key)
                        .with_for_update()
                    ).scalar_one_or_none()
                    current = copy.deepcopy(config.value if config else None)
                    result, value = mutation(session, current)
                    if config is None:
                        session.add(SystemConfig(key=key, value=copy.deepcopy(value)))
                    else:
                        config.value = copy.deepcopy(value)
                    session.flush()
                    return result
            finally:
                session.close()


@pytest.fixture
def installation_store(tmp_path):
    """创建带配置、身份和事务表的隔离 SQLite Store。"""
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'plugin-installation.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    for model in (SystemConfig, PluginIdentityModel, PluginInstallation):
        model.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    system_config = _AtomicSystemConfig(factory)
    try:
        yield engine, factory, TransactionalPluginInstallationStore(
            factory,
            system_config.update_atomically,
        )
    finally:
        engine.dispose()


def _store_record(
    *,
    transaction_id: str,
    plugin_id: str = "DemoPlugin",
    membership_before: bool = False,
    identity_before_revision: int | None = None,
) -> PluginInstallationRecord:
    """构造 Store 测试用的 PREPARED 记录。"""
    return PluginInstallationRecord(
        transaction_id=transaction_id,
        plugin_id=plugin_id,
        phase=PluginInstallationPhase.PREPARED,
        membership_before=membership_before,
        membership_target=None,
        identity_before_revision=identity_before_revision,
        identity_target_revision=None,
        package_existed=membership_before,
        persistent_backup_existed=False,
        created_at=NOW,
        updated_at=NOW,
    )


def _identity_model(identity: PluginIdentity) -> PluginIdentityModel:
    """把应用身份转换为测试数据库模型。"""
    return PluginIdentityModel(
        plugin_id=identity.plugin_id,
        normalized_plugin_id=identity.normalized_plugin_id,
        trusted_source_type=identity.trusted_source_type.value,
        trusted_source_key=identity.trusted_source_key,
        binding_basis=identity.binding_basis.value,
        payload_source_type=identity.payload_source_type.value,
        payload_source_key=identity.payload_source_key,
        declared_version=identity.declared_version,
        package_generation=identity.package_generation,
        supports_v3=identity.supports_v3,
        supports_v3t=identity.supports_v3t,
        payload_receipt=identity.payload_receipt,
        revision=identity.revision,
        created_at=identity.created_at.isoformat(),
        updated_at=identity.updated_at.isoformat(),
        bound_at=identity.bound_at.isoformat() if identity.bound_at else None,
        payload_applied_at=(
            identity.payload_applied_at.isoformat()
            if identity.payload_applied_at
            else None
        ),
    )


def _set_config(factory, value: list[str]) -> None:
    """直接准备测试用的安装清单。"""
    with factory() as session:
        config = session.execute(
            sa.select(SystemConfig).where(SystemConfig.key == "UserInstalledPlugins")
        ).scalar_one_or_none()
        if config is None:
            session.add(SystemConfig(key="UserInstalledPlugins", value=value))
        else:
            config.value = value
        session.commit()


def _get_config(factory) -> list[str] | None:
    """读取测试用的安装清单。"""
    with factory() as session:
        config = session.execute(
            sa.select(SystemConfig).where(SystemConfig.key == "UserInstalledPlugins")
        ).scalar_one_or_none()
        return copy.deepcopy(config.value) if config else None


def _upgrade_migration(connection, module_name: str) -> None:
    """在当前隔离 schema 中按生产 Alembic 路径执行迁移。"""
    migration = importlib.import_module(module_name)
    original_op = migration.op
    try:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
    finally:
        migration.op = original_op


def _set_identity_revision(
    factory,
    revision: int,
    plugin_id: str = "DemoPlugin",
) -> None:
    """模拟事务外的身份 revision 更新。"""
    with factory() as session:
        identity = session.execute(
            sa.select(PluginIdentityModel).where(
                PluginIdentityModel.normalized_plugin_id == plugin_id.lower()
            )
        ).scalar_one()
        identity.revision = revision
        session.commit()


def test_store_round_trips_plugin_level_journal(installation_store) -> None:
    """SQLite 往返只保留插件级 membership、revision 和备份标记。"""
    _, _, store = installation_store
    record = _store_record(transaction_id="install-roundtrip")

    store.create(record)

    restored = store.get(record.transaction_id)
    assert restored == record


@pytest.mark.parametrize(
    "phase",
    [PluginInstallationPhase.PREPARED, PluginInstallationPhase.COMMITTED],
)
def test_store_blocks_new_journal_until_previous_phase_is_closed(
    installation_store,
    phase: PluginInstallationPhase,
) -> None:
    """同一物理插件的未收尾 journal 不得被后续事务覆盖。"""
    _, _, store = installation_store
    existing = _store_record(transaction_id=f"install-{phase.value}")
    if phase is PluginInstallationPhase.COMMITTED:
        existing = replace(existing, phase=phase, membership_target=True)
    store.create(existing)

    with pytest.raises(PluginInstallationConflictError, match="未收尾安装事务"):
        store.create(
            _store_record(
                transaction_id="install-follow-up",
                plugin_id="demoplugin",
            )
        )

    assert store.get(existing.transaction_id).phase is phase
    assert store.delete(
        existing.transaction_id,
        expected_phase=phase,
    ) is True
    assert store.create(
        _store_record(
            transaction_id="install-follow-up",
            plugin_id="demoplugin",
        )
    ).transaction_id == "install-follow-up"


def test_store_commits_membership_identity_and_phase_atomically(installation_store) -> None:
    """membership、身份和 journal phase 必须在一个配置原子事务中提交。"""
    _, factory, store = installation_store
    before = _identity()
    target = replace(
        before,
        declared_version="2.0.0",
        revision=2,
        updated_at=NOW.replace(second=1),
        payload_applied_at=NOW.replace(second=1),
    )
    _set_config(factory, ["OtherPlugin"])
    with factory() as session:
        session.add(_identity_model(before))
        session.commit()

    store.create(
        _store_record(
            transaction_id="install-atomic",
            identity_before_revision=before.revision,
        )
    )
    staged = store.set_target(
        "install-atomic",
        membership_target=True,
        identity_target=target,
        expected_phase=PluginInstallationPhase.PREPARED,
    )
    assert staged.identity_target_revision == target.revision

    committed = store.commit_target(
        "install-atomic",
        identity_target=target,
        expected_phase=PluginInstallationPhase.PREPARED,
    )

    assert committed.phase is PluginInstallationPhase.COMMITTED
    assert _get_config(factory) == ["OtherPlugin", "DemoPlugin"]
    with factory() as session:
        identity = session.execute(
            sa.select(PluginIdentityModel).where(
                PluginIdentityModel.normalized_plugin_id == "demoplugin"
            )
        ).scalar_one()
        assert identity.revision == 2


def test_store_preserves_other_plugin_membership(installation_store) -> None:
    """目标插件提交不能用旧完整清单覆盖其他插件。"""
    _, factory, store = installation_store
    _set_config(factory, ["OtherPlugin"])
    store.create(_store_record(transaction_id="install-narrow"))
    store.set_target(
        "install-narrow",
        membership_target=True,
        identity_target=None,
        expected_phase=PluginInstallationPhase.PREPARED,
    )

    _set_config(factory, ["OtherPlugin", "AnotherPlugin"])
    committed = store.commit_target(
        "install-narrow",
        identity_target=None,
        expected_phase=PluginInstallationPhase.PREPARED,
    )

    assert committed.phase is PluginInstallationPhase.COMMITTED
    assert _get_config(factory) == ["OtherPlugin", "AnotherPlugin", "DemoPlugin"]


def test_store_rejects_target_identity_revision_jump(installation_store) -> None:
    """最终写者必须拒绝跳号 revision，避免绕过后续来源 CAS。"""
    _, factory, store = installation_store
    before = _identity()
    with factory() as session:
        session.add(_identity_model(before))
        session.commit()
    store.create(
        _store_record(
            transaction_id="install-revision-jump",
            identity_before_revision=before.revision,
        )
    )
    jumped = replace(
        before,
        revision=before.revision + 2,
        updated_at=NOW.replace(second=1),
    )

    with pytest.raises(PluginInstallationConflictError, match="必须为 2"):
        store.set_target(
            "install-revision-jump",
            membership_target=True,
            identity_target=jumped,
            expected_phase=PluginInstallationPhase.PREPARED,
        )

    assert store.get("install-revision-jump").identity_target_revision is None


def test_store_rejects_membership_and_identity_cas_drift(installation_store) -> None:
    """同一插件 membership 或 identity revision 漂移时拒绝覆盖。"""
    _, factory, store = installation_store
    before = _identity()
    with factory() as session:
        session.add(_identity_model(before))
        session.commit()
    store.create(
        _store_record(
            transaction_id="install-drift",
            identity_before_revision=before.revision,
        )
    )
    target = replace(before, revision=2, updated_at=NOW.replace(second=1))
    store.set_target(
        "install-drift",
        membership_target=True,
        identity_target=target,
        expected_phase=PluginInstallationPhase.PREPARED,
    )

    _set_config(factory, ["DemoPlugin"])
    with pytest.raises(PluginInstallationConflictError, match="membership"):
        store.commit_target(
            "install-drift",
            identity_target=target,
            expected_phase=PluginInstallationPhase.PREPARED,
        )
    assert store.get("install-drift").phase is PluginInstallationPhase.PREPARED

    _set_config(factory, [])
    _set_identity_revision(factory, 3)
    with pytest.raises(PluginInstallationConflictError, match="revision"):
        store.commit_target(
            "install-drift",
            identity_target=target,
            expected_phase=PluginInstallationPhase.PREPARED,
        )


def _commit_or_conflict(store, transaction_id: str) -> str:
    """把 phase CAS 竞争转换为可断言的测试结果。"""
    try:
        store.commit_target(
            transaction_id,
            identity_target=None,
            expected_phase=PluginInstallationPhase.PREPARED,
        )
    except PluginInstallationConflictError:
        return "conflict"
    return "committed"


def test_store_serializes_membership_commits_and_phase_cas(installation_store) -> None:
    """SQLite 下不同插件并发提交应合并，重复提交同一事务只能失败。"""
    _, factory, store = installation_store
    first = _store_record(transaction_id="install-first")
    second = _store_record(transaction_id="install-second", plugin_id="OtherPlugin")
    store.create(first)
    store.create(second)
    store.set_target(
        first.transaction_id,
        membership_target=True,
        identity_target=None,
        expected_phase=PluginInstallationPhase.PREPARED,
    )
    store.set_target(
        second.transaction_id,
        membership_target=True,
        identity_target=None,
        expected_phase=PluginInstallationPhase.PREPARED,
    )

    def commit(record_id: str):
        return store.commit_target(
            record_id,
            identity_target=None,
            expected_phase=PluginInstallationPhase.PREPARED,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(commit, [first.transaction_id, second.transaction_id])
        )
    assert {result.phase for result in results} == {
        PluginInstallationPhase.COMMITTED,
    }
    assert set(_get_config(factory) or []) == {"DemoPlugin", "OtherPlugin"}

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda _: _commit_or_conflict(store, first.transaction_id),
                range(2),
            )
        )
    assert outcomes == ["conflict", "conflict"]


def test_store_delete_is_idempotent_after_recovery(installation_store) -> None:
    """恢复处理重复清理同一 journal 时不产生第二次副作用。"""
    _, _, store = installation_store
    store.create(_store_record(transaction_id="install-delete"))

    assert store.delete(
        "install-delete",
        expected_phase=PluginInstallationPhase.PREPARED,
    ) is True
    assert store.delete(
        "install-delete",
        expected_phase=PluginInstallationPhase.PREPARED,
    ) is False


@pytest.fixture
def postgresql_installation_stores():
    """创建两个不共享进程锁的 PostgreSQL Store，验证数据库并发合同。"""
    prefix = "MOVIEPILOT_TEST_POSTGRESQL_"
    host = os.getenv(f"{prefix}HOST")
    database = os.getenv(f"{prefix}DATABASE")
    username = os.getenv(f"{prefix}USERNAME")
    if not host or not database or not username:
        pytest.skip("未配置隔离 PostgreSQL transaction 测试库")

    port = os.getenv(f"{prefix}PORT", "5432")
    password = os.getenv(f"{prefix}PASSWORD", "")
    schema = f"plugin_transaction_{uuid.uuid4().hex}"
    with postgres_driver.connect(
        host=host,
        port=port,
        dbname=database,
        user=username,
        password=password,
    ) as connection:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
            )

    engine = sa.create_engine(
        sa.URL.create(
            POSTGRESQL_DIALECT,
            username=username,
            password=password,
            host=host,
            port=int(port),
            database=database,
        ),
        connect_args={"options": f"-csearch_path={schema}"},
    )
    SystemConfig.__table__.create(engine)
    with engine.begin() as connection:
        _upgrade_migration(
            connection,
            "database.versions.d2e4f6a8b0c1_3_0_9",
        )
        _upgrade_migration(
            connection,
            "database.versions.e4f7a1b2c3d5_3_0_10",
        )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    _set_config(factory, [])
    first = TransactionalPluginInstallationStore(
        factory,
        _AtomicSystemConfig(factory).update_atomically,
    )
    second = TransactionalPluginInstallationStore(
        factory,
        _AtomicSystemConfig(factory).update_atomically,
    )
    try:
        yield factory, first, second
    finally:
        engine.dispose()
        with postgres_driver.connect(
            host=host,
            port=port,
            dbname=database,
            user=username,
            password=password,
        ) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        sql.Identifier(schema)
                    )
                )


def test_postgresql_store_serializes_membership_phase_and_revision_cas(
    postgresql_installation_stores,
) -> None:
    """PostgreSQL 行锁必须合并不同插件写入并拒绝 phase/revision 竞争。"""
    factory, first_store, second_store = postgresql_installation_stores
    first = _store_record(transaction_id="postgres-first")
    second = _store_record(
        transaction_id="postgres-second",
        plugin_id="OtherPlugin",
    )
    for store, record in ((first_store, first), (second_store, second)):
        store.create(record)
        store.set_target(
            record.transaction_id,
            membership_target=True,
            identity_target=None,
            expected_phase=PluginInstallationPhase.PREPARED,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda item: item[0].commit_target(
                    item[1].transaction_id,
                    identity_target=None,
                    expected_phase=PluginInstallationPhase.PREPARED,
                ),
                ((first_store, first), (second_store, second)),
            )
        )

    assert {result.phase for result in results} == {
        PluginInstallationPhase.COMMITTED,
    }
    assert set(_get_config(factory) or []) == {"DemoPlugin", "OtherPlugin"}

    race = _store_record(
        transaction_id="postgres-phase-race",
        plugin_id="RacePlugin",
    )
    first_store.create(race)
    first_store.set_target(
        race.transaction_id,
        membership_target=True,
        identity_target=None,
        expected_phase=PluginInstallationPhase.PREPARED,
    )
    barrier = threading.Barrier(2)

    def commit_race(store) -> str:
        barrier.wait()
        return _commit_or_conflict(store, race.transaction_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(commit_race, (first_store, second_store)))
    assert sorted(outcomes) == ["committed", "conflict"]

    before = _identity(plugin_id="RevisionPlugin")
    with factory() as session:
        session.add(_identity_model(before))
        session.commit()
    revision = _store_record(
        transaction_id="postgres-revision",
        plugin_id=before.plugin_id,
        identity_before_revision=before.revision,
    )
    first_store.create(revision)
    target = replace(
        before,
        revision=2,
        updated_at=NOW.replace(second=1),
    )
    first_store.set_target(
        revision.transaction_id,
        membership_target=True,
        identity_target=target,
        expected_phase=PluginInstallationPhase.PREPARED,
    )
    _set_identity_revision(factory, 3, plugin_id=before.plugin_id)

    with pytest.raises(PluginInstallationConflictError, match="revision"):
        second_store.commit_target(
            revision.transaction_id,
            identity_target=target,
            expected_phase=PluginInstallationPhase.PREPARED,
        )
