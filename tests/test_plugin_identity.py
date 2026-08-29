"""插件身份事实、条件写和存量迁移决策测试。"""

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.application.plugin.declaration import PluginDeclaredMetadata
from app.application.plugin.identity import (
    PluginBindingBasis,
    PluginIdentity,
    PluginIdentityConflictError,
    PluginMarketAvailability,
    PluginPayloadSourceType,
    PluginSourceCandidate,
    TrustedPluginSourceType,
    WritePluginIdentityCommand,
    plan_legacy_plugin_identity,
)
from app.db.adapters.pluginidentity import TransactionalPluginIdentityStore
from app.db.models import load_all_models
from app.db.models.pluginidentity import PluginIdentity as PluginIdentityModel

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
OFFICIAL_SOURCE = "github:jxxghp/moviepilot-plugins"
THIRD_PARTY_SOURCE = "github:example/moviepilot-plugins"


def _identity(
    plugin_id: str = "DemoPlugin",
    *,
    declared_version: str | None = None,
) -> PluginIdentity:
    """构造一份已从官方仓成功安装的物理插件身份。"""
    installed_version = declared_version or "1.0.0"
    return PluginIdentity(
        plugin_id=plugin_id,
        normalized_plugin_id=plugin_id.lower(),
        trusted_source_type=TrustedPluginSourceType.OFFICIAL,
        trusted_source_key=OFFICIAL_SOURCE,
        binding_basis=PluginBindingBasis.OFFICIAL_DEFAULT,
        payload_source_type=PluginPayloadSourceType.OFFICIAL,
        payload_source_key=OFFICIAL_SOURCE,
        declared_version=installed_version,
        package_generation="v3",
        declared_metadata=PluginDeclaredMetadata.from_package(
            {
                "name": "Demo",
                "description": "Demo plugin",
                "v3": True,
                "v3t": False,
                "release": True,
            },
            declaration_version=installed_version,
            manifest_matches_payload=True,
        ),
        payload_receipt="sha256:" + "0" * 64,
        revision=1,
        created_at=NOW,
        updated_at=NOW,
        bound_at=NOW,
        payload_applied_at=NOW,
    )


@pytest.fixture
def identity_store(tmp_path):
    """创建可跨线程竞争的独立 SQLite 身份表。"""
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'plugin-identity.db'}",
        connect_args={"check_same_thread": False},
    )
    PluginIdentityModel.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    try:
        yield TransactionalPluginIdentityStore(factory)
    finally:
        engine.dispose()


