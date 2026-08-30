"""通用系统设置入口的插件 mutation 准入测试。"""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.tools.impl.update_system_settings import UpdateSystemSettingsTool
from app.api.endpoints import system as system_endpoint
from app.application.plugin import runtime as plugin_runtime
from app.runtime.extensions.plugin.admission import PluginMutationAdmission
from app.schemas.types import SystemConfigKey
from app.startup.composition.system import compose_system_service

PLUGIN_RUNTIME_KEYS = (
    SystemConfigKey.UserInstalledPlugins,
    SystemConfigKey.PluginInstances,
    SystemConfigKey.PluginFolders,
)


def _runtime(config, *, rule_group_mutation=None):
    """构造注入指定配置仓储的最小系统运行时。"""
    return SimpleNamespace(
        system=compose_system_service(
            settings=SimpleNamespace(contains=lambda _key: False),
            system_config=config,
            rule_group_mutation=rule_group_mutation or SimpleNamespace(),
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("config_key", PLUGIN_RUNTIME_KEYS)
async def test_system_http_rejects_plugin_keys_after_admission_seal(
    config_key: SystemConfigKey,
    monkeypatch,
) -> None:
    """HTTP 通用设置入口在封口后不得写入插件运行态配置。"""
    admission = PluginMutationAdmission()
    admission.seal()
    config = MagicMock()
    config.async_set = AsyncMock()
    monkeypatch.setattr(
        plugin_runtime,
        "get_plugin_manager",
        lambda: SimpleNamespace(mutation=admission.hold),
    )
    response = await system_endpoint.set_setting(
        config_key.value, {}, None, runtime=_runtime(config)
    )

    assert response.success is False
    assert "停机阶段" in response.message
    config.async_set.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("config_key", PLUGIN_RUNTIME_KEYS)
async def test_agent_rejects_plugin_keys_after_admission_seal(
    config_key: SystemConfigKey,
    monkeypatch,
) -> None:
    """Agent 通用设置入口在封口后不得读取或写入插件运行态配置。"""
    admission = PluginMutationAdmission()
    admission.seal()
    config = MagicMock()
    config.async_set = AsyncMock()
    monkeypatch.setattr(
        plugin_runtime,
        "get_plugin_manager",
        lambda: SimpleNamespace(mutation=admission.hold),
    )
    tool = UpdateSystemSettingsTool(
        session_id="session-1",
        user_id="10001",
        system_config=config,
    )

    payload = json.loads(await tool.run(setting_key=config_key.value, value={}))

    assert payload["success"] is False
    assert "停机阶段" in payload["message"]
    config.get.assert_not_called()
    config.async_set.assert_not_awaited()


@pytest.mark.asyncio
async def test_system_http_plugin_write_remains_owned_until_settled(
    monkeypatch,
) -> None:
    """HTTP 在途插件配置写入完成前，封口等待不得误判为空闲。"""
    admission = PluginMutationAdmission()
    write_started = asyncio.Event()
    write_release = asyncio.Event()

    async def async_set(_key: str, _value: object) -> bool:
        """阻塞配置写入，暴露 quiesce 等待窗口。"""
        write_started.set()
        await write_release.wait()
        return True

    config = MagicMock()
    config.async_set = AsyncMock(side_effect=async_set)
    monkeypatch.setattr(
        plugin_runtime,
        "get_plugin_manager",
        lambda: SimpleNamespace(mutation=admission.hold),
    )
    monkeypatch.setattr(
        "app.startup.composition.system.eventmanager.async_send_event", AsyncMock()
    )

    task = asyncio.create_task(
        system_endpoint.set_setting(
            SystemConfigKey.UserInstalledPlugins.value,
            ["DemoPlugin"],
            None,
            runtime=_runtime(config),
        )
    )
    await write_started.wait()
    assert admission.seal() == 1
    idle_waiter = asyncio.create_task(asyncio.to_thread(admission.wait_until_idle))
    await asyncio.sleep(0.02)
    assert idle_waiter.done() is False

    write_release.set()
    response = await task
    await idle_waiter
    assert response.success is True
    assert admission.active_count == 0


@pytest.mark.asyncio
async def test_agent_plugin_write_remains_owned_until_saved_value_read(
    monkeypatch,
) -> None:
    """Agent 插件配置事务在写入和结果读取完成前持续持有 lease。"""
    admission = PluginMutationAdmission()
    write_started = asyncio.Event()
    write_release = asyncio.Event()

    async def async_set(_key: SystemConfigKey, _value: object) -> bool:
        """阻塞 Agent 配置写入，暴露 quiesce 等待窗口。"""
        write_started.set()
        await write_release.wait()
        return True

    config = MagicMock()
    config.get.side_effect = [[], ["DemoPlugin"]]
    config.async_set = AsyncMock(side_effect=async_set)
    monkeypatch.setattr(
        plugin_runtime,
        "get_plugin_manager",
        lambda: SimpleNamespace(mutation=admission.hold),
    )
    monkeypatch.setattr(
        "app.agent.tools.impl.update_system_settings.eventmanager.async_send_event",
        AsyncMock(),
    )
    tool = UpdateSystemSettingsTool(
        session_id="session-1",
        user_id="10001",
        system_config=config,
    )

    task = asyncio.create_task(
        tool.run(
            setting_key=SystemConfigKey.UserInstalledPlugins.value,
            value=["DemoPlugin"],
        )
    )
    await write_started.wait()
    assert admission.seal() == 1
    idle_waiter = asyncio.create_task(asyncio.to_thread(admission.wait_until_idle))
    await asyncio.sleep(0.02)
    assert idle_waiter.done() is False

    write_release.set()
    payload = json.loads(await task)
    await idle_waiter
    assert payload["success"] is True
    assert payload["saved_value"] == ["DemoPlugin"]
    assert admission.active_count == 0


@pytest.mark.asyncio
async def test_non_plugin_system_config_does_not_resolve_plugin_runtime(
    monkeypatch,
) -> None:
    """非插件 SystemConfig 写入保持原路径且不依赖插件组合根。"""
    config = MagicMock()
    config.async_set = AsyncMock(return_value=True)

    def fail_runtime_resolution():
        """若非插件配置误取插件运行时则立即暴露回归。"""
        raise AssertionError("非插件配置不应解析插件运行时")

    monkeypatch.setattr(plugin_runtime, "get_plugin_manager", fail_runtime_resolution)
    monkeypatch.setattr(
        "app.startup.composition.system.eventmanager.async_send_event", AsyncMock()
    )

    response = await system_endpoint.set_setting(
        SystemConfigKey.Directories.value,
        [],
        None,
        runtime=_runtime(config),
    )

    assert response.success is True
    config.async_set.assert_awaited_once_with(SystemConfigKey.Directories.value, None)


@pytest.mark.asyncio
async def test_rule_group_setting_reconciles_stale_references_when_unchanged(
    monkeypatch,
) -> None:
    """重复保存规则组也应按有效名称对账，修复旧版本遗留的悬空订阅引用。"""
    config = MagicMock()
    definitions = [{"name": "keep", "rule_string": "4K"}]
    config.get.return_value = definitions
    config.async_set = AsyncMock()
    mutation = MagicMock()
    mutation.apply = AsyncMock()

    @asynccontextmanager
    async def mutation_scope():
        """提供可观测的异步规则组事务作用域。"""
        yield mutation

    send_event = AsyncMock()
    monkeypatch.setattr(
        "app.startup.composition.system.eventmanager.async_send_event", send_event
    )
    runtime = _runtime(config, rule_group_mutation=mutation_scope)

    response = await system_endpoint.set_setting(
        SystemConfigKey.UserFilterRuleGroups.value,
        definitions,
        None,
        runtime=runtime,
    )

    assert response.success is True
    mutation.apply.assert_awaited_once_with(
        definitions,
        expected_rule_groups=definitions,
    )
    config.async_set.assert_not_awaited()
    send_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_non_plugin_config_does_not_resolve_plugin_runtime(
    monkeypatch,
) -> None:
    """Agent 非插件配置写入保持既有读写和事件语义。"""
    config = MagicMock()
    config.get.side_effect = [{}, {"chatgpt": {"enabled": True}}]
    config.async_set = AsyncMock(return_value=True)

    def fail_runtime_resolution():
        """若 Agent 非插件配置误取插件运行时则立即暴露回归。"""
        raise AssertionError("非插件配置不应解析插件运行时")

    monkeypatch.setattr(plugin_runtime, "get_plugin_manager", fail_runtime_resolution)
    monkeypatch.setattr(
        "app.agent.tools.impl.update_system_settings.eventmanager.async_send_event",
        AsyncMock(),
    )
    tool = UpdateSystemSettingsTool(
        session_id="session-1",
        user_id="10001",
        system_config=config,
    )

    payload = json.loads(
        await tool.run(
            setting_key=SystemConfigKey.AIAgentConfig.value,
            value={"chatgpt": {"enabled": True}},
        )
    )

    assert payload["success"] is True
    assert payload["changed"] is True
    config.async_set.assert_awaited_once()
