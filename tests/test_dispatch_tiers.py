"""广播、多播、单播三级分发的语义回归测试。"""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import Mock

import pytest

from app.runtime.extensions.projection.dispatcher import ModuleInvocationDispatcher
from app.schemas.exception import RateLimitExceededException


class _PluginCatalog:
    """提供固定插件方法表的内存目录。"""

    def __init__(self, modules: dict) -> None:
        """保存测试提供的插件模块快照。"""
        self.modules = modules

    def get_plugin_modules(self) -> dict:
        """返回当前插件模块快照。"""
        return self.modules


class _ModuleCatalog:
    """区分广播线性扫描与能力索引命中的内存目录。"""

    def __init__(self, modules: list) -> None:
        """保存测试模块，并记录两条查询路径的调用次数。"""
        self.modules = modules
        self.running_calls: list[str] = []
        self.provider_calls: list[str] = []

    def get_running_modules(self, method: str) -> list:
        """返回全部测试模块，模拟广播的线性扫描入口。"""
        self.running_calls.append(method)
        return list(self.modules)

    def providers_for(self, method: str) -> tuple:
        """返回按优先级排序的测试模块，模拟能力索引命中。"""
        self.provider_calls.append(method)
        return tuple(sorted(self.modules, key=lambda module: module.get_priority()))


class _Module:
    """实现可配置优先级和调用函数的测试宿主模块。"""

    def __init__(self, name: str, priority: int, func: Callable) -> None:
        """保存展示名、优先级和测试调用函数。"""
        self._name = name
        self._priority = priority
        self._func = func

    def get_name(self) -> str:
        """返回测试模块展示名。"""
        return self._name

    def get_priority(self) -> int:
        """返回调度优先级。"""
        return self._priority

    def execute(self, *args, **kwargs):
        """把模块调用转发到测试函数。"""
        return self._func(*args, **kwargs)


class _AsyncModule(_Module):
    """以协程入口暴露能力的测试宿主模块。"""

    async def execute(self, *args, **kwargs):
        """等待测试协程函数完成后返回结果。"""
        return await self._func(*args, **kwargs)


def _dispatcher(
    *,
    plugins: dict | None = None,
    modules: list | None = None,
) -> tuple[ModuleInvocationDispatcher, _ModuleCatalog, Mock, Mock, Mock]:
    """构造完全内存化的调度器、模块目录及错误策略替身。"""
    plugin_error = Mock()
    system_error = Mock()
    rate_error = Mock()
    catalog = _ModuleCatalog(modules or [])

    async def default_runner(func, *args, **kwargs):
        """在测试事件循环中直接运行同步函数。"""
        return func(*args, **kwargs)

    dispatcher = ModuleInvocationDispatcher(
        module_catalog=catalog,
        plugin_catalog=_PluginCatalog(plugins or {}),
        plugin_error_handler=plugin_error,
        system_error_handler=system_error,
        rate_limit_handler=rate_error,
        async_function_runner=default_runner,
    )
    return dispatcher, catalog, plugin_error, system_error, rate_error


def _recorder(calls: list, value, *, name: str) -> Callable:
    """生成记录调用顺序并返回固定值的提供者函数。"""

    def call(*_args, **_kwargs):
        """记录本次调用并返回预置结果。"""
        calls.append(name)
        return value

    return call


def _async_recorder(calls: list, value, *, name: str) -> Callable:
    """生成记录调用顺序并返回固定值的协程提供者函数。"""

    async def call(*_args, **_kwargs):
        """记录本次调用并返回预置结果。"""
        calls.append(name)
        return value

    return call


def test_broadcast_reaches_every_provider_without_short_circuit() -> None:
    """广播必须触达全部提供者，非空返回值不得中止后续通知。"""
    calls: list[str] = []
    dispatcher, catalog, _, _, _ = _dispatcher(
        plugins={("P1", "插件一"): {"execute": _recorder(calls, "plugin", name="plugin")}},
        modules=[
            _Module("慢模块", 20, _recorder(calls, "system-20", name="system-20")),
            _Module("快模块", 10, _recorder(calls, "system-10", name="system-10")),
        ],
    )

    assert dispatcher.broadcast("execute") is None
    assert calls == ["plugin", "system-10", "system-20"]
    assert catalog.running_calls == ["execute"]
    assert catalog.provider_calls == []


def test_broadcast_continues_after_provider_error() -> None:
    """广播中单个提供者抛错只上报系统错误，其余提供者继续执行。"""
    calls: list[str] = []

    def broken(*_args, **_kwargs):
        """模拟宿主模块执行失败。"""
        calls.append("broken")
        raise RuntimeError("broken")

    dispatcher, _, _, system_error, _ = _dispatcher(
        modules=[
            _Module("失败模块", 10, broken),
            _Module("后续模块", 20, _recorder(calls, "ok", name="ok")),
        ],
    )

    dispatcher.broadcast("execute")

    assert calls == ["broken", "ok"]
    system_error.assert_called_once()


