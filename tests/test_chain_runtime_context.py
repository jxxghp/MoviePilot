"""Chain 运行上下文注入和无参兼容 provider 测试。"""

import asyncio
import sys
from dataclasses import replace
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, call

import pytest

from app.application.chain import context as chain_context
from app.application.chain.context import ChainRuntimeContext
from app.application.configuration import ChainRuntimeConfig
from app.application.transfer.execution import TransferPlanningRejectedError
from app.chain.base import ChainBase
from app.runtime.extensions.module.dispatcher import ModuleInvocationDispatcher
from app.schemas.types import EventType
from app.startup.composition.runtime import RuntimeDependencies


def _context() -> ChainRuntimeContext:
    """构造不连接数据库、不启动线程的最小 Chain 上下文。"""
    message_queue = Mock()
    message_queue.bind.return_value = Mock()
    return ChainRuntimeContext(
        module_manager=Mock(),
        plugin_manager=Mock(),
        event_manager=Mock(),
        message_oper=Mock(),
        message_helper=Mock(),
        file_cache=Mock(),
        async_file_cache=Mock(),
        message_queue=message_queue,
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
        system_service=Mock(),
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
    assert chain.system_service is context.system_service
    context.message_queue.bind.assert_called_once_with(chain.run_module)


def _failing_chain(error: Exception, source: str) -> ChainBase:
    """注入会抛出指定异常的同步、异步模块，覆盖真实调度到通知的链路。"""
    def fail(**_kwargs):
        """模拟同步模块执行失败。"""
        raise error

    async def async_fail(**_kwargs):
        """模拟异步模块执行失败。"""
        raise error

    context = _context()
    context.plugin_manager.get_plugin_modules.return_value = {}
    context.module_manager.get_running_modules.return_value = []
    if source == "plugin":
        context.plugin_manager.get_plugin_modules.return_value = {
            ("TestPlugin", "测试插件"): {"fail": fail, "async_fail": async_fail},
        }
    else:
        context.module_manager.get_running_modules.return_value = [
            SimpleNamespace(
                get_name=Mock(return_value="测试模块"),
                get_priority=Mock(return_value=1),
                fail=fail,
                async_fail=async_fail,
            ),
        ]
    return ChainBase(context)


@pytest.mark.parametrize("source", ["system", "plugin"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    ("error", "expected_reason"),
    [
        (RuntimeError("无法连接媒体服务器\nConnection refused"), "无法连接媒体服务器\nConnection refused"),
        (TimeoutError(), "TimeoutError"),
        (ValueError("  \n "), "ValueError"),
    ],
)
def test_module_error_notification_preserves_reason(source, asynchronous, error, expected_reason) -> None:
    """系统与插件的同步、异步失败通知应携带调用位置和错误原因，空异常使用类型兜底。"""
    chain = _failing_chain(error, source)
    method = "async_fail" if asynchronous else "fail"

    result = asyncio.run(chain.async_run_module(method)) if asynchronous else chain.run_module(method)

    assert result is None
    source_id = "TestPlugin" if source == "plugin" else "SimpleNamespace"
    chain.messagehelper.put.assert_called_once_with(
        title="测试插件 运行失败" if source == "plugin" else "测试模块运行失败",
        message=f"{source_id}.{method}：{expected_reason}",
        role=source,
        level="error",
    )
    chain.eventmanager.send_event.assert_called_once()
    event_type, payload = chain.eventmanager.send_event.call_args.args
    assert event_type == EventType.SystemError
    assert payload["error"] == str(error)
    assert "Traceback (most recent call last)" in payload["traceback"]
    assert type(error).__name__ in payload["traceback"]


@pytest.mark.parametrize("source", ["system", "plugin"])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_module_explicit_error_propagation_does_not_notify(source, asynchronous) -> None:
    """显式要求传播异常时，保持原异常对象且不重复生成通知或错误事件。"""
    error = RuntimeError("连接被拒绝")
    chain = _failing_chain(error, source)

    with pytest.raises(RuntimeError) as caught:
        if asynchronous:
            asyncio.run(chain.async_run_module("async_fail", raise_exception=True))
        else:
            chain.run_module("fail", raise_exception=True)

    assert caught.value is error
    chain.messagehelper.put.assert_not_called()
    chain.eventmanager.send_event.assert_not_called()


def test_module_business_rejection_is_not_reported_as_system_error() -> None:
    """模块按业务规则拒绝属于预期结果，不生成错误通知和事件，严格调用仍把原异常交给调用方结算。"""
    error = TransferPlanningRejectedError("未找到有效的媒体库目录")
    chain = _failing_chain(error, "system")

    assert chain.run_module("fail") is None
    assert asyncio.run(chain.async_run_module("async_fail")) is None
    with pytest.raises(TransferPlanningRejectedError) as caught:
        chain.run_module_strict("fail")

    assert caught.value is error
    chain.messagehelper.put.assert_not_called()
    chain.eventmanager.send_event.assert_not_called()


def test_chains_bind_distinct_callbacks_without_starting_queue() -> None:
    """多个 Chain 只创建轻量客户端，不得启动或覆盖共享队列 owner。"""
    context = _context()

    first = ChainBase(context)
    second = ChainBase(context)

    assert context.message_queue.start.call_count == 0
    assert context.message_queue.bind.call_args_list == [
        call(first.run_module),
        call(second.run_module),
    ]


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


def test_chain_runtime_config_provider_refreshes_compatibility_singleton() -> None:
    """兼容单例 Chain 应在配置保存后读取新的运行时配置。"""
    context = _context()
    state = {"configuration": context.configuration}
    context = replace(
        context,
        configuration_provider=lambda: state["configuration"],
    )

    chain = ChainBase(context)
    assert chain.runtime_config.media_extensions == (".mkv",)

    state["configuration"] = replace(
        context.configuration,
        media_extensions=(".mp4",),
    )
    assert chain.runtime_config.media_extensions == (".mp4",)

    pinned = replace(chain.runtime_config, media_extensions=(".avi",))
    chain.runtime_config = pinned
    state["configuration"] = replace(
        context.configuration,
        media_extensions=(".mov",),
    )
    assert chain.runtime_config.media_extensions == (".avi",)


def test_chain_composition_registers_lazy_compatibility_provider(monkeypatch) -> None:
    """组合 API 只登记 provider，首次无参 Chain 请求时才构造完整上下文。"""
    from app.startup.composition import chain as chain_composition

    context = _context()
    builder = Mock(return_value=context)
    register = Mock()
    runtime_dependencies = RuntimeDependencies(
        download_history=Mock(),
        transfer_history=Mock(),
        site=Mock(),
        subscription=Mock(),
        subscription_history=Mock(),
        transfer_execution=Mock(),
        message_helper=Mock(),
        message_queue=Mock(),
    )
    inputs = {
        "dependencies": runtime_dependencies,
        "system_config": Mock(),
        "configuration": Mock(),
        "classification_service": Mock(),
        "system_service": Mock(),
    }
    monkeypatch.setattr(chain_composition, "build_chain_runtime_context", builder)
    monkeypatch.setattr(
        chain_composition,
        "configure_chain_runtime_context_provider",
        register,
    )

    chain_composition.configure_chain_runtime_context(**inputs)

    builder.assert_not_called()
    provider = register.call_args.args[0]
    assert provider() is context
    builder.assert_called_once_with(**inputs)


def test_chain_composition_keeps_legacy_transfer_import_lazy(monkeypatch) -> None:
    """旧整理命令仅在真实调用时加载 TransferChain，并原样转交参数。"""
    from app.startup.composition import chain as chain_composition

    execute = Mock(return_value="transferred")
    transfer_chain = Mock(return_value=Mock(execute_legacy_transfer_command=execute))
    facade = ModuleType("app.chain.transfer.facade")
    facade.TransferChain = transfer_chain
    monkeypatch.setitem(sys.modules, "app.chain.transfer.facade", facade)

    result = chain_composition.execute_legacy_transfer_command(
        source="downloads",
        target="library",
    )

    assert result == "transferred"
    transfer_chain.assert_called_once_with()
    execute.assert_called_once_with(source="downloads", target="library")


def test_chain_composition_registers_lazy_wallpaper_providers(monkeypatch) -> None:
    """壁纸装配只登记 callable，不在启动阶段提前物化业务 Chain。"""
    from app.startup.composition import chain as chain_composition

    register = Mock()
    tmdb_instance = Mock()
    tmdb_instance.get_random_wallpager.return_value = "tmdb-one"
    tmdb_instance.get_trending_wallpapers.return_value = ["tmdb-many"]
    media_instance = Mock()
    media_instance.get_latest_wallpaper.return_value = "media-one"
    media_instance.get_latest_wallpapers.return_value = ["media-many"]
    tmdb_chain = Mock(return_value=tmdb_instance)
    media_chain = Mock(return_value=media_instance)
    monkeypatch.setattr(chain_composition, "configure_wallpaper_providers", register)
    monkeypatch.setattr(chain_composition, "TmdbChain", tmdb_chain)
    monkeypatch.setattr(chain_composition, "MediaServerChain", media_chain)

    chain_composition.configure_wallpaper_services()

    tmdb_chain.assert_not_called()
    media_chain.assert_not_called()
    providers = register.call_args.kwargs
    assert providers["tmdb_wallpaper"]() == "tmdb-one"
    assert providers["tmdb_wallpapers"](3) == ["tmdb-many"]
    assert providers["mediaserver_wallpaper"]() == "media-one"
    assert providers["mediaserver_wallpapers"](4) == ["media-many"]
    tmdb_instance.get_trending_wallpapers.assert_called_once_with(3)
    media_instance.get_latest_wallpapers.assert_called_once_with(count=4)