def test_plugin_identity_model_is_registered_by_composition_entry() -> None:
    """组合根加载模型后必须包含插件身份表及物理 ID 唯一约束。"""
    load_all_models()

    table = PluginIdentityModel.__table__
    unique_columns = {
        tuple(constraint.columns.keys())
        for constraint in table.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    assert table.name == "pluginidentity"
    assert ("normalized_plugin_id",) in unique_columns


def test_plugin_identity_store_normalizes_reads_and_rejects_case_duplicate(
    identity_store,
) -> None:
    """同一物理目录的大小写别名只能对应数据库中的一行。"""
    created = identity_store.compare_and_set(
        _identity("DemoPlugin"),
        expected_revision=None,
    )

    assert identity_store.get("DEMOPLUGIN") == created
    with pytest.raises(PluginIdentityConflictError):
        identity_store.compare_and_set(
            _identity("demoplugin"),
            expected_revision=None,
        )


def test_plugin_identity_store_ignores_invalid_legacy_ids_on_read(
    identity_store,
) -> None:
    """历史安装清单含不合规插件 ID 时，身份读取不得阻断插件列表。"""
    created = identity_store.compare_and_set(
        _identity("DemoPlugin"),
        expected_revision=None,
    )

    assert identity_store.list(
        ["legacy-plugin", "115Cloud", "DemoPlugin", "demoplugin"]
    ) == [created]
    assert identity_store.get("legacy-plugin") is None


def test_plugin_identity_store_rejects_stale_revision(identity_store) -> None:
    """旧安装事务不能覆盖已经提交的新身份。"""
    original = identity_store.compare_and_set(
        _identity(),
        expected_revision=None,
    )
    updated = identity_store.compare_and_set(
        replace(
            original,
            declared_version="2.0.0",
            updated_at=NOW + timedelta(seconds=1),
        ),
        expected_revision=original.revision,
    )

    with pytest.raises(PluginIdentityConflictError):
        identity_store.compare_and_set(
            replace(
                original,
                declared_version="stale",
                updated_at=NOW + timedelta(seconds=2),
            ),
            expected_revision=original.revision,
        )

    assert identity_store.get("DemoPlugin") == updated
    assert updated.revision == 2


def test_plugin_identity_store_rejects_implicit_source_change(identity_store) -> None:
    """通用条件写不能代替管理员显式换源命令。"""
    original = identity_store.compare_and_set(
        _identity(),
        expected_revision=None,
    )
    changed = replace(
        original,
        trusted_source_type=TrustedPluginSourceType.THIRD_PARTY,
        trusted_source_key=THIRD_PARTY_SOURCE,
        binding_basis=PluginBindingBasis.EXPLICIT_INSTALL,
        payload_source_type=PluginPayloadSourceType.THIRD_PARTY,
        payload_source_key=THIRD_PARTY_SOURCE,
        updated_at=NOW + timedelta(seconds=1),
    )

    with pytest.raises(PluginIdentityConflictError, match="不能改变"):
        identity_store.compare_and_set(
            changed,
            expected_revision=original.revision,
        )

    assert identity_store.get("DemoPlugin") == original


def test_plugin_identity_store_allows_only_one_same_revision_writer(
    identity_store,
) -> None:
    """两个独立会话竞争同一 revision 时必须恰好一个提交成功。"""
    original = identity_store.compare_and_set(
        _identity(),
        expected_revision=None,
    )
    barrier = threading.Barrier(2)

    def update(version: str) -> str:
        """在相同起点并发提交不同版本。"""
        barrier.wait()
        try:
            identity_store.compare_and_set(
                replace(
                    original,
                    declared_version=version,
                    updated_at=NOW + timedelta(seconds=1),
                ),
                expected_revision=original.revision,
            )
            return "applied"
        except PluginIdentityConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(update, ("2.0.0", "3.0.0")))

    assert sorted(results) == ["applied", "conflict"]
    assert identity_store.get("DemoPlugin").revision == 2


def test_write_command_rolls_back_when_conditional_replace_loses_race() -> None:
    """条件更新在最终 CAS 失利时必须确定回滚并报告竞争冲突。"""
    current = _identity()
    repository = Mock()
    repository.get.return_value = current
    repository.stage_replace.return_value = False
    unit_of_work = Mock()
    command = WritePluginIdentityCommand(repository, unit_of_work)

    with pytest.raises(PluginIdentityConflictError, match="其他任务更新"):
        command.execute(
            replace(
                current,
                declared_version="2.0.0",
                updated_at=NOW + timedelta(seconds=1),
            ),
            expected_revision=current.revision,
        )

    repository.stage_replace.assert_called_once()
    unit_of_work.commit.assert_not_called()
    unit_of_work.rollback.assert_called_once()


def test_local_payload_preserves_trusted_online_binding() -> None:
    """本地开发覆盖只改变载荷事实，不得抹掉可信在线更新仓库。"""
    identity = replace(
        _identity(),
        payload_source_type=PluginPayloadSourceType.LOCAL,
        payload_source_key=None,
        declared_version="2.0.0-dev",
        package_generation="v3",
        declared_metadata=PluginDeclaredMetadata.from_package(
            {
                "name": "Demo local",
                "v3": True,
                "v3t": False,
            },
            declaration_version="2.0.0-dev",
            manifest_matches_payload=True,
        ),
        payload_receipt="sha256:" + "a" * 64,
        payload_applied_at=NOW + timedelta(seconds=1),
        updated_at=NOW + timedelta(seconds=1),
    )

    assert identity.trusted_source_key == OFFICIAL_SOURCE
    assert identity.payload_source_key is None
    assert identity.payload_source_type is PluginPayloadSourceType.LOCAL