def test_broadcast_rate_limit_is_not_reported_as_system_error() -> None:
    """广播中的本地限流跳过只走限流策略，不得进入系统错误告警。"""
    calls: list[str] = []

    def limited(*_args, **_kwargs):
        """模拟宿主模块处于限流期间。"""
        raise RateLimitExceededException("限流期间，跳过调用")

    dispatcher, _, _, system_error, rate_error = _dispatcher(
        modules=[
            _Module("限流模块", 10, limited),
            _Module("后续模块", 20, _recorder(calls, "ok", name="ok")),
        ],
    )

    dispatcher.broadcast("execute")

    assert calls == ["ok"]
    system_error.assert_not_called()
    rate_error.assert_called_once()


def test_multicast_collects_all_non_empty_answers_from_index() -> None:
    """多播走能力索引收集全部非空答案，返回 None 的提供者不计入结果。"""
    calls: list[str] = []
    dispatcher, catalog, _, _, _ = _dispatcher(
        plugins={
            ("P1", "插件一"): {"execute": _recorder(calls, "plugin-1", name="plugin-1")},
            ("P2", "插件二"): {"execute": _recorder(calls, None, name="plugin-2")},
        },
        modules=[
            _Module("慢模块", 20, _recorder(calls, "system-20", name="system-20")),
            _Module("空模块", 15, _recorder(calls, None, name="system-15")),
            _Module("快模块", 10, _recorder(calls, "system-10", name="system-10")),
        ],
    )

    assert dispatcher.multicast("execute") == [
        "plugin-1",
        "system-10",
        "system-20",
    ]
    assert calls == [
        "plugin-1",
        "plugin-2",
        "system-10",
        "system-15",
        "system-20",
    ]
    assert catalog.provider_calls == ["execute"]
    assert catalog.running_calls == []


def test_multicast_keeps_remaining_providers_after_error() -> None:
    """多播中单个提供者抛错不影响其余提供者的答案收集。"""
    def broken(*_args, **_kwargs):
        """模拟宿主模块执行失败。"""
        raise RuntimeError("broken")

    dispatcher, _, _, system_error, _ = _dispatcher(
        modules=[
            _Module("失败模块", 10, broken),
            _Module("后续模块", 20, lambda: "ok"),
        ],
    )

    assert dispatcher.multicast("execute") == ["ok"]
    system_error.assert_called_once()


def test_multicast_returns_empty_list_when_nobody_claims() -> None:
    """全部提供者返回 None 时多播返回空列表。"""
    dispatcher, _, _, _, _ = _dispatcher(
        modules=[_Module("空模块", 10, lambda: None)],
    )

    assert dispatcher.multicast("execute") == []


def test_unicast_short_circuits_on_first_non_empty_answer() -> None:
    """单播在首个非空答案处短路，后续提供者不再执行。"""
    calls: list[str] = []
    dispatcher, catalog, _, _, _ = _dispatcher(
        modules=[
            _Module("慢模块", 20, _recorder(calls, "system-20", name="system-20")),
            _Module("空模块", 5, _recorder(calls, None, name="system-5")),
            _Module("快模块", 10, _recorder(calls, "system-10", name="system-10")),
        ],
    )

    assert dispatcher.unicast("execute") == "system-10"
    assert calls == ["system-5", "system-10"]
    assert catalog.provider_calls == ["execute"]
    assert catalog.running_calls == []


def test_unicast_returns_none_without_falling_back_to_broadcast() -> None:
    """无人认领时单播返回 None，且不得回落到广播的线性扫描。"""
    dispatcher, catalog, _, _, _ = _dispatcher(
        modules=[_Module("空模块", 10, lambda: None)],
    )

    assert dispatcher.unicast("execute") is None
    assert catalog.running_calls == []


def test_unicast_skips_failed_provider_and_uses_next_answer() -> None:
    """单播中出错的提供者视为未认领，仲裁继续交给下一个提供者。"""
    def broken(*_args, **_kwargs):
        """模拟宿主模块执行失败。"""
        raise RuntimeError("broken")

    dispatcher, _, _, system_error, _ = _dispatcher(
        modules=[
            _Module("失败模块", 10, broken),
            _Module("后续模块", 20, lambda: "ok"),
        ],
    )

    assert dispatcher.unicast("execute") == "ok"
    system_error.assert_called_once()


def test_plugin_providers_answer_before_host_modules() -> None:
    """插件提供者在多播和单播中都必须先于宿主模块被询问。"""
    calls: list[str] = []
    dispatcher, _, _, _, _ = _dispatcher(
        plugins={("P1", "插件一"): {"execute": _recorder(calls, "plugin", name="plugin")}},
        modules=[_Module("宿主模块", 10, _recorder(calls, "system", name="system"))],
    )

    assert dispatcher.unicast("execute") == "plugin"
    assert calls == ["plugin"]

    assert dispatcher.multicast("execute") == ["plugin", "system"]
    assert calls == ["plugin", "plugin", "system"]


