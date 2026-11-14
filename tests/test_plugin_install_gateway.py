"""
统一插件安装 Gateway 测试。
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.application.plugin import gateway as gateway_module
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
from app.schemas.exception import PluginMutationRejectedError

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
REPO_URL = "https://github.com/jxxghp/MoviePilot-Plugins"


def _inventory(plugin_version: str = "1.0.0") -> CandidateInventory:
    """
    构造仅含官方候选的完整库存。
    """
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
                    plugin_version=plugin_version,
                    dto={"v3": True},
                ),
            ),
            package_generation="v3",
        ),
    ))


def _other_plugin_inventory() -> CandidateInventory:
    """
    构造不含目标插件的库存，用于验证本地专属安装不强刷市场。
    """
    return CandidateInventory((
        MarketRead.present(
            REPO_URL,
            (
                PluginMarketCandidate(
                    plugin_id="OtherPlugin",
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


def _gateway(
    *,
    inventory,
    executor,
    market_refresh=None,
    identity=None,
):
    """
    组装一个只依赖注入桩件的 Gateway。
    """
    return PluginInstallGateway(
        inventory=inventory,
        identity=identity or AsyncMock(return_value=None),
        candidate_compatibility=lambda _candidate: (True, ""),
        executor=executor,
        clock=lambda: NOW,
        source_plugin_id=lambda plugin_id: plugin_id,
        market_refresh=market_refresh,
    )


def _executor() -> AsyncMock:
    """
    返回带成功结果的执行器桩件。
    """
    executor = AsyncMock()
    executor.execute.return_value = type(
        "Result",
        (),
        {"success": True, "message": ""},
    )()
    return executor


@pytest.mark.asyncio
async def test_forced_update_refreshes_only_the_markets_holding_the_plugin() -> None:
    """
    未指定版本的强制更新只重读该插件出现过的市场，并按新索引准入。

    Release tag 取自索引里的 version；沿用缓存索引会把同一个旧版本重装一遍
    还提示更新成功，这正是「关于」里点更新没有反应的原因。
    """
    inventory = AsyncMock(side_effect=[_inventory(), _inventory("1.0.1")])
    market_refresh = AsyncMock()
    executor = _executor()
    gateway = _gateway(
        inventory=inventory, executor=executor, market_refresh=market_refresh
    )

    result = await gateway.install(
        plugin_id="DemoPlugin",
        repo_url=REPO_URL,
        package_version="v3",
        force=True,
        explicit_source=True,
    )

    assert result.success is True
    market_refresh.assert_awaited_once_with((REPO_URL,))
    # 全市场读取仍然只走缓存，不能被单个插件的更新放大成强制刷新
    assert [call.args[0] for call in inventory.await_args_list] == [False, False]
    assert executor.execute.await_args.kwargs["admission"].candidate.plugin_version == "1.0.1"


@pytest.mark.asyncio
async def test_selected_release_skips_the_market_refresh() -> None:
    """
    用户已点名 Release 版本时不需要重读索引，tag 直接由所选版本拼出。
    """
    inventory = AsyncMock(return_value=_inventory())
    market_refresh = AsyncMock()
    gateway = _gateway(
        inventory=inventory, executor=_executor(), market_refresh=market_refresh
    )

    await gateway.install(
        plugin_id="DemoPlugin",
        repo_url=REPO_URL,
        package_version="v3",
        release_version="0.9.0",
        force=True,
        explicit_source=True,
    )

    market_refresh.assert_not_awaited()
    inventory.assert_awaited_once_with(False)


@pytest.mark.asyncio
async def test_market_without_the_plugin_is_not_refreshed_on_forced_install() -> None:
    """
    目标插件不在任何市场索引里（本地专属安装）时不发起市场刷新。
    """
    inventory = AsyncMock(return_value=_other_plugin_inventory())
    market_refresh = AsyncMock()
    gateway = _gateway(
        inventory=inventory, executor=_executor(), market_refresh=market_refresh
    )

    await gateway.install(
        plugin_id="DemoPlugin",
        repo_url="local://DemoPlugin?version=v3",
        package_version="v3",
        force=True,
    )

    market_refresh.assert_not_awaited()
    inventory.assert_awaited_once_with(False)


@pytest.mark.asyncio
async def test_gateway_reports_lifecycle_busy_before_identity_or_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    生命周期准入预算耗尽时返回明确失败，不能继续读取身份或写入载荷。
    """
    identity = AsyncMock()
    executor = AsyncMock()

    monkeypatch.setattr(
        gateway_module.plugin_lifecycle, "hold",
        Mock(side_effect=PluginMutationRejectedError("安装 DemoPlugin", "插件生命周期准入正忙")),
    )
    gateway = PluginInstallGateway(
        inventory=AsyncMock(return_value=_inventory()), identity=identity,
        candidate_compatibility=lambda _candidate: (True, ""),
        executor=executor, clock=lambda: NOW,
        source_plugin_id=lambda plugin_id: plugin_id,
    )
    result = await gateway.install(plugin_id="DemoPlugin", repo_url=REPO_URL)
    assert result.success is False
    assert result.failure_stage == "admission"
    assert "准入正忙" in result.message
    identity.assert_not_awaited()
    executor.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_freezes_admission_before_executing_transaction() -> None:
    """
    Gateway 只把已选中的候选交给事务执行器。
    """
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
        source_plugin_id=lambda plugin_id: plugin_id,
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
async def test_force_install_reuses_cached_candidate_inventory() -> None:
    """
    强制覆盖载荷时不得连带强刷全部远程插件仓库。
    """
    inventory = AsyncMock(return_value=_inventory())
    executor = AsyncMock()
    executor.execute.return_value = type(
        "Result",
        (),
        {"success": True, "message": ""},
    )()
    gateway = PluginInstallGateway(
        inventory=inventory,
        identity=AsyncMock(return_value=None),
        candidate_compatibility=lambda _candidate: (True, ""),
        executor=executor,
        clock=lambda: NOW,
        source_plugin_id=lambda plugin_id: plugin_id,
    )

    result = await gateway.install(
        plugin_id="DemoPlugin",
        repo_url=REPO_URL,
        package_version="v3",
        force=True,
        explicit_source=True,
    )

    assert result.success is True
    inventory.assert_awaited_once_with(False)
    assert executor.execute.await_args.kwargs["force"] is True


