"""统一插件安装 Gateway 测试。"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.application.plugin.gateway import PluginInstallGateway
from app.application.plugin.identity import (
    PluginBindingBasis,
    PluginIdentity,
    PluginPayloadSourceType,
    TrustedPluginSourceType,
)
from app.application.plugin.source import (
    CandidateInventory,
    MarketRead,
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
        system_version=None,
        supports_v3=True,
        supports_v3t=None,
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
