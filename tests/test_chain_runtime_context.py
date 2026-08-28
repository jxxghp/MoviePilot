"""Chain 运行上下文注入和无参兼容 provider 测试。"""

from unittest.mock import Mock

import pytest

from app.application.chain import context as chain_context
from app.application.chain.context import ChainRuntimeContext
from app.application.configuration import ChainRuntimeConfig
from app.chain import ChainBase
from app.runtime.extensions.module.dispatcher import ModuleInvocationDispatcher


def _context() -> ChainRuntimeContext:
    """构造不连接数据库、不启动线程的最小 Chain 上下文。"""
    return ChainRuntimeContext(
        module_manager=Mock(),
        plugin_manager=Mock(),
        event_manager=Mock(),
        message_oper=Mock(),
        message_helper=Mock(),
        file_cache=Mock(),
        async_file_cache=Mock(),
        message_queue_factory=Mock(return_value=Mock()),
        module_dispatcher_factory=ModuleInvocationDispatcher,
        site_repository=Mock(),
        subscription_repository=Mock(),
        subscription_mutation_scope=Mock(),
        sync_subscription_mutation_scope=Mock(),
        subscription_delete_scope=Mock(),
        sync_subscription_delete_scope=Mock(),
        subscription_completion_scope=Mock(),
        rule_group_mutation_scope=Mock(),
        site_reference_mutation_scope=Mock(),
        download_history_repository=Mock(),
        transfer_history_repository=Mock(),
        transfer_admission_repository=Mock(),
        transfer_execution_repository=Mock(),
        media_server_repository=Mock(),
        download_failure_repository=Mock(),
        user_repository=Mock(),
        configuration=ChainRuntimeConfig(media_extensions=(".mkv",)),
        durable_event_writer=Mock(),
    )


def test_chain_accepts_explicit_runtime_context() -> None:
    """新代码应能显式注入最小运行时依赖而不创建真实管理器。"""
    context = _context()

    chain = ChainBase(context)

    assert chain.modulemanager is context.module_manager
    assert chain.pluginmanager is context.plugin_manager
    assert chain.eventmanager is context.event_manager
    assert chain.messagehelper is context.message_helper
    assert chain.durable_event_writer is context.durable_event_writer
    context.message_queue_factory.assert_called_once_with(chain.run_module)


def test_no_arg_chain_uses_compatibility_context_provider(monkeypatch) -> None:
    """V3 兼容期内无参 Chain() 应从组合根 provider 获取相同上下文。"""
    context = _context()
    provider = Mock(return_value=context)
    monkeypatch.setattr(chain_context, "_context_provider", provider)

    chain = ChainBase()

    provider.assert_called_once_with()
    assert chain.modulemanager is context.module_manager
    assert chain.pluginmanager is context.plugin_manager


def test_chain_runtime_context_rejects_unconfigured_provider(monkeypatch) -> None:
    """未由组合根配置运行上下文时必须显式拒绝无参 Chain。"""
    monkeypatch.setattr(
        chain_context,
        "_context_provider",
        chain_context._unconfigured_chain_runtime_context,
    )

    with pytest.raises(RuntimeError, match="Chain 运行上下文尚未由启动组合根配置"):
        chain_context.get_chain_runtime_context()


def test_chain_keeps_explicit_typed_repositories() -> None:
    """Chain 实例必须直接保存组合根注入的数据端口，不再查找全局注册表。"""
    context = _context()

    chain = ChainBase(context)

    assert chain.site_repository is context.site_repository
    assert chain.subscription_repository is context.subscription_repository
    assert chain.subscription_mutation_scope is context.subscription_mutation_scope
    assert chain.sync_subscription_mutation_scope is context.sync_subscription_mutation_scope
    assert chain.subscription_delete_scope is context.subscription_delete_scope
    assert chain.sync_subscription_delete_scope is context.sync_subscription_delete_scope
    assert chain.subscription_completion_scope is context.subscription_completion_scope
    assert chain.transfer_execution_repository is context.transfer_execution_repository
    assert chain.user_repository is context.user_repository
