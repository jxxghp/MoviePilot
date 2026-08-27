"""统一插件安装 Gateway 测试。"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from app.application.plugin.declaration import PluginDeclaredMetadata
from app.application.plugin.gateway import PluginInstallGateway
from app.application.plugin.identity import (
    PluginBindingBasis,
    PluginIdentity,
    PluginPayloadSourceType,
    TrustedPluginSourceType,
)
from app.application.plugin.source import (
    CandidateInventory,
    LocalCandidateRead,
    MarketRead,
    PluginLocalCandidate,
    PluginMarketCandidate,
)

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
REPO_URL = "https://github.com/jxxghp/MoviePilot-Plugins"


def _inventory() -> CandidateInventory:
    """构造仅含官方候选的完整库存。"""
    return CandidateInventory((
        MarketRead.present(
            REPO_URL,
            (
                PluginMarketCandidate(
                    plugin_id="DemoPlugin",
                    source_key="github:jxxghp/moviepilot-plugins",
                    source_type=TrustedPluginSourceType.OFFICIAL,
                    repo_url=REPO_URL,
                    package_generation="v3",
                    plugin_version="1.0.0",
                    dto={"v3": True},
                ),
            ),
            package_generation="v3",
        ),
    ))


@pytest.mark.asyncio
async def test_gateway_freezes_admission_before_executing_transaction() -> None:
    """Gateway 只把已选中的候选交给事务执行器。"""
    executor = AsyncMock()
    executor.execute.return_value = type(
        "Result",
        (),
        {"success": True, "message": ""},
    )()
    gateway = PluginInstallGateway(
        inventory=AsyncMock(return_value=_inventory()),
        identity=AsyncMock(return_value=None),
        candidate_compatibility=lambda _candidate: (True, ""),
        executor=executor,
        clock=lambda: NOW,
    )

    result = await gateway.install(
        plugin_id="DemoPlugin",
        repo_url=REPO_URL,
        package_version="v3",
        explicit_source=True,
    )

    assert result.success is True
    admission = executor.execute.await_args.kwargs["admission"]
    assert admission.candidate.repo_url == REPO_URL
    assert admission.expected_revision is None


@pytest.mark.asyncio
async def test_local_only_requires_explicit_online_binding() -> None:
    """本地专属身份即使发现唯一在线来源，也只能由管理员显式绑定。"""
    online = PluginMarketCandidate(
        plugin_id="DemoPlugin",
        source_key="github:jxxghp/moviepilot-plugins",
        source_type=TrustedPluginSourceType.OFFICIAL,
        repo_url=REPO_URL,
        package_generation="v3",
        plugin_version="9.0.0",
        dto={"v3": True},
    )
    local = PluginLocalCandidate(
        plugin_id="DemoPlugin",
        repo_url="local://DemoPlugin?version=v3",
        package_generation="v3",
        plugin_version="1.0.0",
        dto={"v3": True},
    )
    identity = PluginIdentity(
        plugin_id="DemoPlugin",
        normalized_plugin_id="demoplugin",
        trusted_source_type=TrustedPluginSourceType.UNKNOWN,
        trusted_source_key=None,
        binding_basis=PluginBindingBasis.LOCAL_ONLY,
        payload_source_type=PluginPayloadSourceType.LOCAL,
        payload_source_key=None,
        declared_version="1.0.0",
        package_generation="v3",
        declared_metadata=PluginDeclaredMetadata.from_package(
            {"name": "Demo local", "v3": True},
            declaration_version="1.0.0",
            manifest_matches_payload=True,
        ),
        payload_receipt="sha256:" + "1" * 64,
        revision=2,
        created_at=NOW,
        updated_at=NOW,
        bound_at=None,
        payload_applied_at=NOW,
    )
    executor = AsyncMock()
    executor.execute.return_value = type(
        "Result",
        (),
        {"success": True, "message": ""},
    )()
    gateway = PluginInstallGateway(
        inventory=AsyncMock(
            return_value=CandidateInventory(
                (MarketRead.present(REPO_URL, (online,)),),
                (local,),
                local_read=LocalCandidateRead.present((local,)),
            )
        ),
        identity=AsyncMock(return_value=identity),
        candidate_compatibility=lambda _candidate: (True, ""),
        executor=executor,
        clock=lambda: NOW,
    )

    automatic = await gateway.install(
        plugin_id="DemoPlugin",
        repo_url=None,
        package_version="v3",
    )

    assert automatic.success is True
    automatic_admission = executor.execute.await_args.kwargs["admission"]
    assert automatic_admission.candidate is local
    assert automatic_admission.binding_basis is PluginBindingBasis.LOCAL_ONLY
    assert automatic_admission.trusted_source_key is None

    hinted = await gateway.install(
        plugin_id="DemoPlugin",
        repo_url=REPO_URL,
        package_version="v3",
    )

    assert hinted.success is True
    hinted_admission = executor.execute.await_args.kwargs["admission"]
    assert hinted_admission.candidate is local
    assert hinted_admission.binding_basis is PluginBindingBasis.LOCAL_ONLY
    assert hinted_admission.trusted_source_key is None

    explicit = await gateway.install(
        plugin_id="DemoPlugin",
        repo_url=REPO_URL,
        package_version="v3",
        explicit_source=True,
    )

    assert explicit.success is True
    explicit_admission = executor.execute.await_args.kwargs["admission"]
    assert explicit_admission.candidate is online
    assert explicit_admission.binding_basis is PluginBindingBasis.EXPLICIT_INSTALL
    assert explicit_admission.trusted_source_key == online.source_key


@pytest.mark.asyncio
async def test_gateway_rejects_source_conflict_before_package_execution() -> None:
    """来源准入失败时不进入文件和数据库事务。"""
    other = PluginMarketCandidate(
        plugin_id="DemoPlugin",
        source_key="github:example/moviepilot-plugins",
        source_type=TrustedPluginSourceType.THIRD_PARTY,
        repo_url="https://github.com/example/moviepilot-plugins",
        package_generation="v3",
        plugin_version="2.0.0",
        dto={"v3": True},
    )
    executor = AsyncMock()
    gateway = PluginInstallGateway(
        inventory=AsyncMock(
            return_value=CandidateInventory((
                MarketRead.present(REPO_URL, (_inventory().online_candidates[0], other)),
            ))
        ),
        identity=AsyncMock(return_value=None),
        candidate_compatibility=lambda _candidate: (True, ""),
        executor=executor,
        clock=lambda: NOW,
    )

    result = await gateway.install(
        plugin_id="DemoPlugin",
        repo_url=None,
    )

    assert result.success is False
    assert result.failure_stage == "source_admission"
    executor.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_checks_compatibility_on_final_trusted_candidate() -> None:
    """跨仓聚合不能替代最终可信候选的系统版本兼容门禁。"""
    official = PluginMarketCandidate(
        plugin_id="DemoPlugin",
        source_key="github:jxxghp/moviepilot-plugins",
        source_type=TrustedPluginSourceType.OFFICIAL,
        repo_url=REPO_URL,
        package_generation="v3",
        plugin_version="1.2.0",
        dto={"v3": True, "system_version": ">=99"},
    )
    competing = PluginMarketCandidate(
        plugin_id="DemoPlugin",
        source_key="github:example/moviepilot-plugins",
        source_type=TrustedPluginSourceType.THIRD_PARTY,
        repo_url="https://github.com/example/moviepilot-plugins",
        package_generation="v3",
        plugin_version="9.9.10",
        dto={"v3": True},
    )
    identity = PluginIdentity(
        plugin_id="DemoPlugin",
        normalized_plugin_id="demoplugin",
        trusted_source_type=TrustedPluginSourceType.OFFICIAL,
        trusted_source_key="github:jxxghp/moviepilot-plugins",
        binding_basis=PluginBindingBasis.OFFICIAL_DEFAULT,
        payload_source_type=PluginPayloadSourceType.LOCAL,
        payload_source_key=None,
        declared_version="9.9.9",
        package_generation="v3",
        declared_metadata=PluginDeclaredMetadata.from_package(
            {"name": "Demo local", "v3": True},
            declaration_version="9.9.9",
            manifest_matches_payload=True,
        ),
        payload_receipt="sha256:" + "1" * 64,
        revision=3,
        created_at=NOW,
        updated_at=NOW,
        bound_at=NOW,
        payload_applied_at=NOW,
    )
    compatibility = Mock(return_value=(False, "当前版本不满足插件要求"))
    executor = AsyncMock()
    gateway = PluginInstallGateway(
        inventory=AsyncMock(
            return_value=CandidateInventory((
                MarketRead.present(REPO_URL, (official,)),
                MarketRead.present(competing.repo_url, (competing,)),
            ))
        ),
        identity=AsyncMock(return_value=identity),
        candidate_compatibility=compatibility,
        executor=executor,
        clock=lambda: NOW,
    )

    result = await gateway.install(
        plugin_id="DemoPlugin",
        repo_url=None,
        package_version="v3",
    )

    assert result.success is False
    assert result.failure_stage == "source_admission"
    assert result.message == "当前版本不满足插件要求"
    compatibility.assert_called_once_with(official)
    executor.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_source_inspection_preserves_sources_and_hides_local_path() -> None:
    """来源查询按在线仓归并版本，本地候选只保留类型与版本。"""
    official_v3 = _inventory().online_candidates[0]
    official_v2 = PluginMarketCandidate(
        plugin_id="DemoPlugin",
        source_key="github:jxxghp/moviepilot-plugins",
        source_type=TrustedPluginSourceType.OFFICIAL,
        repo_url=REPO_URL,
        package_generation="v2",
        plugin_version="9.0.0",
        dto={"v2": True},
    )
    third_party = PluginMarketCandidate(
        plugin_id="DemoPlugin",
        source_key="github:example/moviepilot-plugins",
        source_type=TrustedPluginSourceType.THIRD_PARTY,
        repo_url="https://github.com/example/moviepilot-plugins",
        package_generation="v3",
        plugin_version="2.0.0",
        dto={"v3": True},
    )
    local = PluginLocalCandidate(
        plugin_id="DemoPlugin",
        repo_url="local://DemoPlugin?path=/private/plugins&version=v3",
        package_generation="v3",
        plugin_version="3.0.0-dev",
        dto={"path": "/private/plugins", "v3": True},
    )
    inventory = CandidateInventory(
        (
            MarketRead.present(
                third_party.repo_url,
                (third_party,),
                package_generation="v3",
            ),
            MarketRead.present(REPO_URL, (official_v3,), package_generation="v3"),
            MarketRead.present(REPO_URL, (official_v2,), package_generation="v2"),
        ),
        (local,),
        local_read=LocalCandidateRead.present((local,)),
    )
    gateway = PluginInstallGateway(
        inventory=AsyncMock(return_value=inventory),
        identity=AsyncMock(return_value=None),
        candidate_compatibility=lambda _candidate: (True, ""),
        executor=AsyncMock(),
        clock=lambda: NOW,
    )

    inspection = await gateway.inspect_source(plugin_id="DemoPlugin")

    assert [candidate.source_key for candidate in inspection.online_candidates] == [
        "github:jxxghp/moviepilot-plugins",
        "github:example/moviepilot-plugins",
    ]
    assert inspection.online_candidates[0].package_generation == "v3"
    assert inspection.local_candidate is local
    assert "/private/plugins" not in str(inspection.local_candidate.public_dict())


@pytest.mark.asyncio
async def test_gateway_forwards_explicit_source_change_revision() -> None:
    """显式换源的目标来源和 revision 必须冻结到事务准入结果。"""
    current = PluginIdentity(
        plugin_id="DemoPlugin",
        normalized_plugin_id="demoplugin",
        trusted_source_type=TrustedPluginSourceType.OFFICIAL,
        trusted_source_key="github:jxxghp/moviepilot-plugins",
        binding_basis=PluginBindingBasis.OFFICIAL_DEFAULT,
        payload_source_type=PluginPayloadSourceType.OFFICIAL,
        payload_source_key="github:jxxghp/moviepilot-plugins",
        declared_version="1.0.0",
        package_generation="v3",
        declared_metadata=PluginDeclaredMetadata.from_package(
            {"name": "Demo", "v3": True},
            declaration_version="1.0.0",
            manifest_matches_payload=True,
        ),
        payload_receipt="sha256:" + "0" * 64,
        revision=4,
        created_at=NOW,
        updated_at=NOW,
        bound_at=NOW,
        payload_applied_at=NOW,
    )
    candidate = PluginMarketCandidate(
        plugin_id="DemoPlugin",
        source_key="github:example/moviepilot-plugins",
        source_type=TrustedPluginSourceType.THIRD_PARTY,
        repo_url="https://github.com/example/moviepilot-plugins",
        package_generation="v3",
        plugin_version="2.0.0",
        dto={"v3": True},
    )
    executor = AsyncMock()
    executor.execute.return_value = type(
        "Result",
        (),
        {"success": True, "message": ""},
    )()
    gateway = PluginInstallGateway(
        inventory=AsyncMock(
            return_value=CandidateInventory((MarketRead.present(REPO_URL, (candidate,)),))
        ),
        identity=AsyncMock(return_value=current),
        candidate_compatibility=lambda _candidate: (True, ""),
        executor=executor,
        clock=lambda: NOW,
    )

    result = await gateway.install(
        plugin_id="DemoPlugin",
        repo_url=candidate.repo_url,
        explicit_source=True,
        source_change=True,
        expected_revision=4,
    )

    assert result.success is True
    admission = executor.execute.await_args.kwargs["admission"]
    assert admission.identity_before == current
    assert admission.expected_revision == 4
    assert admission.binding_basis is PluginBindingBasis.EXPLICIT_SOURCE_CHANGE
    assert admission.trusted_source_key == candidate.source_key