@pytest.mark.asyncio
async def test_local_only_requires_explicit_online_binding() -> None:
    """
    本地专属身份即使发现唯一在线来源，也只能由管理员显式绑定。
    """
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
        source_plugin_id=lambda plugin_id: plugin_id,
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
async def test_explicit_local_sync_uses_local_candidate_and_execution_mode() -> None:
    """
    文件监控明确指定本地候选时必须同步本地载荷。
    """
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
        trusted_source_type=TrustedPluginSourceType.OFFICIAL,
        trusted_source_key="github:jxxghp/moviepilot-plugins",
        binding_basis=PluginBindingBasis.OFFICIAL_DEFAULT,
        payload_source_type=PluginPayloadSourceType.OFFICIAL,
        payload_source_key="github:jxxghp/moviepilot-plugins",
        declared_version="9.0.0",
        package_generation="v3",
        declared_metadata=PluginDeclaredMetadata.from_package(
            {"name": "Demo online", "v3": True},
            declaration_version="9.0.0",
            manifest_matches_payload=True,
        ),
        payload_receipt="sha256:" + "1" * 64,
        revision=2,
        created_at=NOW,
        updated_at=NOW,
        bound_at=NOW,
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
        source_plugin_id=lambda plugin_id: plugin_id,
    )

    result = await gateway.install(
        plugin_id="DemoPlugin",
        repo_url=local.repo_url,
        package_version="v3",
        force=True,
        local_sync=True,
        explicit_source=True,
    )

    assert result.success is True
    assert executor.execute.await_args.kwargs["admission"].candidate is local
    assert executor.execute.await_args.kwargs["local_sync"] is True


