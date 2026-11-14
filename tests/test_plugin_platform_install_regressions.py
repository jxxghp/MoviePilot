"""
验证安装回滚运行状态与历史Release的版本准入。
"""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from app.application.plugin.gateway import PluginInstallGateway
from app.application.plugin.install import PluginInstallResult
from app.domain.plugin import (
    build_plugin_release_install_plan,
    check_plugin_runtime_compatibility,
    check_plugin_system_version,
)
from app.schemas.plugin import PluginRuntimeStatus
from tests.test_plugin_install_command import _admission, _command
from tests.test_plugin_install_gateway import NOW, REPO_URL, _inventory


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [PluginRuntimeStatus.LOAD_FAILED, PluginRuntimeStatus.BLOCKED_BY_POLICY,
                                   PluginRuntimeStatus.DEPENDENCY_PENDING])
async def test_rollback_requires_successful_old_runtime_and_keeps_journal(status):
    """
    回滚重载返回非active时不宣称恢复，不删journal或执行注册刷新。
    """
    refresh = AsyncMock()
    command, persistence, _ = _command(
        target_reloader=AsyncMock(return_value=PluginRuntimeStatus.LOAD_FAILED),
        rollback_reloader=AsyncMock(return_value=status),
        registration_refresher=refresh,
    )
    result = await command.execute(admission=_admission(), release_version=None, force=True)
    assert not result.success
    assert result.rollback.runtime_attempted
    assert not result.rollback.runtime_restored
    assert not result.rollback.journal_deleted
    assert persistence.records
    assert any(status.value in error for error in result.rollback.errors)
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_install_restores_previously_disabled_host(monkeypatch):
    """
    启用位已持久修改后后续阶段失败，补偿仍恢复安装前停用状态。
    """
    enabled = [False]
    command, persistence, _ = _command(
        package_activate_backup=AsyncMock(side_effect=RuntimeError("activate failed")),
    )

    async def restore(_plugin_id, previous):
        """
        模拟实际启用位恢复端口。
        """
        enabled[0] = previous

    monkeypatch.setattr(command, "_PluginInstallCommand__host_enabled_reader", lambda _: enabled[0], raising=False)
    monkeypatch.setattr(command, "_PluginInstallCommand__host_enabled_restorer", restore, raising=False)
    monkeypatch.setattr(command, "_PluginInstallCommand__loadable_marker", lambda _: enabled.__setitem__(0, True))
    result = await command.execute(admission=_admission(), release_version=None, force=True)
    assert not result.success
    assert enabled[0] is False
    assert result.rollback.journal_deleted
    assert not persistence.records


@pytest.mark.asyncio
async def test_disabled_host_rollback_does_not_require_activating_old_plugin(monkeypatch):
    """
    安装前本体停用时，补偿应停止候选并恢复停用位，不强行重载旧插件才算成功。
    """
    old_reload = AsyncMock(return_value=PluginRuntimeStatus.LOAD_FAILED)
    restore = AsyncMock()
    command, persistence, _ = _command(
        target_reloader=AsyncMock(return_value=PluginRuntimeStatus.LOAD_FAILED),
        rollback_reloader=old_reload,
    )
    monkeypatch.setattr(command, "_PluginInstallCommand__host_enabled_reader", lambda _: False)
    monkeypatch.setattr(command, "_PluginInstallCommand__host_enabled_restorer", restore)
    result = await command.execute(admission=_admission(), release_version=None, force=True)
    assert not result.success
    assert result.rollback.runtime_restored
    assert result.rollback.journal_deleted
    assert not persistence.records
    old_reload.assert_not_awaited()
    restore.assert_awaited_once_with("DemoPlugin", False)


@pytest.mark.asyncio
@pytest.mark.parametrize("release,free_threaded,expected", [("1.0", False, True), (None, False, False),
                                                          ("2.0", False, False), ("1.0", True, False)])
async def test_gateway_historical_release_keeps_source_runtime_checks(release, free_threaded, expected):
    """
    指定旧Release不受最新包宿主版本限制，同时保留当前Release与运行时限制。
    """
    inventory = _inventory()
    candidate = replace(inventory.market_reads[0].candidates[0], plugin_version="2.0", dto={
        "version": "2.0", "release": True, "v3": True, "v3t": False, "system_version": ">=99",
    })
    inventory = replace(inventory, market_reads=(replace(inventory.market_reads[0], candidates=(candidate,)),))

    def compatibility(selected):
        """
        使用真实领域规则分别检查运行时和宿主版本。
        """
        runtime = check_plugin_runtime_compatibility(selected.dto, free_threaded=free_threaded)
        return runtime if not runtime[0] else check_plugin_system_version(selected.dto, current_version="3.1.0")

    executor = AsyncMock()
    executor.execute.return_value = PluginInstallResult(success=True)
    gateway = PluginInstallGateway(
        inventory=AsyncMock(return_value=inventory), identity=AsyncMock(return_value=None),
        candidate_compatibility=compatibility, executor=executor, clock=lambda: NOW,
        source_plugin_id=lambda value: value,
    )
    result = await gateway.install(plugin_id="DemoPlugin", repo_url=REPO_URL,
                                   explicit_source=True, release_version=release)
    assert result.success is expected
    if expected:
        assert executor.execute.await_args.kwargs["release_version"] == release
        assert executor.execute.await_args.kwargs["admission"].candidate is candidate
    else:
        executor.execute.assert_not_awaited()
    assert candidate.dto["system_version"] == ">=99"


@pytest.mark.parametrize("requirement,allowed", [(">=3,<4", True), (">=99", False)])
def test_selected_release_uses_its_own_host_constraint(requirement, allowed):
    """
    历史Release若提供宿主约束，应验证该版本自身的约束而非最新版本的约束。
    """
    plan, _ = build_plugin_release_install_plan(
        plugin_id="DemoPlugin", metadata={"release": True, "version": "2.0", "system_version": ">=99"},
        release_version="1.0", release_items=[{"version": "1.0", "system_version": requirement}],
        current_version="3.1.0",
    )
    assert (plan is not None) is allowed
