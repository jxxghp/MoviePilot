"""内建模块与市场插件满足同一 Extension 契约的验证。"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple, Union
from unittest.mock import Mock

import pytest

from app.modules import _ModuleBase
from app.sdk.extension import _PluginBase
from app.runtime.extensions.contract.extension import (
    ExtensionDistribution,
    ExtensionFaultScope,
    ExtensionProvider,
    ExtensionProviderSource,
    ExtensionView,
    extension_capability_names,
    supports_extension_hook,
)
from app.runtime.extensions.lifecycle.host_module_adapter import (
    HostModuleExtension,
    HostModuleProviderSource,
)
from app.runtime.extensions.projection.dispatcher import ModuleInvocationDispatcher
from app.runtime.extensions.projection.plugin import (
    PluginExtension,
    PluginProviderSource,
)
from app.schemas.exception import RateLimitExceededException
from app.schemas.types import OtherModulesType


CONTRACT_MEMBERS = (
    "extension_id",
    "display_name",
    "distribution",
    "priority",
    "is_enabled",
    "initialize",
    "terminate",
    "self_test",
    "supports_hook",
    "capability_names",
    "capability",
)


class DemoModule(_ModuleBase):
    """记录生命周期调用的内建模块。"""

    def __init__(self) -> None:
        """初始化生命周期事件记录。"""
        super().__init__()
        self.events: list[str] = []

    def init_module(self) -> None:
        """记录一次模块初始化。"""
        self.events.append("init_module")

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        """声明模块开关设置。"""
        return "DEMO_MODULE", True

    def stop(self) -> None:
        """记录一次模块停止。"""
        self.events.append("stop")

    def test(self) -> Optional[Tuple[bool, str]]:
        """返回固定的连通性自检结果。"""
        return True, "ok"

    def recognize_media(self, title: str) -> str:
        """返回内建模块识别到的媒体标题。"""
        return f"builtin:{title}"

    def unimplemented_capability(self) -> None:
        """声明但未实现的模块方法。"""
        pass

    @staticmethod
    def get_name() -> str:
        """返回模块展示名。"""
        return "内建演示模块"

    @staticmethod
    def get_subtype() -> OtherModulesType:
        """返回模块子类型。"""
        return OtherModulesType.Other

    @staticmethod
    def get_priority() -> int:
        """返回模块优先级。"""
        return 10


class DemoPlugin(_PluginBase):
    """记录生命周期调用的市场插件。"""

    plugin_name = "市场演示插件"
    plugin_order = 21

    def __init__(self) -> None:
        """建立插件自身状态，不牵入宿主数据库与处理链。"""
        self.events: list[str] = []
        self.config: Optional[dict] = None
        self.enabled = True

    def init_plugin(self, config: dict = None) -> None:
        """记录一次插件配置生效。"""
        self.events.append("init_plugin")
        self.config = config

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self.enabled

    def get_api(self) -> list:
        """插件不注册 API。"""
        return []

    def get_form(self) -> Tuple[Optional[list], Dict[str, Any]]:
        """插件不提供配置表单。"""
        return None, {}

    def get_page(self) -> Optional[list]:
        """插件不提供详情页。"""
        return None

    def get_module(self) -> Dict[str, Any]:
        """声明插件注入的模块方法表。"""
        return {"recognize_media": self.recognize_media}

    def recognize_media(self, title: str) -> str:
        """返回插件识别到的媒体标题。"""
        return f"market:{title}"

    def unimplemented_capability(self) -> None:
        """声明但未实现的插件方法。"""
        pass

    def close(self) -> None:
        """记录一次数据库连接关闭。"""
        self.events.append("close")

    def stop_service(self) -> None:
        """记录一次插件服务停止。"""
        self.events.append("stop_service")


class _ModuleCatalog:
    """按扩展契约暴露运行中内建模块的内存目录。"""

    def __init__(self, modules: list) -> None:
        """保存参与分发的内建模块。"""
        self.modules = modules

    def get_running_modules(self, method: str) -> list:
        """返回实现了指定方法的内建模块。"""
        return [
            module for module in self.modules
            if supports_extension_hook(module, method)
        ]

    def providers_for(self, method: str) -> tuple:
        """返回按优先级升序排列的内建模块。"""
        return tuple(
            sorted(
                self.get_running_modules(method),
                key=lambda module: HostModuleExtension(module).priority,
            )
        )


class _PluginCatalog:
    """按扩展契约暴露运行态插件方法表的内存目录。"""

    def __init__(self, plugins: dict) -> None:
        """保存插件 ID 到插件实例的映射。"""
        self.plugins = plugins

    def get_plugin_modules(self) -> dict:
        """把插件视图声明的方法表投影为分发目录快照。"""
        return {
            (extension.extension_id, extension.display_name):
                extension.capability_table()
            for extension in (
                PluginExtension(plugin, plugin_id)
                for plugin_id, plugin in self.plugins.items()
            )
        }


class _RemoteSource:
    """按扩展契约接入的第三种发行方式目录。"""

    distribution = ExtensionDistribution.MARKET

    def __init__(self, extension_id: str, method: str, func) -> None:
        """保存远程扩展的标识、方法名与调用入口。"""
        self._extension_id = extension_id
        self._method = method
        self._func = func
        self.announced: list[str] = []

    def announce_phase(self, method: str) -> None:
        """记录一次阶段日志调用。"""
        self.announced.append(method)

    def _providers(self, method: str):
        """产出远程扩展的提供者记录。"""
        if method != self._method:
            return
        yield ExtensionProvider(
            extension_id=self._extension_id,
            display_name="远程扩展",
            distribution=ExtensionDistribution.MARKET,
            fault_scope=ExtensionFaultScope.PLUGIN,
            invoke=self._func,
        )

    def notify_providers(self, method: str):
        """返回应被通知的远程提供者。"""
        return self._providers(method)

    def answer_providers(self, method: str):
        """返回参与仲裁的远程提供者。"""
        return self._providers(method)


def _views() -> tuple[HostModuleExtension, PluginExtension]:
    """构造内建模块与市场插件各自的扩展视图。

    :return: `(内建模块视图, 市场插件视图)`
    """
    return HostModuleExtension(DemoModule()), PluginExtension(DemoPlugin(), "DemoPlugin")


def _dispatcher(
    *,
    modules: Optional[list] = None,
    plugins: Optional[dict] = None,
    extra_sources: tuple = (),
) -> tuple[ModuleInvocationDispatcher, Mock, Mock, Mock]:
    """构造完全内存化的调度器及错误策略替身。

    :param modules: 参与分发的内建模块
    :param plugins: 参与分发的市场插件
    :param extra_sources: 追加的扩展目录
    :return: `(调度器, 插件错误策略, 系统错误策略, 限流策略)`
    """
    plugin_error = Mock()
    system_error = Mock()
    rate_error = Mock()
    dispatcher = ModuleInvocationDispatcher(
        module_catalog=_ModuleCatalog(modules or []),
        plugin_catalog=_PluginCatalog(plugins or {}),
        plugin_error_handler=plugin_error,
        system_error_handler=system_error,
        rate_limit_handler=rate_error,
        extra_sources=extra_sources,
    )
    return dispatcher, plugin_error, system_error, rate_error


def test_builtin_and_market_extensions_satisfy_the_same_contract() -> None:
    """两种发行方式的视图都必须结构化满足同一 Extension 契约。"""
    module_view, plugin_view = _views()

    for view in (module_view, plugin_view):
        assert isinstance(view, ExtensionView)
        for member in CONTRACT_MEMBERS:
            assert hasattr(view, member), member

    assert module_view.distribution is ExtensionDistribution.BUILTIN
    assert plugin_view.distribution is ExtensionDistribution.MARKET


def test_contract_reads_identity_from_both_release_channels() -> None:
    """身份与仲裁顺序经契约取用时，两种发行方式给出各自的声明值。"""
    module_view, plugin_view = _views()

    assert module_view.extension_id == "DemoModule"
    assert module_view.display_name == "内建演示模块"
    assert module_view.priority == 10

    assert plugin_view.extension_id == "DemoPlugin"
    assert plugin_view.display_name == "市场演示插件"
    assert plugin_view.priority == 21


def test_contract_maps_lifecycle_to_each_release_channel_entrypoint() -> None:
    """初始化、停止与自检经契约调用时落到各自基类的入口方法。"""
    module_view, plugin_view = _views()

    module_view.initialize()
    module_view.terminate()

    assert module_view.instance.events == ["init_module", "stop"]
    assert module_view.is_enabled() is True
    assert module_view.self_test() == (True, "ok")

    plugin_view.initialize({"enabled": True})
    plugin_view.terminate()

    assert plugin_view.instance.events == ["init_plugin", "close", "stop_service"]
    assert plugin_view.instance.config == {"enabled": True}
    assert plugin_view.is_enabled() is True
    assert plugin_view.self_test() is None

    plugin_view.instance.enabled = False
    assert plugin_view.is_enabled() is False


def test_hook_probe_is_shared_by_both_release_channels() -> None:
    """空实现的扩展点在两种发行方式上都不被识别为可用能力。"""
    module_view, plugin_view = _views()

    for view in (module_view, plugin_view):
        assert view.supports_hook("recognize_media") is True
        assert view.supports_hook("unimplemented_capability") is False
        assert view.supports_hook("not_declared_at_all") is False
        assert view.supports_hook("recognize_media") == supports_extension_hook(
            view.instance,
            "recognize_media",
        )


def test_capability_lookup_agrees_across_release_channels() -> None:
    """两种发行方式的可分发方法集合都只包含已实现的方法。"""
    module_view, plugin_view = _views()

    for view in (module_view, plugin_view):
        assert "recognize_media" in view.capability_names()
        assert "unimplemented_capability" not in view.capability_names()
        assert view.capability("recognize_media")("片名") in (
            "builtin:片名",
            "market:片名",
        )
        assert view.capability("unimplemented_capability") is None

    assert extension_capability_names(module_view.instance) == (
        module_view.capability_names()
    )
    assert plugin_view.capability_names() == ("recognize_media",)


def test_disabled_market_extension_exposes_no_capability() -> None:
    """未启用的市场扩展不向分发暴露任何可调用方法。"""
    plugin = DemoPlugin()
    plugin.enabled = False
    view = PluginExtension(plugin, "DemoPlugin")

    assert view.capability_names() == ()
    assert view.capability("recognize_media") is None


def test_provider_sources_describe_both_release_channels() -> None:
    """两种目录都满足提供者目录契约，并标注各自的发行方式。"""
    module_source = HostModuleProviderSource(_ModuleCatalog([DemoModule()]))
    plugin_source = PluginProviderSource(_PluginCatalog({"DemoPlugin": DemoPlugin()}))

    for source in (module_source, plugin_source):
        assert isinstance(source, ExtensionProviderSource)

    module_provider = next(iter(module_source.answer_providers("recognize_media")))
    plugin_provider = next(iter(plugin_source.answer_providers("recognize_media")))

    assert module_provider.extension_id == "DemoModule"
    assert module_provider.display_name == "内建演示模块"
    assert module_provider.distribution is ExtensionDistribution.BUILTIN
    assert module_provider.fault_scope is ExtensionFaultScope.HOST
    assert module_provider.relays_result is True

    assert plugin_provider.extension_id == "DemoPlugin"
    assert plugin_provider.display_name == "市场演示插件"
    assert plugin_provider.distribution is ExtensionDistribution.MARKET
    assert plugin_provider.fault_scope is ExtensionFaultScope.PLUGIN
    assert plugin_provider.relays_result is False


def test_dispatch_tiers_reach_both_release_channels_through_the_contract() -> None:
    """三级分发经契约取用提供者时，两种发行方式给出一致可预期的结果。"""
    dispatcher, _, _, _ = _dispatcher(
        modules=[DemoModule()],
        plugins={"DemoPlugin": DemoPlugin()},
    )

    assert dispatcher.multicast("recognize_media", "片名") == [
        "market:片名",
        "builtin:片名",
    ]
    assert dispatcher.unicast("recognize_media", "片名") == "market:片名"
    assert dispatcher.broadcast("recognize_media", "片名") is None
    assert dispatcher.dispatch("recognize_media", "片名") == "market:片名"


def test_dispatch_reports_faults_by_release_channel_scope() -> None:
    """提供者出错时按其归属方分流告警，另一发行方式继续参与仲裁。"""

    class BrokenPlugin(DemoPlugin):
        """模块方法始终失败的市场插件。"""

        def recognize_media(self, title: str) -> str:
            """模拟插件方法执行失败。"""
            raise RuntimeError("broken")

    dispatcher, plugin_error, system_error, _ = _dispatcher(
        modules=[DemoModule()],
        plugins={"BrokenPlugin": BrokenPlugin()},
    )

    assert dispatcher.unicast("recognize_media", "片名") == "builtin:片名"
    system_error.assert_not_called()
    error, extension_id, display_name, method = plugin_error.call_args.args
    assert isinstance(error, RuntimeError)
    assert (extension_id, display_name, method) == (
        "BrokenPlugin",
        "市场演示插件",
        "recognize_media",
    )


def test_dispatch_reports_builtin_faults_with_module_identity() -> None:
    """内建扩展出错时上报模块身份，并交给系统错误策略。"""

    class BrokenModule(DemoModule):
        """模块方法始终失败的内建模块。"""

        def recognize_media(self, title: str) -> str:
            """模拟模块方法执行失败。"""
            raise RuntimeError("broken")

    dispatcher, plugin_error, system_error, _ = _dispatcher(modules=[BrokenModule()])

    assert dispatcher.unicast("recognize_media", "片名") is None
    plugin_error.assert_not_called()
    error, extension_id, display_name, method = system_error.call_args.args
    assert isinstance(error, RuntimeError)
    assert (extension_id, display_name, method) == (
        "BrokenModule",
        "内建演示模块",
        "recognize_media",
    )


def test_rate_limit_is_reported_with_release_channel_label() -> None:
    """限流跳过按发行方式给出归属标签，不进入错误告警。"""

    class LimitedModule(DemoModule):
        """始终处于限流期间的内建模块。"""

        def recognize_media(self, title: str) -> str:
            """模拟模块处于限流期间。"""
            raise RateLimitExceededException("限流期间，跳过调用")

    dispatcher, _, system_error, rate_error = _dispatcher(modules=[LimitedModule()])

    assert dispatcher.unicast("recognize_media", "片名") is None
    system_error.assert_not_called()
    error, scope, extension_id, method = rate_error.call_args.args
    assert isinstance(error, RateLimitExceededException)
    assert (scope, extension_id, method) == (
        ExtensionFaultScope.HOST.value,
        "LimitedModule",
        "recognize_media",
    )


def test_added_release_channel_needs_no_change_in_shared_dispatch() -> None:
    """再接入一种发行方式时，分发内核仅按契约多取一个目录。"""
    remote = _RemoteSource("RemoteExtension", "recognize_media", lambda title: None)
    dispatcher, _, _, _ = _dispatcher(
        modules=[DemoModule()],
        plugins={"DemoPlugin": DemoPlugin()},
        extra_sources=(remote,),
    )

    assert dispatcher.multicast("recognize_media", "片名") == [
        "market:片名",
        "builtin:片名",
    ]

    remote_answer = _RemoteSource(
        "RemoteExtension",
        "remote_only",
        lambda: "remote",
    )
    dispatcher, _, _, _ = _dispatcher(extra_sources=(remote_answer,))

    assert dispatcher.unicast("remote_only") == "remote"
    assert dispatcher.multicast("remote_only") == ["remote"]
    assert dispatcher.dispatch("remote_only") == "remote"
    assert dispatcher.broadcast("remote_only") is None
    assert remote_answer.announced == ["remote_only"]


def test_added_release_channel_reuses_existing_fault_policy() -> None:
    """新发行方式声明归属方后即可复用既有告警通道。"""

    def broken() -> None:
        """模拟远程扩展执行失败。"""
        raise RuntimeError("broken")

    remote = _RemoteSource("RemoteExtension", "remote_only", broken)
    dispatcher, plugin_error, system_error, _ = _dispatcher(extra_sources=(remote,))

    assert dispatcher.unicast("remote_only") is None
    system_error.assert_not_called()
    error, extension_id, display_name, method = plugin_error.call_args.args
    assert isinstance(error, RuntimeError)
    assert (extension_id, display_name, method) == (
        "RemoteExtension",
        "远程扩展",
        "remote_only",
    )


@pytest.mark.asyncio
async def test_async_dispatch_tiers_reach_both_release_channels() -> None:
    """异步三级分发对两种发行方式保持与同步路径一致的结果。"""
    dispatcher, _, _, _ = _dispatcher(
        modules=[DemoModule()],
        plugins={"DemoPlugin": DemoPlugin()},
    )

    assert await dispatcher.async_multicast("recognize_media", "片名") == [
        "market:片名",
        "builtin:片名",
    ]
    assert await dispatcher.async_unicast("recognize_media", "片名") == "market:片名"
    assert await dispatcher.async_broadcast("recognize_media", "片名") is None
    assert await dispatcher.async_dispatch("recognize_media", "片名") == "market:片名"
