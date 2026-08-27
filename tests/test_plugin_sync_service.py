"""插件市场同步服务用例。"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from packaging.version import Version

from app.application.plugin.catalog import PluginCatalogService
from app.application.plugin.declaration import PluginDeclaredMetadata
from app.application.plugin.gateway import PluginInstallGateway
from app.application.plugin.identity import (
    PluginBindingBasis,
    PluginIdentity,
    PluginPayloadSourceType,
    TrustedPluginSourceType,
)
from app.application.plugin.install import PluginInstallResult
from app.application.plugin.lifecycle import plugin_lifecycle
from app.application.plugin.source import (
    CandidateInventory,
    LocalCandidateRead,
    MarketRead,
    PluginLocalCandidate,
    PluginMarketCandidate,
)
from app.runtime.config import global_vars
from app.runtime.extensions.plugin.sync import LocalPluginSyncService, PluginSyncService
from app.startup.initializers import plugins as plugins_initializer

REPO_URL = "https://github.com/jxxghp/MoviePilot-Plugins"


def _merge_catalog_plugins(higher, base, markets):
    """通过生产目录服务合并启动同步候选。"""
    service = PluginCatalogService(
        market_loader=Mock(return_value={}),
        async_market_loader=AsyncMock(return_value={}),
        installed_plugins_provider=Mock(return_value=[]),
        plugin_mapper=Mock(),
        is_local_repo=lambda value: str(value).startswith("local://"),
        version_compare=lambda left, operator, right: (
            operator == ">" and Version(left) > Version(right)
        ),
        warning=Mock(),
        error=Mock(),
    )
    return service.merge(higher, base, markets)


def test_market_sync_keeps_install_rollback_enabled() -> None:
    """自动更新插件时保留旧版本，失败后可由安装器恢复。"""
    plugin = SimpleNamespace(
        id="DemoPlugin",
        repo_url="https://example.com/plugins",
        plugin_name="Demo",
        plugin_version="1.0.0",
        system_version_compatible=True,
    )
    install = Mock(return_value=(True, ""))
    service = PluginSyncService(
        frozen=lambda: False,
        installed_plugins=lambda: [plugin.id],
        online_plugins=lambda: [plugin],
        local_plugins=lambda: [],
        merge_plugins=lambda items, *_args: items,
        plugin_exists=lambda *_args: False,
        install=install,
        log=Mock(),
    )

    assert service.sync() == [plugin.id]
    install.assert_called_once_with(plugin.id, None, False, None)


def test_market_sync_restores_trusted_online_payload_after_local_source_removed() -> None:
    """本地高版本来源消失后，启动同步仍恢复已绑定的在线载荷。"""
    plugin = SimpleNamespace(
        id="DemoPlugin",
        repo_url=REPO_URL,
        plugin_name="Demo",
        plugin_version="1.2.0",
        system_version_compatible=False,
    )
    install = Mock(return_value=(True, ""))
    service = PluginSyncService(
        frozen=lambda: False,
        installed_plugins=lambda: [plugin.id],
        online_plugins=lambda: [plugin],
        local_plugins=lambda: [],
        merge_plugins=lambda items, *_args: items,
        plugin_exists=lambda *_args: True,
        install=install,
        log=Mock(),
    )

    assert service.sync(
        online_restore_plugins={"demoplugin"},
    ) == [plugin.id]
    install.assert_called_once_with(plugin.id, None, False, None)


def test_market_sync_reconciles_existing_local_candidate_through_gateway() -> None:
    """本地候选对应插件已延后激活，必须经 Gateway 协调后再启动。"""
    online = SimpleNamespace(
        id="DemoPlugin",
        repo_url=REPO_URL,
        plugin_name="Demo",
        plugin_version="1.2.0",
        system_version_compatible=True,
    )
    local = SimpleNamespace(
        id="DemoPlugin",
        repo_url="local://DemoPlugin?package_version=v3",
        plugin_name="Demo Local",
        plugin_version="9.9.10",
        system_version_compatible=True,
    )
    install = Mock(return_value=(True, ""))
    service = PluginSyncService(
        frozen=lambda: False,
        installed_plugins=lambda: [online.id],
        online_plugins=lambda: [online],
        local_plugins=lambda: [local],
        merge_plugins=lambda items, *_args: [online],
        plugin_exists=lambda *_args: True,
        install=install,
        log=Mock(),
    )

    assert service.sync(online_restore_plugins={"demoplugin"}) == [online.id]
    install.assert_called_once_with(online.id, None, False, None)


def test_market_sync_defers_source_selection_to_gateway() -> None:
    """启动目录只能决定同步目标，不能把本地候选升级为选源授权。"""
    local = SimpleNamespace(
        id="DemoPlugin",
        repo_url="local://DemoPlugin?path=/private/plugins&version=v3",
        plugin_name="Demo Local",
        plugin_version="3.3.2",
        system_version_compatible=True,
    )
    install = Mock(return_value=(True, ""))
    service = PluginSyncService(
        frozen=lambda: False,
        installed_plugins=lambda: [local.id],
        online_plugins=lambda: [],
        local_plugins=lambda: [local],
        merge_plugins=lambda items, *_args: items,
        plugin_exists=lambda *_args: False,
        install=install,
        log=Mock(),
    )

    assert service.sync() == [local.id]
    install.assert_called_once_with(local.id, None, False, None)


def test_market_sync_reports_local_install_failure() -> None:
    """本地载荷安装失败必须阻止启动编排继续激活旧代码。"""
    local = SimpleNamespace(
        id="DemoPlugin",
        repo_url="local://DemoPlugin?path=/private/plugins&version=v3",
        plugin_name="Demo Local",
        plugin_version="3.3.2",
        system_version_compatible=True,
    )
    service = PluginSyncService(
        frozen=lambda: False,
        installed_plugins=lambda: [local.id],
        online_plugins=lambda: [],
        local_plugins=lambda: [local],
        merge_plugins=lambda items, *_args: items,
        plugin_exists=lambda *_args: False,
        install=Mock(return_value=(False, "copy failed")),
        log=Mock(),
    )

    with pytest.raises(
        RuntimeError,
        match="延后激活的插件同步未完成：DemoPlugin",
    ):
        service.sync()


def test_local_sync_matches_installed_plugin_id_case_insensitively() -> None:
    """本地热同步应把大小写不同的索引 ID 识别为同一已安装插件。"""
    candidate = {
        "repo_url": "local://downloadcenter?package_version=v3",
        "package_version": "v3",
        "compatible": True,
    }
    system = Mock()
    system.install_plugin.return_value = (True, "")
    recent_sync: dict[str, float] = {}
    service = LocalPluginSyncService(
        installed_plugins=lambda: ["DownloadCenter"],
        candidate=Mock(return_value=candidate),
        system=lambda: system,
        recent_sync=recent_sync,
        log=Mock(),
    )

    assert service.sync("DownloadCenter", candidate)
    system.install_plugin.assert_called_once_with(
        plugin_id="DownloadCenter",
        repo_url=candidate["repo_url"],
        package_version="v3",
        force=True,
        local_sync=True,
        explicit_source=True,
    )
    assert "downloadcenter" in recent_sync


@pytest.mark.asyncio
async def test_market_sync_preserves_generation_priority_through_gateway(
    monkeypatch,
) -> None:
    """目录中的高版本 V2 不能绕过 Gateway 覆盖绑定仓库的 V3。"""
    online = PluginMarketCandidate(
        plugin_id="DownloadCenter",
        source_key="github:jxxghp/moviepilot-plugins",
        source_type=TrustedPluginSourceType.OFFICIAL,
        repo_url=REPO_URL,
        package_generation="v3",
        plugin_version="1.0.0",
        dto={"v3": True},
    )
    local = PluginLocalCandidate(
        plugin_id="downloadcenter",
        repo_url="local://downloadcenter?path=/private/plugins&version=v2",
        package_generation="v2",
        plugin_version="9.0.0",
        dto={"v3": True},
    )
    identity = PluginIdentity(
        plugin_id="DownloadCenter",
        normalized_plugin_id="downloadcenter",
        trusted_source_type=TrustedPluginSourceType.OFFICIAL,
        trusted_source_key=online.source_key,
        binding_basis=PluginBindingBasis.OFFICIAL_DEFAULT,
        payload_source_type=PluginPayloadSourceType.OFFICIAL,
        payload_source_key=online.source_key,
        declared_version=online.plugin_version,
        package_generation="v3",
        declared_metadata=PluginDeclaredMetadata.from_package(
            {"name": "Demo", "v3": True},
            declaration_version=online.plugin_version,
            manifest_matches_payload=True,
        ),
        payload_receipt="sha256:" + "2" * 64,
        revision=2,
        created_at=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
        bound_at=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
        payload_applied_at=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
    )
    inventory = CandidateInventory(
        (MarketRead.present(REPO_URL, (online,)),),
        (local,),
        local_read=LocalCandidateRead.present((local,)),
    )
    executor = AsyncMock()
    executor.execute.return_value = PluginInstallResult(success=True)
    gateway = PluginInstallGateway(
        inventory=AsyncMock(return_value=inventory),
        identity=AsyncMock(return_value=identity),
        candidate_compatibility=lambda _candidate: (True, ""),
        executor=executor,
        clock=lambda: datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        global_vars,
        "CURRENT_EVENT_LOOP",
        asyncio.get_running_loop(),
    )

    def install(
        plugin_id: str,
        repo_url: str | None,
        force: bool,
        startup_token: object | None,
    ) -> tuple[bool, str]:
        """按生产兼容入口让 Gateway 完成最终来源准入。"""
        return plugins_initializer._run_plugin_install_sync(
            gateway,
            plugin_id=plugin_id,
            repo_url=repo_url or "",
            package_version="v3",
            release_version=None,
            force=force,
            local_sync=True,
            explicit_source=False,
            startup_token=startup_token,
        )

    merged_local = SimpleNamespace(
        id=local.plugin_id,
        repo_url=local.repo_url,
        plugin_name="Demo Local",
        plugin_version=local.plugin_version,
        system_version_compatible=True,
    )
    merged_online = SimpleNamespace(
        id=online.plugin_id,
        repo_url=online.repo_url,
        plugin_name="Download Center",
        plugin_version=online.plugin_version,
        system_version_compatible=True,
    )
    service = PluginSyncService(
        frozen=lambda: False,
        installed_plugins=lambda: [online.plugin_id],
        online_plugins=lambda: [merged_online],
        local_plugins=lambda: [merged_local],
        merge_plugins=_merge_catalog_plugins,
        plugin_exists=lambda *_args: False,
        install=install,
        log=Mock(),
    )

    async with plugin_lifecycle.hold_startup() as startup_token:
        synced = await asyncio.wait_for(
            asyncio.to_thread(service.sync, startup_token),
            timeout=2,
        )

    assert synced == [local.plugin_id]
    executor.execute.assert_awaited_once()
    assert executor.execute.await_args.kwargs["local_sync"] is False
    admission = executor.execute.await_args.kwargs["admission"]
    assert admission.candidate is online
    assert admission.trusted_source_key == online.source_key


@pytest.mark.asyncio
async def test_market_sync_blocks_activation_when_gateway_selected_local_fails(
    monkeypatch,
) -> None:
    """目录预选在线候选时，Gateway 改选本地失败仍必须阻止旧载荷激活。"""
    competing_repo_url = "https://github.com/example/MoviePilot-Plugins"
    official = PluginMarketCandidate(
        plugin_id="DemoPlugin",
        source_key="github:jxxghp/moviepilot-plugins",
        source_type=TrustedPluginSourceType.OFFICIAL,
        repo_url=REPO_URL,
        package_generation="v3",
        plugin_version="1.0.0",
        dto={"v3": True},
    )
    competing = PluginMarketCandidate(
        plugin_id=official.plugin_id,
        source_key="github:example/moviepilot-plugins",
        source_type=TrustedPluginSourceType.THIRD_PARTY,
        repo_url=competing_repo_url,
        package_generation="v3",
        plugin_version="9.0.0",
        dto={"v3": True},
    )
    local = PluginLocalCandidate(
        plugin_id=official.plugin_id,
        repo_url="local://DemoPlugin?path=/private/plugins&version=v3",
        package_generation="v3",
        plugin_version="2.0.0",
        dto={"v3": True},
    )
    identity = PluginIdentity(
        plugin_id=official.plugin_id,
        normalized_plugin_id="demoplugin",
        trusted_source_type=TrustedPluginSourceType.OFFICIAL,
        trusted_source_key=official.source_key,
        binding_basis=PluginBindingBasis.OFFICIAL_DEFAULT,
        payload_source_type=PluginPayloadSourceType.OFFICIAL,
        payload_source_key=official.source_key,
        declared_version=official.plugin_version,
        package_generation="v3",
        declared_metadata=PluginDeclaredMetadata.from_package(
            {"name": "Demo", "v3": True},
            declaration_version=official.plugin_version,
            manifest_matches_payload=True,
        ),
        payload_receipt="sha256:" + "3" * 64,
        revision=2,
        created_at=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
        bound_at=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
        payload_applied_at=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
    )
    inventory = CandidateInventory(
        (
            MarketRead.present(REPO_URL, (official,)),
            MarketRead.present(competing_repo_url, (competing,)),
        ),
        (local,),
        local_read=LocalCandidateRead.present((local,)),
    )
    executor = AsyncMock(
        **{"execute.return_value": PluginInstallResult(
            success=False,
            message="copy failed",
        )}
    )
    gateway = PluginInstallGateway(
        inventory=AsyncMock(return_value=inventory),
        identity=AsyncMock(return_value=identity),
        candidate_compatibility=lambda _candidate: (True, ""),
        executor=executor,
        clock=lambda: datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        global_vars,
        "CURRENT_EVENT_LOOP",
        asyncio.get_running_loop(),
    )

    def install(
        plugin_id: str,
        repo_url: str | None,
        force: bool,
        startup_token: object | None,
    ) -> tuple[bool, str]:
        return plugins_initializer._run_plugin_install_sync(
            gateway,
            plugin_id=plugin_id,
            repo_url=repo_url or "",
            package_version="v3",
            release_version=None,
            force=force,
            local_sync=False,
            explicit_source=False,
            startup_token=startup_token,
        )

    merged_official = SimpleNamespace(
        id=official.plugin_id,
        repo_url=official.repo_url,
        plugin_name="Demo Official",
        plugin_version=official.plugin_version,
        system_version_compatible=True,
    )
    merged_competing = SimpleNamespace(
        id=competing.plugin_id,
        repo_url=competing.repo_url,
        plugin_name="Demo Competing",
        plugin_version=competing.plugin_version,
        system_version_compatible=True,
    )
    merged_local = SimpleNamespace(
        id=local.plugin_id,
        repo_url=local.repo_url,
        plugin_name="Demo Local",
        plugin_version=local.plugin_version,
        system_version_compatible=True,
    )
    service = PluginSyncService(
        frozen=lambda: False,
        installed_plugins=lambda: [official.plugin_id],
        online_plugins=lambda: [merged_official, merged_competing],
        local_plugins=lambda: [merged_local],
        merge_plugins=_merge_catalog_plugins,
        plugin_exists=lambda *_args: False,
        install=install,
        log=Mock(),
    )

    async with plugin_lifecycle.hold_startup() as startup_token:
        with pytest.raises(
            RuntimeError,
            match="延后激活的插件同步未完成：DemoPlugin",
        ):
            await asyncio.wait_for(
                asyncio.to_thread(service.sync, startup_token),
                timeout=2,
            )

    executor.execute.assert_awaited_once()
    assert executor.execute.await_args.kwargs["local_sync"] is True
    admission = executor.execute.await_args.kwargs["admission"]
    assert admission.candidate is local


@pytest.mark.asyncio
async def test_market_sync_reuses_startup_lease_through_real_gateway(
    monkeypatch,
) -> None:
    """启动自动安装跨线程进入 Gateway 时必须复用同一个 startup lease。"""
    competing_repo_url = "https://github.com/example/MoviePilot-Plugins"
    plugin = SimpleNamespace(
        id="DemoPlugin",
        repo_url=competing_repo_url,
        plugin_name="Demo",
        plugin_version="9.0.0",
        system_version_compatible=True,
    )
    official_candidate = PluginMarketCandidate(
        plugin_id=plugin.id,
        source_key="github:jxxghp/moviepilot-plugins",
        source_type=TrustedPluginSourceType.OFFICIAL,
        repo_url=REPO_URL,
        package_generation="v3",
        plugin_version="1.1.0",
        dto={"v3": True},
    )
    competing_candidate = PluginMarketCandidate(
        plugin_id=plugin.id,
        source_key="github:example/moviepilot-plugins",
        source_type=TrustedPluginSourceType.THIRD_PARTY,
        repo_url=competing_repo_url,
        package_generation="v3",
        plugin_version=plugin.plugin_version,
        dto={"v3": True},
    )
    inventory = CandidateInventory((
        MarketRead.present(
            REPO_URL,
            (official_candidate,),
            package_generation="v3",
        ),
        MarketRead.present(
            competing_repo_url,
            (competing_candidate,),
            package_generation="v3",
        ),
    ))
    identity = PluginIdentity(
        plugin_id=plugin.id,
        normalized_plugin_id="demoplugin",
        trusted_source_type=TrustedPluginSourceType.OFFICIAL,
        trusted_source_key="github:jxxghp/moviepilot-plugins",
        binding_basis=PluginBindingBasis.OFFICIAL_DEFAULT,
        payload_source_type=PluginPayloadSourceType.LOCAL,
        payload_source_key=None,
        declared_version="9.9.10",
        package_generation="v3",
        declared_metadata=PluginDeclaredMetadata.from_package(
            {"name": "Demo local", "v3": True},
            declaration_version="9.9.10",
            manifest_matches_payload=True,
        ),
        payload_receipt="sha256:" + "0" * 64,
        revision=1,
        created_at=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
        bound_at=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
        payload_applied_at=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
    )
    executor = AsyncMock()
    executor.execute.return_value = PluginInstallResult(success=True)
    gateway = PluginInstallGateway(
        inventory=AsyncMock(return_value=inventory),
        identity=AsyncMock(return_value=identity),
        candidate_compatibility=lambda _candidate: (True, ""),
        executor=executor,
        clock=lambda: datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        global_vars,
        "CURRENT_EVENT_LOOP",
        asyncio.get_running_loop(),
    )

    def install(
        plugin_id: str,
        repo_url: str | None,
        force: bool,
        startup_token: object | None,
    ) -> tuple[bool, str]:
        """复用生产同步包装层，把线程池安装提交回宿主事件循环。"""
        return plugins_initializer._run_plugin_install_sync(
            gateway,
            plugin_id=plugin_id,
            repo_url=repo_url,
            package_version="v3",
            release_version=None,
            force=force,
            local_sync=False,
            explicit_source=False,
            startup_token=startup_token,
        )

    service = PluginSyncService(
        frozen=lambda: False,
        installed_plugins=lambda: [plugin.id],
        online_plugins=lambda: [plugin],
        local_plugins=lambda: [],
        merge_plugins=lambda items, *_args: items,
        plugin_exists=lambda *_args: True,
        install=install,
        log=Mock(),
    )

    async with plugin_lifecycle.hold_startup() as startup_token:
        synced = await asyncio.wait_for(
            asyncio.to_thread(
                service.sync,
                startup_token,
                online_restore_plugins={"demoplugin"},
            ),
            timeout=2,
        )

    assert synced == [plugin.id]
    executor.execute.assert_awaited_once()
    admission = executor.execute.await_args.kwargs["admission"]
    assert admission.candidate.repo_url == REPO_URL