def test_first_local_sync_has_a_nonlegacy_identity_basis() -> None:
    """首次本地同步不得被记录为未知存量插件迁移。"""
    identity = replace(
        _identity(),
        trusted_source_type=TrustedPluginSourceType.UNKNOWN,
        trusted_source_key=None,
        binding_basis=PluginBindingBasis.LOCAL_ONLY,
        payload_source_type=PluginPayloadSourceType.LOCAL,
        payload_source_key=None,
        bound_at=None,
    )

    assert identity.binding_basis is PluginBindingBasis.LOCAL_ONLY
    assert identity.trusted_source_key is None


def test_online_binding_rejects_local_only_basis() -> None:
    """已绑定在线仓库不能冒用首次本地同步的身份依据。"""
    with pytest.raises(ValueError, match="未绑定来源依据"):
        replace(_identity(), binding_basis=PluginBindingBasis.LOCAL_ONLY)


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("bound_at", NOW - timedelta(seconds=1)),
        ("bound_at", NOW + timedelta(seconds=1)),
        ("payload_applied_at", NOW - timedelta(seconds=1)),
        ("payload_applied_at", NOW + timedelta(seconds=1)),
    ),
)
def test_plugin_identity_rejects_audit_times_outside_record_lifetime(
    field_name,
    value,
) -> None:
    """来源审计时间必须落在该 revision 的创建和更新时间范围内。"""
    with pytest.raises(ValueError, match="审计时间"):
        replace(_identity(), **{field_name: value})


def test_unknown_payload_rejects_version_and_receipt_evidence() -> None:
    """存量载荷来源未知时不得保留看似经过安装确认的版本证据。"""
    with pytest.raises(ValueError, match="未知载荷不能携带"):
        replace(
            _identity(),
            payload_source_type=PluginPayloadSourceType.UNKNOWN,
            payload_source_key=None,
            payload_applied_at=None,
        )


def test_online_payload_must_match_trusted_source() -> None:
    """在线载荷来源与可信更新仓库不一致时身份必须失败关闭。"""
    with pytest.raises(ValueError, match="必须与可信更新来源一致"):
        replace(
            _identity(),
            payload_source_type=PluginPayloadSourceType.THIRD_PARTY,
            payload_source_key=THIRD_PARTY_SOURCE,
        )


@pytest.mark.parametrize(
    ("source_type", "source_key"),
    (
        (TrustedPluginSourceType.OFFICIAL, THIRD_PARTY_SOURCE),
        (TrustedPluginSourceType.THIRD_PARTY, OFFICIAL_SOURCE),
    ),
)
def test_trusted_source_type_must_match_official_repository(
    source_type,
    source_key,
) -> None:
    """官方仓库键与官方来源类型不能形成相互矛盾的信任事实。"""
    with pytest.raises(ValueError, match="官方来源类型"):
        replace(
            _identity(),
            trusted_source_type=source_type,
            trusted_source_key=source_key,
        )


@pytest.mark.parametrize(
    ("source_type", "source_key"),
    (
        (TrustedPluginSourceType.OFFICIAL, THIRD_PARTY_SOURCE),
        (TrustedPluginSourceType.THIRD_PARTY, OFFICIAL_SOURCE),
    ),
)
def test_source_candidate_type_must_match_official_repository(
    source_type,
    source_key,
) -> None:
    """迁移候选也必须服从与持久化身份相同的来源分类合同。"""
    with pytest.raises(ValueError, match="官方来源类型"):
        PluginSourceCandidate(source_type, source_key)


@pytest.mark.parametrize(
    "plugin_id",
    (" DemoPlugin", "DemoPlugin ", "A" * 129),
)
def test_plugin_identity_rejects_noncanonical_physical_id(plugin_id) -> None:
    """物理 ID 不得靠静默裁剪或数据库方言差异改变身份。"""
    with pytest.raises(ValueError, match="插件 ID"):
        _identity(plugin_id)


