"""插件市场同步服务用例。"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

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
    MarketRead,
    PluginMarketCandidate,
)
from app.runtime.config import global_vars
from app.runtime.extensions.plugin.sync import PluginSyncService
from app.startup.initializers import plugins as plugins_initializer

REPO_URL = "https://github.com/jxxghp/MoviePilot-Plugins"


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


def test_market_sync_keeps_active_local_payload_when_candidate_still_exists() -> None:
    """本地候选仍存在时，不应被启动在线恢复覆盖。"""
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

    assert service.sync(online_restore_plugins={"demoplugin"}) == []
    install.assert_not_called()


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
        system_version=None,
        supports_v3=True,
        supports_v3t=None,
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
