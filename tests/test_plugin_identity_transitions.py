"""插件来源身份专用转换命令的 CAS 合同测试。"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.application.plugin.identity import (
    PluginBindingBasis,
    PluginIdentity,
    PluginIdentityConflictError,
    PluginPayloadSourceType,
    TrustedPluginSourceType,
)
from app.db.adapters.pluginidentity import TransactionalPluginIdentityStore
from app.db.models.pluginidentity import PluginIdentity as PluginIdentityModel
from app.db.uow import SqlAlchemyUnitOfWork

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
OFFICIAL_SOURCE = "github:jxxghp/moviepilot-plugins"
THIRD_PARTY_SOURCE = "github:example/moviepilot-plugins"


def _identity(
    plugin_id: str = "DemoPlugin",
    *,
    trusted_source_type: TrustedPluginSourceType = TrustedPluginSourceType.OFFICIAL,
    trusted_source_key: str | None = OFFICIAL_SOURCE,
    binding_basis: PluginBindingBasis = PluginBindingBasis.OFFICIAL_DEFAULT,
    payload_source_type: PluginPayloadSourceType = PluginPayloadSourceType.OFFICIAL,
    payload_source_key: str | None = OFFICIAL_SOURCE,
) -> PluginIdentity:
    """构造一份带完整在线载荷审计事实的插件身份。"""
    return PluginIdentity(
        plugin_id=plugin_id,
        normalized_plugin_id=plugin_id.lower(),
        trusted_source_type=trusted_source_type,
        trusted_source_key=trusted_source_key,
        binding_basis=binding_basis,
        payload_source_type=payload_source_type,
        payload_source_key=payload_source_key,
        declared_version="1.0.0",
        package_generation="v3",
        system_version=None,
        supports_v3=None,
        supports_v3t=None,
        payload_receipt="sha256:" + "0" * 64,
        revision=1,
        created_at=NOW,
        updated_at=NOW,
        bound_at=NOW if trusted_source_type is not TrustedPluginSourceType.UNKNOWN else None,
        payload_applied_at=NOW,
    )


@pytest.fixture
def identity_store(tmp_path):
    """创建可验证事务回滚和 revision CAS 的独立 SQLite 身份表。"""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'plugin-identity.db'}")
    PluginIdentityModel.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    try:
        yield TransactionalPluginIdentityStore(factory)
    finally:
        engine.dispose()


def _third_party_target(identity: PluginIdentity) -> PluginIdentity:
    """构造一次明确指向第三方在线仓库的换源目标。"""
    return replace(
        identity,
        trusted_source_type=TrustedPluginSourceType.THIRD_PARTY,
        trusted_source_key=THIRD_PARTY_SOURCE,
        binding_basis=PluginBindingBasis.EXPLICIT_SOURCE_CHANGE,
        payload_source_type=PluginPayloadSourceType.THIRD_PARTY,
        payload_source_key=THIRD_PARTY_SOURCE,
        declared_version="2.0.0",
        updated_at=NOW + timedelta(seconds=1),
        bound_at=NOW + timedelta(seconds=1),
        payload_applied_at=NOW + timedelta(seconds=1),
    )


def _legacy_identity(plugin_id: str = "DemoPlugin") -> PluginIdentity:
    """构造尚未建立可信来源且没有已知载荷的存量身份。"""
    return PluginIdentity(
        plugin_id=plugin_id,
        normalized_plugin_id=plugin_id.lower(),
        trusted_source_type=TrustedPluginSourceType.UNKNOWN,
        trusted_source_key=None,
        binding_basis=PluginBindingBasis.LEGACY_UNBOUND,
        payload_source_type=PluginPayloadSourceType.UNKNOWN,
        payload_source_key=None,
        declared_version=None,
        package_generation=None,
        system_version=None,
        supports_v3=None,
        supports_v3t=None,
        payload_receipt=None,
        revision=1,
        created_at=NOW,
        updated_at=NOW,
        bound_at=None,
        payload_applied_at=None,
    )


def _online_binding_target(
    identity: PluginIdentity,
    *,
    source_type: TrustedPluginSourceType = TrustedPluginSourceType.THIRD_PARTY,
    source_key: str = THIRD_PARTY_SOURCE,
    updated_at: datetime = NOW + timedelta(seconds=1),
) -> PluginIdentity:
    """构造用户明确选定在线仓库后的首次绑定目标。"""
    return replace(
        identity,
        trusted_source_type=source_type,
        trusted_source_key=source_key,
        binding_basis=PluginBindingBasis.EXPLICIT_INSTALL,
        payload_source_type=PluginPayloadSourceType(source_type.value),
        payload_source_key=source_key,
        declared_version="2.0.0",
        package_generation="v3",
        payload_receipt="sha256:" + "2" * 64,
        updated_at=updated_at,
        bound_at=updated_at,
        payload_applied_at=updated_at,
    )


def test_change_source_commits_explicit_online_transition(identity_store) -> None:
    """显式换源必须保留创建时间并只推进一个 revision。"""
    original = identity_store.compare_and_set(_identity(), expected_revision=None)

    changed = identity_store.change_source(
        _third_party_target(original),
        expected_revision=original.revision,
    )

    assert changed.trusted_source_type is TrustedPluginSourceType.THIRD_PARTY
    assert changed.trusted_source_key == THIRD_PARTY_SOURCE
    assert changed.payload_source_type is PluginPayloadSourceType.THIRD_PARTY
    assert changed.payload_source_key == THIRD_PARTY_SOURCE
    assert changed.binding_basis is PluginBindingBasis.EXPLICIT_SOURCE_CHANGE
    assert changed.created_at == original.created_at
    assert changed.revision == original.revision + 1
    assert identity_store.get(original.plugin_id) == changed


def test_change_source_rejects_revision_competition(identity_store) -> None:
    """换源目标使用旧 revision 时不能覆盖已经提交的身份。"""
    original = identity_store.compare_and_set(_identity(), expected_revision=None)
    changed = identity_store.change_source(
        _third_party_target(original),
        expected_revision=original.revision,
    )

    stale_target = replace(
        _third_party_target(original),
        trusted_source_key=OFFICIAL_SOURCE,
        trusted_source_type=TrustedPluginSourceType.OFFICIAL,
        payload_source_key=OFFICIAL_SOURCE,
        payload_source_type=PluginPayloadSourceType.OFFICIAL,
        updated_at=NOW + timedelta(seconds=2),
        bound_at=NOW + timedelta(seconds=2),
        payload_applied_at=NOW + timedelta(seconds=2),
    )
    with pytest.raises(PluginIdentityConflictError, match="revision"):
        identity_store.change_source(stale_target, expected_revision=original.revision)

    assert identity_store.get(original.plugin_id) == changed


def test_change_source_rejects_same_source_and_local_payload(identity_store) -> None:
    """换源必须改变实际在线来源，且不能以本地载荷冒充在线换源。"""
    original = identity_store.compare_and_set(_identity(), expected_revision=None)

    same_source = replace(
        original,
        binding_basis=PluginBindingBasis.EXPLICIT_SOURCE_CHANGE,
        updated_at=NOW + timedelta(seconds=1),
        bound_at=NOW + timedelta(seconds=1),
        payload_applied_at=NOW + timedelta(seconds=1),
    )
    with pytest.raises(PluginIdentityConflictError, match="来源必须变化"):
        identity_store.change_source(
            same_source,
            expected_revision=original.revision,
        )

    local_payload = replace(
        _third_party_target(original),
        payload_source_type=PluginPayloadSourceType.LOCAL,
        payload_source_key=None,
    )
    with pytest.raises(PluginIdentityConflictError, match="在线载荷"):
        identity_store.change_source(
            local_payload,
            expected_revision=original.revision,
        )
    assert identity_store.get(original.plugin_id) == original


def test_bind_local_commits_only_legacy_unbound_transition(identity_store) -> None:
    """本地绑定只能把存量未绑定行转换为本地专属身份。"""
    legacy = _legacy_identity()
    original = identity_store.compare_and_set(legacy, expected_revision=None)
    local = replace(
        original,
        binding_basis=PluginBindingBasis.LOCAL_ONLY,
        payload_source_type=PluginPayloadSourceType.LOCAL,
        declared_version="2.0.0-dev",
        package_generation="v3",
        payload_receipt="sha256:" + "1" * 64,
        updated_at=NOW + timedelta(seconds=1),
        payload_applied_at=NOW + timedelta(seconds=1),
    )

    changed = identity_store.bind_local(
        local,
        expected_revision=original.revision,
    )

    assert changed.trusted_source_type is TrustedPluginSourceType.UNKNOWN
    assert changed.binding_basis is PluginBindingBasis.LOCAL_ONLY
    assert changed.payload_source_type is PluginPayloadSourceType.LOCAL
    assert changed.created_at == original.created_at
    assert changed.revision == 2
    assert identity_store.get(original.plugin_id) == changed


def test_bind_online_commits_legacy_and_local_first_bindings(identity_store) -> None:
    """显式在线安装可绑定存量未知来源，也可承接先本地开发的插件。"""
    legacy = identity_store.compare_and_set(
        _legacy_identity("LegacyPlugin"),
        expected_revision=None,
    )
    legacy_bound = identity_store.bind_online(
        _online_binding_target(legacy),
        expected_revision=legacy.revision,
    )

    local = replace(
        _legacy_identity("LocalPlugin"),
        binding_basis=PluginBindingBasis.LOCAL_ONLY,
        payload_source_type=PluginPayloadSourceType.LOCAL,
        declared_version="2.0.0-dev",
        package_generation="v3",
        payload_receipt="sha256:" + "1" * 64,
        updated_at=NOW + timedelta(seconds=1),
        payload_applied_at=NOW + timedelta(seconds=1),
    )
    local = identity_store.compare_and_set(local, expected_revision=None)
    local_bound = identity_store.bind_online(
        _online_binding_target(
            local,
            source_type=TrustedPluginSourceType.OFFICIAL,
            source_key=OFFICIAL_SOURCE,
            updated_at=NOW + timedelta(seconds=2),
        ),
        expected_revision=local.revision,
    )

    assert legacy_bound.trusted_source_key == THIRD_PARTY_SOURCE
    assert legacy_bound.binding_basis is PluginBindingBasis.EXPLICIT_INSTALL
    assert legacy_bound.revision == 2
    assert local_bound.trusted_source_key == OFFICIAL_SOURCE
    assert local_bound.payload_source_type is PluginPayloadSourceType.OFFICIAL
    assert local_bound.binding_basis is PluginBindingBasis.EXPLICIT_INSTALL
    assert local_bound.revision == 2


def test_bind_online_rejects_bound_identity_and_stale_revision(identity_store) -> None:
    """首次在线绑定不能覆盖已有可信来源，也不能使用失效 revision。"""
    bound = identity_store.compare_and_set(_identity(), expected_revision=None)
    with pytest.raises(PluginIdentityConflictError, match="未绑定"):
        identity_store.bind_online(
            _third_party_target(bound),
            expected_revision=bound.revision,
        )

    legacy = identity_store.compare_and_set(
        _legacy_identity("StalePlugin"),
        expected_revision=None,
    )
    target = _online_binding_target(legacy)
    identity_store.bind_online(target, expected_revision=legacy.revision)
    with pytest.raises(PluginIdentityConflictError, match="revision"):
        identity_store.bind_online(target, expected_revision=legacy.revision)


def test_first_local_install_still_uses_ordinary_create(identity_store) -> None:
    """未安装插件的首次本地载荷仍可由普通 create 建立身份。"""
    local = replace(
        _identity(),
        trusted_source_type=TrustedPluginSourceType.UNKNOWN,
        trusted_source_key=None,
        binding_basis=PluginBindingBasis.LOCAL_ONLY,
        payload_source_type=PluginPayloadSourceType.LOCAL,
        payload_source_key=None,
        declared_version="2.0.0-dev",
        payload_receipt="sha256:" + "1" * 64,
        bound_at=None,
    )

    created = identity_store.compare_and_set(local, expected_revision=None)

    assert created.binding_basis is PluginBindingBasis.LOCAL_ONLY
    assert created.payload_source_type is PluginPayloadSourceType.LOCAL
    assert created.revision == 1


def test_bind_local_rejects_nonlegacy_state_and_stale_revision(identity_store) -> None:
    """本地绑定不能绕过已绑定身份或 revision 条件。"""
    original = identity_store.compare_and_set(_identity(), expected_revision=None)
    local = replace(
        original,
        trusted_source_type=TrustedPluginSourceType.UNKNOWN,
        trusted_source_key=None,
        binding_basis=PluginBindingBasis.LOCAL_ONLY,
        payload_source_type=PluginPayloadSourceType.LOCAL,
        payload_source_key=None,
        bound_at=None,
        declared_version="2.0.0-dev",
        payload_receipt="sha256:" + "1" * 64,
        updated_at=NOW + timedelta(seconds=1),
        payload_applied_at=NOW + timedelta(seconds=1),
    )
    with pytest.raises(PluginIdentityConflictError, match="legacy_unbound"):
        identity_store.bind_local(local, expected_revision=original.revision)

    legacy = replace(
        _identity("LegacyPlugin"),
        trusted_source_type=TrustedPluginSourceType.UNKNOWN,
        trusted_source_key=None,
        binding_basis=PluginBindingBasis.LEGACY_UNBOUND,
        payload_source_type=PluginPayloadSourceType.UNKNOWN,
        payload_source_key=None,
        declared_version=None,
        package_generation=None,
        payload_receipt=None,
        bound_at=None,
        payload_applied_at=None,
    )
    identity_store.compare_and_set(legacy, expected_revision=None)
    changed = replace(
        legacy,
        binding_basis=PluginBindingBasis.LOCAL_ONLY,
        payload_source_type=PluginPayloadSourceType.LOCAL,
        declared_version="2.0.0-dev",
        package_generation="v3",
        payload_receipt="sha256:" + "1" * 64,
        updated_at=NOW + timedelta(seconds=1),
        payload_applied_at=NOW + timedelta(seconds=1),
    )
    identity_store.bind_local(changed, expected_revision=1)
    with pytest.raises(PluginIdentityConflictError, match="revision"):
        identity_store.bind_local(changed, expected_revision=1)


def test_ordinary_writer_still_rejects_binding_change(identity_store) -> None:
    """普通 writer 不能借 CAS 参数伪装成来源绑定转换。"""
    original = identity_store.compare_and_set(_identity(), expected_revision=None)
    with pytest.raises(PluginIdentityConflictError, match="不能改变"):
        identity_store.compare_and_set(
            _third_party_target(original),
            expected_revision=original.revision,
        )
    assert identity_store.get(original.plugin_id) == original


def test_transition_rolls_back_when_commit_fails(identity_store, monkeypatch) -> None:
    """转换提交失败时必须回滚暂存的身份变化。"""
    original = identity_store.compare_and_set(_identity(), expected_revision=None)
    target = _third_party_target(original)

    def fail_commit(_unit_of_work: SqlAlchemyUnitOfWork) -> None:
        """模拟数据库提交失败。"""
        raise RuntimeError("commit failed")

    monkeypatch.setattr(SqlAlchemyUnitOfWork, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="commit failed"):
        identity_store.change_source(target, expected_revision=original.revision)

    assert identity_store.get(original.plugin_id) == original