@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    (
        ("declared_version", "v" * 65, "插件声明版本"),
    ),
)
def test_plugin_identity_rejects_values_longer_than_database_columns(
    field_name,
    value,
    message,
) -> None:
    """SQLite 与 PostgreSQL 必须在进入持久化前共享相同长度边界。"""
    with pytest.raises(ValueError, match=message):
        replace(_identity(), **{field_name: value})


def test_legacy_official_candidate_binds_updates_but_not_payload() -> None:
    """官方默认只建立未来更新绑定，不能冒充存量载荷来源。"""
    identity = plan_legacy_plugin_identity(
        plugin_id="DemoPlugin",
        market_availability=PluginMarketAvailability.AVAILABLE,
        online_candidates=(
            PluginSourceCandidate(
                TrustedPluginSourceType.THIRD_PARTY,
                THIRD_PARTY_SOURCE,
            ),
            PluginSourceCandidate(
                TrustedPluginSourceType.OFFICIAL,
                OFFICIAL_SOURCE,
            ),
        ),
        is_virtual_instance=False,
        now=NOW,
    )

    assert identity.trusted_source_type is TrustedPluginSourceType.OFFICIAL
    assert identity.trusted_source_key == OFFICIAL_SOURCE
    assert identity.binding_basis is PluginBindingBasis.OFFICIAL_DEFAULT
    assert identity.payload_source_type is PluginPayloadSourceType.UNKNOWN
    assert identity.declared_version is None


def test_legacy_single_third_party_candidate_uses_tofu() -> None:
    """唯一第三方候选可建立一次性更新绑定，但载荷仍保持未知。"""
    identity = plan_legacy_plugin_identity(
        plugin_id="DemoPlugin",
        market_availability=PluginMarketAvailability.AVAILABLE,
        online_candidates=(
            PluginSourceCandidate(
                TrustedPluginSourceType.THIRD_PARTY,
                THIRD_PARTY_SOURCE,
            ),
        ),
        is_virtual_instance=False,
        now=NOW,
    )

    assert identity.binding_basis is PluginBindingBasis.TOFU
    assert identity.trusted_source_key == THIRD_PARTY_SOURCE
    assert identity.payload_source_type is PluginPayloadSourceType.UNKNOWN


@pytest.mark.parametrize(
    ("availability", "candidates"),
    (
        (PluginMarketAvailability.UNAVAILABLE, ()),
        (PluginMarketAvailability.AVAILABLE, ()),
        (
            PluginMarketAvailability.AVAILABLE,
            (
                PluginSourceCandidate(
                    TrustedPluginSourceType.THIRD_PARTY,
                    "github:first/plugins",
                ),
                PluginSourceCandidate(
                    TrustedPluginSourceType.THIRD_PARTY,
                    "github:second/plugins",
                ),
            ),
        ),
    ),
)
def test_legacy_unavailable_empty_or_ambiguous_market_stays_unbound(
    availability,
    candidates,
) -> None:
    """离线、无候选和多候选必须保持可区分输入，但均不得猜测绑定。"""
    identity = plan_legacy_plugin_identity(
        plugin_id="DemoPlugin",
        market_availability=availability,
        online_candidates=candidates,
        is_virtual_instance=False,
        now=NOW,
    )

    assert identity.trusted_source_type is TrustedPluginSourceType.UNKNOWN
    assert identity.trusted_source_key is None
    assert identity.binding_basis is PluginBindingBasis.LEGACY_UNBOUND


def test_virtual_instance_does_not_create_independent_plugin_identity() -> None:
    """有效 PluginInstances 派生实例只继承物理宿主身份。"""
    assert plan_legacy_plugin_identity(
        plugin_id="DemoPluginWork",
        market_availability=PluginMarketAvailability.AVAILABLE,
        online_candidates=(
            PluginSourceCandidate(
                TrustedPluginSourceType.OFFICIAL,
                OFFICIAL_SOURCE,
            ),
        ),
        is_virtual_instance=True,
        now=NOW,
    ) is None
