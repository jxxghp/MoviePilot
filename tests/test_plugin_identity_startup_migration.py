"""存量插件身份启动迁移的来源和顺序合同测试。"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.plugin.identity import (
    PluginBindingBasis,
    PluginIdentity,
    PluginIdentityConflictError,
    PluginPayloadSourceType,
    TrustedPluginSourceType,
)
from app.application.plugin.identity_migration import PluginIdentityMigrationService
from app.application.plugin.source import (
    CandidateInventory,
    LocalCandidateRead,
    MarketRead,
    PluginMarketCandidate,
)
from app.runtime.extensions.plugin.dependency import PluginDependencyInstallResult
from app.startup.initializers import plugins as plugins_initializer

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
OFFICIAL_REPO = "https://github.com/jxxghp/MoviePilot-Plugins"
OFFICIAL_SOURCE = "github:jxxghp/moviepilot-plugins"
THIRD_PARTY_REPO = "https://github.com/example/MoviePilot-Plugins"
THIRD_PARTY_SOURCE = "github:example/moviepilot-plugins"


class _Persistence:
    """提供可观察 CAS 竞争的内存迁移持久化端口。"""

    def __init__(self, identities: tuple[PluginIdentity, ...] = ()) -> None:
        self.identities = {
            identity.normalized_plugin_id: identity for identity in identities
        }
        self.fail_create = False
        self.fail_bind = False

    async def get_identity(self, plugin_id: str) -> PluginIdentity | None:
        """按规范物理 ID 返回当前身份。"""
        return self.identities.get(plugin_id.lower())

    async def migrate_identity(
        self,
        identity: PluginIdentity,
        *,
        expected_revision: int | None,
    ) -> PluginIdentity:
        """模拟首次身份 CAS。"""
        assert expected_revision is None
        if self.fail_create or identity.normalized_plugin_id in self.identities:
            raise PluginIdentityConflictError("create conflict")
        self.identities[identity.normalized_plugin_id] = identity
        return identity

    async def bind_online_identity(
        self,
        identity: PluginIdentity,
        *,
        expected_revision: int,
    ) -> PluginIdentity:
        """模拟未绑定身份的 revision CAS。"""
        current = self.identities.get(identity.normalized_plugin_id)
        if (
            self.fail_bind
            or current is None
            or current.revision != expected_revision
        ):
            raise PluginIdentityConflictError("bind conflict")
        self.identities[identity.normalized_plugin_id] = identity
        return identity


def _candidate(
    plugin_id: str,
    *,
    source_type: TrustedPluginSourceType,
    source_key: str,
    repo_url: str,
) -> PluginMarketCandidate:
    """构造一个 V3 在线候选。"""
    return PluginMarketCandidate(
        plugin_id=plugin_id,
        source_key=source_key,
        source_type=source_type,
        repo_url=repo_url,
        package_generation="v3",
        plugin_version="1.0.0",
    )


def _inventory(
    *candidates: PluginMarketCandidate,
    failed_market: bool = False,
) -> CandidateInventory:
    """构造完整或部分失败的市场库存。"""
    reads = [
        MarketRead.present(
            OFFICIAL_REPO,
            candidates,
            package_generation="v3",
        )
    ]
    expected_markets = [OFFICIAL_REPO]
    if failed_market:
        failed_repo = "https://github.com/unavailable/MoviePilot-Plugins"
        reads.append(
            MarketRead.failure(
                failed_repo,
                "unavailable",
                package_generation="v3",
            )
        )
        expected_markets.append(failed_repo)
    return CandidateInventory(
        market_reads=tuple(reads),
        expected_markets=tuple(expected_markets),
        expected_generations=("v3",),
        local_read=LocalCandidateRead.absent(),
    )


def _legacy(plugin_id: str = "DemoPlugin") -> PluginIdentity:
    """构造尚未绑定在线来源的存量身份。"""
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


def _service(
    persistence: _Persistence,
    inventory: CandidateInventory,
    installed: list[str],
    *,
    virtual: set[str] | None = None,
) -> PluginIdentityMigrationService:
    """装配固定库存和安装清单的迁移服务。"""
    virtual_ids = virtual or set()
    return PluginIdentityMigrationService(
        persistence=persistence,
        inventory=AsyncMock(return_value=inventory),
        installed_plugins=lambda: installed,
        is_virtual_instance=lambda plugin_id: plugin_id in virtual_ids,
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_migration_binds_official_and_unique_third_party_sources() -> None:
    """官方默认和完整库存中的唯一第三方来源都可建立更新绑定。"""
    persistence = _Persistence()
    inventory = _inventory(
        _candidate(
            "OfficialPlugin",
            source_type=TrustedPluginSourceType.OFFICIAL,
            source_key=OFFICIAL_SOURCE,
            repo_url=OFFICIAL_REPO,
        ),
        _candidate(
            "ThirdPartyPlugin",
            source_type=TrustedPluginSourceType.THIRD_PARTY,
            source_key=THIRD_PARTY_SOURCE,
            repo_url=THIRD_PARTY_REPO,
        ),
    )

    result = await _service(
        persistence,
        inventory,
        ["OfficialPlugin", "ThirdPartyPlugin", "VirtualPlugin"],
        virtual={"VirtualPlugin"},
    ).migrate()

    assert result.created == 2
    assert result.bound == 2
    assert result.unbound == 0
    assert result.skipped == 1
    official = persistence.identities["officialplugin"]
    third_party = persistence.identities["thirdpartyplugin"]
    assert official.binding_basis is PluginBindingBasis.OFFICIAL_DEFAULT
    assert official.payload_source_type is PluginPayloadSourceType.UNKNOWN
    assert third_party.binding_basis is PluginBindingBasis.TOFU
    assert third_party.payload_source_type is PluginPayloadSourceType.UNKNOWN


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_market", (False, True))
async def test_migration_keeps_ambiguous_or_incomplete_third_party_unbound(
    failed_market: bool,
) -> None:
    """多来源或库存读取失败时不得猜测第三方更新来源。"""
    candidates = (
        _candidate(
            "DemoPlugin",
            source_type=TrustedPluginSourceType.THIRD_PARTY,
            source_key=THIRD_PARTY_SOURCE,
            repo_url=THIRD_PARTY_REPO,
        ),
    )
    if not failed_market:
        candidates += (
            _candidate(
                "DemoPlugin",
                source_type=TrustedPluginSourceType.THIRD_PARTY,
                source_key="github:second/moviepilot-plugins",
                repo_url="https://github.com/second/MoviePilot-Plugins",
            ),
        )
    persistence = _Persistence()

    result = await _service(
        persistence,
        _inventory(*candidates, failed_market=failed_market),
        ["DemoPlugin"],
    ).migrate()

    assert result.created == 1
    assert result.bound == 0
    assert result.unbound == 1
    identity = persistence.identities["demoplugin"]
    assert identity.binding_basis is PluginBindingBasis.LEGACY_UNBOUND
    assert identity.trusted_source_key is None


@pytest.mark.asyncio
async def test_migration_later_binds_legacy_identity_without_rewriting_payload() -> None:
    """后续市场证据充分时只升级可信来源，不改写未知存量载荷。"""
    legacy = _legacy("DemoPlugin")
    persistence = _Persistence((legacy,))
    inventory = _inventory(
        _candidate(
            "demoplugin",
            source_type=TrustedPluginSourceType.THIRD_PARTY,
            source_key=THIRD_PARTY_SOURCE,
            repo_url=THIRD_PARTY_REPO,
        )
    )

    result = await _service(
        persistence,
        inventory,
        ["demoplugin"],
    ).migrate()

    assert result.bound == 1
    identity = persistence.identities["demoplugin"]
    assert identity.plugin_id == "DemoPlugin"
    assert identity.created_at == legacy.created_at
    assert identity.revision == 2
    assert identity.binding_basis is PluginBindingBasis.TOFU
    assert identity.payload_source_type is PluginPayloadSourceType.UNKNOWN


@pytest.mark.asyncio
async def test_migration_accepts_concurrent_create_winner() -> None:
    """首次身份 CAS 竞争已有赢家时，迁移跳过而不覆盖最终身份。"""
    persistence = _Persistence()
    inventory = _inventory(
        _candidate(
            "DemoPlugin",
            source_type=TrustedPluginSourceType.OFFICIAL,
            source_key=OFFICIAL_SOURCE,
            repo_url=OFFICIAL_REPO,
        )
    )

    async def create_conflict(
        identity: PluginIdentity,
        *,
        expected_revision: int | None,
    ) -> PluginIdentity:
        assert expected_revision is None
        persistence.identities[identity.normalized_plugin_id] = identity
        raise PluginIdentityConflictError("concurrent create")

    persistence.migrate_identity = create_conflict  # type: ignore[method-assign]

    result = await _service(persistence, inventory, ["DemoPlugin"]).migrate()

    assert result.created == 0
    assert result.skipped == 1
    assert persistence.identities["demoplugin"].trusted_source_key == OFFICIAL_SOURCE


@pytest.mark.asyncio
async def test_migration_accepts_concurrent_bind_winner() -> None:
    """存量绑定 CAS 已由其他执行者推进时，迁移保留赢家并幂等结束。"""
    persistence = _Persistence((_legacy("DemoPlugin"),))
    inventory = _inventory(
        _candidate(
            "DemoPlugin",
            source_type=TrustedPluginSourceType.THIRD_PARTY,
            source_key=THIRD_PARTY_SOURCE,
            repo_url=THIRD_PARTY_REPO,
        )
    )

    async def bind_conflict(
        identity: PluginIdentity,
        *,
        expected_revision: int,
    ) -> PluginIdentity:
        assert expected_revision == 1
        persistence.identities[identity.normalized_plugin_id] = identity
        raise PluginIdentityConflictError("concurrent bind")

    persistence.bind_online_identity = bind_conflict  # type: ignore[method-assign]

    result = await _service(persistence, inventory, ["DemoPlugin"]).migrate()

    assert result.bound == 0
    assert result.skipped == 1
    winner = persistence.identities["demoplugin"]
    assert winner.revision == 2
    assert winner.trusted_source_key == THIRD_PARTY_SOURCE


@pytest.mark.asyncio
async def test_migration_does_not_replace_existing_bound_or_local_identity() -> None:
    """重复启动不得覆盖已绑定在线来源或本地开发身份。"""
    bound = replace(
        _legacy("BoundPlugin"),
        trusted_source_type=TrustedPluginSourceType.OFFICIAL,
        trusted_source_key=OFFICIAL_SOURCE,
        binding_basis=PluginBindingBasis.OFFICIAL_DEFAULT,
        bound_at=NOW,
    )
    local = replace(
        _legacy("LocalPlugin"),
        binding_basis=PluginBindingBasis.LOCAL_ONLY,
        payload_source_type=PluginPayloadSourceType.LOCAL,
        declared_version="1.0.0-dev",
        package_generation="v3",
        payload_receipt="sha256:" + "1" * 64,
        payload_applied_at=NOW,
    )
    persistence = _Persistence((bound, local))
    inventory = _inventory(
        _candidate(
            "BoundPlugin",
            source_type=TrustedPluginSourceType.OFFICIAL,
            source_key=OFFICIAL_SOURCE,
            repo_url=OFFICIAL_REPO,
        ),
        _candidate(
            "LocalPlugin",
            source_type=TrustedPluginSourceType.OFFICIAL,
            source_key=OFFICIAL_SOURCE,
            repo_url=OFFICIAL_REPO,
        ),
    )

    result = await _service(
        persistence,
        inventory,
        ["BoundPlugin", "LocalPlugin", "BOUNDPLUGIN"],
    ).migrate()

    assert result.created == 0
    assert result.bound == 0
    assert result.skipped == 3
    assert persistence.identities["boundplugin"] == bound
    assert persistence.identities["localplugin"] == local


@pytest.mark.asyncio
async def test_collect_online_restore_plugins_requires_trust_and_local_payload() -> None:
    """仅在线可信来源仍绑定的本地载荷需要进入启动恢复候选。"""
    trusted_local = replace(
        _legacy("TrustedLocal"),
        trusted_source_type=TrustedPluginSourceType.OFFICIAL,
        trusted_source_key=OFFICIAL_SOURCE,
        binding_basis=PluginBindingBasis.OFFICIAL_DEFAULT,
        payload_source_type=PluginPayloadSourceType.LOCAL,
        declared_version="9.9.10",
        package_generation="v3",
        payload_receipt="sha256:" + "2" * 64,
        bound_at=NOW,
        payload_applied_at=NOW,
    )
    local_only = replace(
        _legacy("LocalOnly"),
        binding_basis=PluginBindingBasis.LOCAL_ONLY,
        payload_source_type=PluginPayloadSourceType.LOCAL,
        declared_version="1.0.0-dev",
        package_generation="v3",
        payload_receipt="sha256:" + "3" * 64,
        payload_applied_at=NOW,
    )
    online = replace(
        _legacy("OnlinePayload"),
        trusted_source_type=TrustedPluginSourceType.OFFICIAL,
        trusted_source_key=OFFICIAL_SOURCE,
        binding_basis=PluginBindingBasis.OFFICIAL_DEFAULT,
        payload_source_type=PluginPayloadSourceType.OFFICIAL,
        payload_source_key=OFFICIAL_SOURCE,
        declared_version="1.2.0",
        package_generation="v3",
        payload_receipt="sha256:" + "4" * 64,
        bound_at=NOW,
        payload_applied_at=NOW,
    )
    persistence = _Persistence((trusted_local, local_only, online))

    result = await plugins_initializer._collect_online_restore_plugins(
        persistence,
        ["TrustedLocal", "TRUSTEDLOCAL", "LocalOnly", "OnlinePayload", "bad-id"],
    )

    assert result == {"trustedlocal"}


@pytest.mark.asyncio
async def test_sync_runs_identity_migration_before_automatic_install(
    monkeypatch,
) -> None:
    """启动自动同步必须在存量来源迁移完成后才能读取和替换载荷。"""
    order: list[str] = []
    manager = MagicMock()
    manager.mutation.return_value = nullcontext()

    def sync(_token, *, online_restore_plugins):
        order.append("sync")
        assert online_restore_plugins == {"demoplugin"}
        return []

    manager.sync.side_effect = sync
    manager.async_install_plugin_missing_dependencies_with_status = AsyncMock(
        return_value=PluginDependencyInstallResult(missing=[], success=True)
    )
    manager.get_plugin_runtime_statuses.return_value = {}
    manager.classify_plugins.return_value = MagicMock(ready=())
    manager.running_plugins = {}
    migration = MagicMock()

    async def migrate() -> None:
        order.append("migrate")

    migration.migrate = migrate
    identity = replace(
        _legacy(),
        trusted_source_type=TrustedPluginSourceType.OFFICIAL,
        trusted_source_key=OFFICIAL_SOURCE,
        binding_basis=PluginBindingBasis.OFFICIAL_DEFAULT,
        payload_source_type=PluginPayloadSourceType.LOCAL,
        declared_version="9.9.10",
        package_generation="v3",
        payload_receipt="sha256:" + "5" * 64,
        bound_at=NOW,
        payload_applied_at=NOW,
    )
    persistence = MagicMock()

    async def get_identity(_plugin_id: str) -> PluginIdentity:
        order.append("identity")
        return identity

    persistence.get_identity = get_identity
    config = MagicMock()
    config.get.return_value = ["DemoPlugin"]

    async def execute(_loop, task, _name):
        return task()

    monkeypatch.setattr(
        plugins_initializer.global_vars,
        "CURRENT_EVENT_LOOP",
        asyncio.get_running_loop(),
    )
    monkeypatch.setattr(
        plugins_initializer,
        "configure_plugin_services",
        lambda: order.append("configure"),
    )
    monkeypatch.setattr(plugins_initializer, "PluginManager", lambda: manager)
    monkeypatch.setattr(
        plugins_initializer,
        "get_plugin_identity_migration",
        lambda: migration,
    )
    monkeypatch.setattr(
        plugins_initializer,
        "get_plugin_persistence",
        lambda: persistence,
    )
    monkeypatch.setattr(
        plugins_initializer,
        "get_configured_system_config",
        lambda: config,
    )
    monkeypatch.setattr(plugins_initializer, "execute_task", execute)

    assert await plugins_initializer.sync_plugins() is False
    assert order == ["configure", "migrate", "identity", "sync"]


@pytest.mark.asyncio
async def test_sync_stops_before_automatic_install_when_identity_migration_fails(
    monkeypatch,
) -> None:
    """存量身份无法持久化时，启动同步不得继续读取或替换插件载荷。"""
    manager = MagicMock()
    manager.mutation.return_value = nullcontext()
    migration = MagicMock()
    migration.migrate = AsyncMock(side_effect=RuntimeError("database unavailable"))

    monkeypatch.setattr(
        plugins_initializer,
        "configure_plugin_services",
        lambda: None,
    )
    monkeypatch.setattr(plugins_initializer, "PluginManager", lambda: manager)
    monkeypatch.setattr(
        plugins_initializer,
        "get_plugin_identity_migration",
        lambda: migration,
    )

    assert await plugins_initializer.sync_plugins() is False
    manager.sync.assert_not_called()
