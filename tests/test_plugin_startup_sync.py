"""
插件启动恢复跨线程的事务上下文、串行安装和失败隔离回归。
"""

import asyncio
from contextlib import nullcontext
from contextvars import Context
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.application.plugin.gateway import PluginInstallGateway
from app.application.plugin.identity import TrustedPluginSourceType
from app.application.plugin.install import PluginInstallCommand
from app.application.plugin.lifecycle import PluginStartupLease, plugin_lifecycle
from app.application.plugin.source import (
    CandidateInventory,
    MarketRead,
    PluginMarketCandidate,
)
from app.runtime.config import global_vars
from app.runtime.extensions.plugin.admission import PluginMutationAdmission
from app.runtime.extensions.plugin.lifecycle import PluginLifecycle
from app.runtime.extensions.plugin.manager import PluginManager
from app.runtime.extensions.plugin.registry import PluginRegistry
from app.runtime.extensions.plugin.sync import PluginSyncService
from app.schemas.exception import PluginMutationRejectedError
from app.schemas.plugin import PluginRuntimeStatus
from app.schemas.types import EventType
from app.startup.initializers import plugins as plugins_initializer


class _RecoveredPlugin:
    """
    为恢复后的插件提供可观察的最小生命周期，不启动外部任务。
    """

    plugin_name = "待恢复插件"
    plugin_version = "1.0.0"

    def init_plugin(self, config: dict[str, object]) -> None:
        """
        接收启动配置，避免测试装配任何真实插件服务。
        """
        self.config = config

    def get_state(self) -> bool:
        """
        把已恢复实例声明为启用，验证事件和运行态激活路径。
        """
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "returned", "raised"])
async def test_startup_sync_retains_owner_and_recovers_plugins_serially(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: Optional[str],
) -> None:
    """
    真实同步、Gateway 和安装命令复用启动事务，单项失败后继续恢复其余插件。

    仅替代市场、文件下载和数据库端口，保留生产线程桥接、变更准入及生命周期。
    事务内主动让出事件循环，确保批量恢复不会交叠安装或放行外部竞争者。
    """
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(global_vars, "CURRENT_EVENT_LOOP", loop)
    plugin_ids = [f"RecoveredPlugin{index}" for index in range(7)]
    failed_id = plugin_ids[2] if failure else None
    registry = PluginRegistry()
    builtin = object()
    registry.running["BuiltinPlugin"] = builtin
    registry.set_runtime_status("BuiltinPlugin", PluginRuntimeStatus.ACTIVE)
    plugin_classes = {
        plugin_id: type(plugin_id, (_RecoveredPlugin,), {
            "__doc__": "用于验证独立恢复身份的插件类。",
        })
        for plugin_id in plugin_ids
    }
    lifecycle = PluginLifecycle(
        classes=registry.classes, running=registry.running,
        load_plugins=lambda plugin_id, *_args: [plugin_classes[plugin_id]],
        loadable_plugins=lambda: plugin_ids,
        plugin_config=lambda _plugin_id: {}, auth_checker=lambda _plugin: True,
        clear_modules=Mock(), clear_tools=Mock(),
        enable_events=Mock(), disable_events=Mock(),
        runtime_status_writer=registry.set_runtime_status,
        runtime_compatible=lambda _plugin_id: True,
        database=Mock(), log=Mock(), event_sender=Mock(),
    )
    manager = object.__new__(PluginManager)
    admission = PluginMutationAdmission(activity=lifecycle.activity)
    manager._plugin_mutation_admission = admission
    attempts: list[str] = []
    active_installs: set[str] = set()

    def competing_mutation() -> None:
        """
        模拟没有启动事务身份的外部更新，必须在副作用前被拒绝。
        """
        with manager.mutation("外部强制更新"):
            pytest.fail("启动恢复期间错误放行了外部插件变更")

    async def install_package(*, plugin_id: str, **_kwargs: Any) -> tuple[bool, str]:
        """
        在隔离目录模拟下载，并验证安装上下文和不同插件之间的串行约束。
        """
        assert admission.is_held()
        assert not active_installs, "不同插件安装事务发生交叠"
        active_installs.add(plugin_id)
        attempts.append(plugin_id)
        try:
            with pytest.raises(PluginMutationRejectedError, match="插件变更正忙"):
                Context().run(competing_mutation)
            await asyncio.sleep(0)
            if plugin_id == failed_id:
                if failure == "raised":
                    raise OSError("模拟插件下载异常")
                return False, "模拟插件下载失败"
            plugin_dir = tmp_path / plugin_id.lower()
            plugin_dir.mkdir()
            (plugin_dir / "__init__.py").write_text("# 隔离的恢复载荷\n", encoding="utf-8")
            return True, "恢复成功"
        finally:
            active_installs.remove(plugin_id)

    async def reload_target(plugin_id: str) -> PluginRuntimeStatus:
        """
        在线程中执行真实插件停启，并沿用安装命令持有的事务身份。
        """
        def reload_plugin() -> PluginRuntimeStatus:
            """
            在 Manager 相同的变更边界内重载一个恢复后的插件。
            """
            with manager.mutation("重新加载插件"):
                return lifecycle.reload(plugin_id, EventType.PluginReload)

        return await asyncio.to_thread(reload_plugin)

    packages = SimpleNamespace(
        async_checkpoint=AsyncMock(return_value=SimpleNamespace(
            plugin_existed=False, persistent_backup_existed=False,
        )),
        async_install=install_package,
        async_restore=AsyncMock(), async_cleanup=AsyncMock(),
        async_stage_persistent_backup=AsyncMock(),
        async_activate_persistent_backup=AsyncMock(),
        async_finalize_persistent_backup=AsyncMock(), async_commit=AsyncMock(),
        async_payload_receipt=AsyncMock(return_value="sha256:" + "0" * 64),
        async_native_dependency_changes=AsyncMock(return_value=()),
    )
    persistence = AsyncMock()
    command = PluginInstallCommand(
        persistence=persistence,
        installed_plugins_reader=lambda: ["BuiltinPlugin", *plugin_ids],
        loadable_marker=Mock(), host_enabled_reader=lambda _plugin_id: True,
        host_enabled_restorer=AsyncMock(), plugin_ids_provider=lambda: [],
        packages=packages, install_reporter=AsyncMock(return_value=True),
        target_reloader=reload_target, rollback_reloader=reload_target,
        registration_refresher=AsyncMock(), mutation=manager.mutation,
        package_write_guard=lambda _plugin_id: nullcontext(),
        restart_required_recorder=Mock(),
        clock=lambda: datetime.now(timezone.utc),
        transaction_id_factory=lambda: uuid4().hex,
    )
    repo_url = "https://github.com/jxxghp/MoviePilot-Plugins"
    candidates = tuple(PluginMarketCandidate(
        plugin_id=plugin_id, source_key="github:jxxghp/moviepilot-plugins",
        source_type=TrustedPluginSourceType.OFFICIAL, repo_url=repo_url,
        package_generation="v3", plugin_version="1.0.0", dto={"v3": True},
    ) for plugin_id in plugin_ids)
    inventory = CandidateInventory((MarketRead.present(
        repo_url, candidates, package_generation="v3",
    ),))
    gateway = PluginInstallGateway(
        inventory=AsyncMock(return_value=inventory), identity=AsyncMock(return_value=None),
        candidate_compatibility=lambda _candidate: (True, ""), executor=command,
        clock=lambda: datetime.now(timezone.utc), source_plugin_id=lambda plugin_id: plugin_id,
    )

    def install(
        plugin_id: str, repo_url: Optional[str], force: bool,
        startup_token: Optional[PluginStartupLease],
    ) -> tuple[bool, str]:
        """
        复用生产同步兼容入口，把工作线程的安装请求提交回宿主事件循环。
        """
        return plugins_initializer._run_plugin_install_sync(
            gateway, plugin_id=plugin_id, repo_url=repo_url or "", package_version="v3",
            release_version=None, force=force, local_sync=False, explicit_source=False,
            startup_token=startup_token,
        )

    manager._plugin_sync = PluginSyncService(
        frozen=lambda: False, installed_plugins=lambda: ["BuiltinPlugin", *plugin_ids],
        online_plugins=lambda: [SimpleNamespace(
            id=plugin_id, plugin_name=plugin_id, repo_url=repo_url,
            runtime_compatible=True, system_version_compatible=True,
        ) for plugin_id in plugin_ids],
        local_plugins=lambda: [], merge_plugins=lambda items, *_args: items,
        plugin_exists=lambda plugin_id, _version: plugin_id == "builtinplugin",
        install=install, runtime_status_writer=registry.set_runtime_status, log=Mock(),
    )
    with manager.mutation("启动后同步插件"):
        async with plugin_lifecycle.hold_startup() as startup_token:
            synced = await asyncio.wait_for(plugins_initializer.execute_task(
                loop, lambda: manager.sync(startup_token), "插件同步到本地",
            ), timeout=5)
        assert admission.active_count == 1

    expected = [plugin_id for plugin_id in plugin_ids if plugin_id != failed_id]
    assert synced == expected
    assert attempts == plugin_ids
    assert registry.running["BuiltinPlugin"] is builtin
    assert set(registry.running) == {"BuiltinPlugin", *expected}
    assert all(registry.runtime_status(plugin_id) is PluginRuntimeStatus.ACTIVE for plugin_id in expected)
    assert all((tmp_path / plugin_id.lower() / "__init__.py").is_file() for plugin_id in expected)
    assert persistence.commit_installation.await_count == len(expected)
    assert admission.active_count == 0
    assert admission.activity() is None
    if failed_id:
        assert registry.runtime_status(failed_id) is PluginRuntimeStatus.SYNC_FAILED
        assert not (tmp_path / failed_id.lower()).exists()
    with manager.mutation("恢复结束后的外部更新"):
        assert admission.is_held()
