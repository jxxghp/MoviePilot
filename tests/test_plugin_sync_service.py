"""插件市场同步服务用例。"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.application.plugin.gateway import PluginInstallGateway
from app.application.plugin.identity import TrustedPluginSourceType
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
    install.assert_called_once_with(plugin.id, plugin.repo_url, False, None)


@pytest.mark.asyncio
async def test_market_sync_reuses_startup_lease_through_real_gateway(
    monkeypatch,
) -> None:
    """启动自动安装跨线程进入 Gateway 时必须复用同一个 startup lease。"""
    plugin = SimpleNamespace(
        id="DemoPlugin",
        repo_url=REPO_URL,
        plugin_name="Demo",
        plugin_version="1.0.0",
        system_version_compatible=True,
    )
    inventory = CandidateInventory((
        MarketRead.present(
            REPO_URL,
            (
                PluginMarketCandidate(
                    plugin_id=plugin.id,
                    source_key="github:jxxghp/moviepilot-plugins",
                    source_type=TrustedPluginSourceType.OFFICIAL,
                    repo_url=REPO_URL,
                    package_generation="v3",
                    plugin_version=plugin.plugin_version,
                    dto={"v3": True},
                ),
            ),
            package_generation="v3",
        ),
    ))
    executor = AsyncMock()
    executor.execute.return_value = PluginInstallResult(success=True)
    gateway = PluginInstallGateway(
        inventory=AsyncMock(return_value=inventory),
        identity=AsyncMock(return_value=None),
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
        repo_url: str,
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
            explicit_source=True,
            startup_token=startup_token,
        )

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

    async with plugin_lifecycle.hold_startup() as startup_token:
        synced = await asyncio.wait_for(
            asyncio.to_thread(service.sync, startup_token),
            timeout=2,
        )

    assert synced == [plugin.id]
    executor.execute.assert_awaited_once()
