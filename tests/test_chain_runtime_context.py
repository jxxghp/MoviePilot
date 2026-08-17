"""Chain 运行上下文注入和无参兼容 provider 测试。"""

from unittest.mock import Mock

from app.application.chain.context import ChainRuntimeContext
from app.application.chain import context as chain_context
from app.chain import ChainBase


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
    )


def test_chain_accepts_explicit_runtime_context() -> None:
    """新代码应能显式注入最小运行时依赖而不创建真实管理器。"""
    context = _context()

    chain = ChainBase(context)

    assert chain.modulemanager is context.module_manager
    assert chain.pluginmanager is context.plugin_manager
    assert chain.eventmanager is context.event_manager
    assert chain.messagehelper is context.message_helper
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