def test_plugin_error_uses_plugin_policy_and_keeps_host_providers() -> None:
    """插件提供者抛错走插件错误策略，宿主提供者仍然参与仲裁。"""
    def broken(*_args, **_kwargs):
        """模拟插件方法执行失败。"""
        raise RuntimeError("broken")

    dispatcher, _, plugin_error, system_error, _ = _dispatcher(
        plugins={("P1", "插件一"): {"execute": broken}},
        modules=[_Module("宿主模块", 10, lambda: "system")],
    )

    assert dispatcher.unicast("execute") == "system"
    plugin_error.assert_called_once()
    system_error.assert_not_called()


@pytest.mark.asyncio
async def test_async_broadcast_reaches_every_provider_without_short_circuit() -> None:
    """异步广播与同步广播同语义，协程与同步函数均被触达。"""
    calls: list[str] = []
    dispatcher, catalog, _, _, _ = _dispatcher(
        plugins={
            ("P1", "插件一"): {
                "execute": _async_recorder(calls, "plugin", name="plugin"),
            }
        },
        modules=[
            _Module("慢模块", 20, _recorder(calls, "system-20", name="system-20")),
            _Module("快模块", 10, _recorder(calls, "system-10", name="system-10")),
        ],
    )

    assert await dispatcher.async_broadcast("execute") is None
    assert calls == ["plugin", "system-10", "system-20"]
    assert catalog.provider_calls == []


@pytest.mark.asyncio
async def test_async_multicast_collects_all_non_empty_answers() -> None:
    """异步多播收集全部非空答案，并跳过未认领的提供者。"""
    calls: list[str] = []
    dispatcher, catalog, _, _, _ = _dispatcher(
        plugins={
            ("P1", "插件一"): {
                "execute": _async_recorder(calls, "plugin", name="plugin"),
            }
        },
        modules=[
            _AsyncModule("空模块", 15, _async_recorder(calls, None, name="system-15")),
            _Module("快模块", 10, _recorder(calls, "system-10", name="system-10")),
        ],
    )

    assert await dispatcher.async_multicast("execute") == ["plugin", "system-10"]
    assert calls == ["plugin", "system-10", "system-15"]
    assert catalog.running_calls == []


@pytest.mark.asyncio
async def test_async_unicast_short_circuits_and_reports_errors() -> None:
    """异步单播在首个非空答案处短路，出错提供者视为未认领。"""
    calls: list[str] = []

    async def broken(*_args, **_kwargs):
        """模拟异步宿主模块执行失败。"""
        calls.append("broken")
        raise RuntimeError("broken")

    dispatcher, catalog, _, system_error, _ = _dispatcher(
        modules=[
            _AsyncModule("失败模块", 10, broken),
            _AsyncModule(
                "命中模块",
                20,
                _async_recorder(calls, "system-20", name="system-20"),
            ),
            _Module("兜底模块", 30, _recorder(calls, "system-30", name="system-30")),
        ],
    )

    assert await dispatcher.async_unicast("execute") == "system-20"
    assert calls == ["broken", "system-20"]
    assert catalog.running_calls == []
    system_error.assert_called_once()


@pytest.mark.asyncio
async def test_async_broadcast_rate_limit_is_not_reported_as_system_error() -> None:
    """异步广播的本地限流跳过只走限流策略，不得进入系统错误告警。"""
    calls: list[str] = []

    async def limited(*_args, **_kwargs):
        """模拟异步宿主模块处于限流期间。"""
        raise RateLimitExceededException("限流期间，跳过调用")

    dispatcher, _, _, system_error, rate_error = _dispatcher(
        modules=[
            _AsyncModule("限流模块", 10, limited),
            _AsyncModule("后续模块", 20, _async_recorder(calls, "ok", name="ok")),
        ],
    )

    await dispatcher.async_broadcast("execute")

    assert calls == ["ok"]
    system_error.assert_not_called()
    rate_error.assert_called_once()


@pytest.mark.asyncio
async def test_async_dispatch_offloads_sync_providers_to_runner() -> None:
    """异步三级分发把同步提供者交给注入的执行器，不阻塞事件循环。"""
    offloaded: list = []
    plugin_error = Mock()
    catalog = _ModuleCatalog([_Module("同步模块", 10, lambda: "system")])

    async def async_runner(func, *args, **kwargs):
        """记录被移出事件循环的同步函数。"""
        offloaded.append(func)
        return func(*args, **kwargs)

    dispatcher = ModuleInvocationDispatcher(
        module_catalog=catalog,
        plugin_catalog=_PluginCatalog({("P1", "插件一"): {"execute": lambda: None}}),
        plugin_error_handler=plugin_error,
        system_error_handler=Mock(),
        rate_limit_handler=Mock(),
        async_function_runner=async_runner,
    )

    assert await dispatcher.async_unicast("execute") == "system"
    assert len(offloaded) == 2
    plugin_error.assert_not_called()