@pytest.mark.asyncio
async def test_gateway_rejects_source_conflict_before_package_execution() -> None:
    """
    来源准入失败时不进入文件和数据库事务。
    """
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
        source_plugin_id=lambda plugin_id: plugin_id,
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
    """
    跨仓聚合不能替代最终可信候选的系统版本兼容门禁。
    """
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
        source_plugin_id=lambda plugin_id: plugin_id,
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
async def test_gateway_source_inspection_preserves_sources_without_local_path() -> None:
    """
    来源查询按在线仓归并版本，但不返回本地仓库路径。
    """
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
        source_plugin_id=lambda plugin_id: plugin_id,
    )

    inspection = await gateway.inspect_source(plugin_id="DemoPlugin")

    assert [candidate.source_key for candidate in inspection.online_candidates] == [
        "github:jxxghp/moviepilot-plugins",
        "github:example/moviepilot-plugins",
    ]
    assert inspection.online_candidates[0].package_generation == "v3"
    assert inspection.local_candidate is local
    public_repo_url = inspection.local_candidate.public_dict()["repo_url"]
    assert public_repo_url == "local://DemoPlugin?version=v3"
    assert "/private/plugins" not in public_repo_url


@pytest.mark.asyncio
async def test_gateway_forwards_explicit_source_change_revision() -> None:
    """
    显式换源的目标来源和 revision 必须冻结到事务准入结果。
    """
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
        source_plugin_id=lambda plugin_id: plugin_id,
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


@pytest.mark.asyncio
async def test_inspect_source_resolves_clone_id_to_its_source_plugin() -> None:
    """
    分身勘察来源必须先归一到源插件，否则查不到任何安装包。
    """
    identity = AsyncMock(return_value=None)
    gateway = PluginInstallGateway(
        inventory=AsyncMock(return_value=_inventory()),
        identity=identity,
        candidate_compatibility=lambda _candidate: (True, ""),
        executor=AsyncMock(),
        clock=lambda: NOW,
        source_plugin_id=lambda plugin_id: (
            "DemoPlugin" if plugin_id == "DemoPluginwork" else plugin_id
        ),
    )

    inspection = await gateway.inspect_source(plugin_id="DemoPluginwork")

    assert [candidate.plugin_id for candidate in inspection.online_candidates] == [
        "DemoPlugin"
    ]
    assert inspection.selection.candidate is not None
    assert inspection.plugin_id == "DemoPlugin"
    identity.assert_awaited_once_with("DemoPlugin")


@pytest.mark.asyncio
async def test_install_resolves_clone_id_to_its_source_plugin() -> None:
    """
    分身安装必须先归一到源插件，否则准入阶段就找不到安装包。
    """
    executor = AsyncMock()
    executor.execute.return_value = type(
        "Result",
        (),
        {"success": True, "message": ""},
    )()
    identity = AsyncMock(return_value=None)
    gateway = PluginInstallGateway(
        inventory=AsyncMock(return_value=_inventory()),
        identity=identity,
        candidate_compatibility=lambda _candidate: (True, ""),
        executor=executor,
        clock=lambda: NOW,
        source_plugin_id=lambda plugin_id: (
            "DemoPlugin" if plugin_id == "DemoPluginwork" else plugin_id
        ),
    )

    result = await gateway.install(
        plugin_id="DemoPluginwork",
        repo_url=REPO_URL,
        package_version="v3",
        explicit_source=True,
    )

    assert result.success is True
    admission = executor.execute.await_args.kwargs["admission"]
    assert admission.candidate.plugin_id == "DemoPlugin"
    identity.assert_awaited_once_with("DemoPlugin")


@pytest.mark.asyncio
async def test_install_holds_lifecycle_on_the_source_plugin_for_clones() -> None:
    """
    分身安装的生命周期占位必须落在源插件上，同源分身才不会并发改写同一份载荷。
    """
    held: list[str] = []

    @asynccontextmanager
    async def _record_hold(plugin_id: str, _startup_token=None):
        """
        记录安装期间实际占位的插件 ID。
        """
        held.append(plugin_id)
        yield

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
        source_plugin_id=lambda plugin_id: (
            "DemoPlugin" if plugin_id == "DemoPluginwork" else plugin_id
        ),
    )

    with patch.object(gateway_module.plugin_lifecycle, "hold", _record_hold):
        result = await gateway.install(
            plugin_id="DemoPluginwork",
            repo_url=REPO_URL,
            package_version="v3",
            explicit_source=True,
        )

    assert result.success is True
    assert held == ["DemoPlugin"]
