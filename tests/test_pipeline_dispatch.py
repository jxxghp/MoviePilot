"""管道分发原语的语义回归测试。

obtain_images 是累积管道：TMDB、fanart、douban 依次在同一个对象上继续富化，
每个提供者接收上一个的产出并返回增强后的产出。管道走能力索引取候选，与多播、
单播同一候选集，不是广播式的全量遍历。
"""

from __future__ import annotations

import sys
import unittest
from collections.abc import Callable
from types import ModuleType as _PyModuleType
from unittest.mock import Mock

import pytest

from app.runtime.extensions.projection.dispatcher import ModuleInvocationDispatcher

sys.modules.setdefault("qbittorrentapi", _PyModuleType("qbittorrentapi"))
setattr(sys.modules["qbittorrentapi"], "TorrentFilesList", list)
sys.modules.setdefault("transmission_rpc", _PyModuleType("transmission_rpc"))
setattr(sys.modules["transmission_rpc"], "File", object)

from app.application.orchestration import ChainBase  # noqa: E402
from app.application.orchestration.context import ChainRuntimeContext  # noqa: E402
from app.schemas.types import MediaType  # noqa: E402


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


def test_pipeline_relays_each_providers_output_to_the_next() -> None:
    """三个提供者依次接力，每一个都拿到上一个的产出，最终返回最后一个的产出。"""
    seen: list[int] = []

    def step(add: int) -> Callable:
        """生成记录收到的输入并把其加上固定值的提供者函数。"""

        def call(previous: int) -> int:
            seen.append(previous)
            return previous + add

        return call

    dispatcher, catalog, _, _, _ = _dispatcher(
        modules=[
            _Module("加一", 10, step(1)),
            _Module("加十", 20, step(10)),
            _Module("加百", 30, step(100)),
        ],
    )

    assert dispatcher.pipeline("execute", 0) == 111
    assert seen == [0, 1, 11]
    assert catalog.provider_calls == ["execute"]
    assert catalog.running_calls == []


def test_pipeline_keeps_previous_output_when_a_provider_abstains() -> None:
    """中间提供者返回 None 时保留上一轮产出，继续传给下一个而不是让产出变成 None。"""
    seen: list[int] = []

    def record_and_return(value):
        """记录收到的输入并返回给定结果。"""

        def call(previous: int):
            seen.append(previous)
            return value

        return call

    dispatcher, _, _, _, _ = _dispatcher(
        modules=[
            _Module("产出一", 10, record_and_return(1)),
            _Module("弃权", 20, record_and_return(None)),
            _Module("接力三", 30, lambda previous: previous + 10),
        ],
    )

    result = dispatcher.pipeline("execute", 0)

    assert result == 11
    assert seen == [0, 1]


def test_pipeline_provider_exception_does_not_interrupt_the_chain() -> None:
    """单个提供者抛异常不中断管道，异常按归属策略上报后其余提供者仍被调用。"""

    def broken(previous):
        """模拟宿主模块执行失败。"""
        raise RuntimeError("broken")

    dispatcher, _, _, system_error, _ = _dispatcher(
        modules=[
            _Module("失败", 10, broken),
            _Module("继续", 20, lambda previous: previous + 1),
        ],
    )

    assert dispatcher.pipeline("execute", 0) == 1
    system_error.assert_called_once()


def test_pipeline_returns_initial_value_when_no_providers() -> None:
    """无提供者时管道原样返回传入的初始值。"""
    dispatcher, _, _, _, _ = _dispatcher(modules=[])
    sentinel = object()

    assert dispatcher.pipeline("execute", sentinel) is sentinel


def test_pipeline_uses_capability_index_not_broadcast_scan() -> None:
    """管道走能力索引取候选，与多播、单播同一候选集，不回落到广播式全量遍历。"""
    dispatcher, catalog, _, _, _ = _dispatcher(
        modules=[_Module("提供者", 10, lambda previous: previous + 1)],
    )

    dispatcher.pipeline("execute", 0)

    assert catalog.provider_calls == ["execute"]
    assert catalog.running_calls == []


@pytest.mark.asyncio
async def test_async_pipeline_matches_sync_semantics_with_mixed_providers() -> None:
    """异步管道对同步与协程提供者的接力语义与同步版本一致。"""
    seen: list[int] = []

    async def async_step(previous: int) -> int:
        """记录收到的输入并异步返回加一百的结果。"""
        seen.append(previous)
        return previous + 100

    def sync_step(previous: int) -> int:
        """记录收到的输入并同步返回加一的结果。"""
        seen.append(previous)
        return previous + 1

    dispatcher, _, _, _, _ = _dispatcher(
        modules=[
            _Module("同步加一", 10, sync_step),
            _AsyncModule("异步加百", 20, async_step),
        ],
    )

    assert await dispatcher.async_pipeline("execute", 0) == 101
    assert seen == [0, 1]


@pytest.mark.asyncio
async def test_async_pipeline_keeps_previous_output_when_a_provider_abstains() -> None:
    """异步管道中间提供者返回 None 时同样保留上一轮产出，不清空为 None。"""

    async def abstain(previous):
        """模拟协程提供者本轮不增强。"""
        return None

    dispatcher, _, _, _, _ = _dispatcher(
        modules=[
            _Module("产出一", 10, lambda previous: 1),
            _AsyncModule("弃权", 20, abstain),
            _Module("接力三", 30, lambda previous: previous + 10),
        ],
    )

    assert await dispatcher.async_pipeline("execute", 0) == 11


class _ImageModule:
    """暴露 obtain_images 方法的测试宿主模块，复现真实 Fanart/TMDB/Douban 分层。"""

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

    def obtain_images(self, mediainfo):
        """把图片补充请求转发到测试函数。"""
        return self._func(mediainfo)


class _MediaInfoDouble:
    """最小媒体信息替身，只暴露 obtain_images 实际读写的字段。"""

    def __init__(self, mtype=None) -> None:
        """保存媒体类型，图片字段初始为空。"""
        self.type = mtype
        self.poster_path = None
        self.backdrop_path = None


def _fanart_abstain(mediainfo):
    """模拟真实 FanartModule：没有可用图片时返回 None，不修改传入对象。"""
    del mediainfo
    return None


def _tmdb_enrich(mediainfo):
    """模拟真实 TheMovieDbModule：命中时原地补充海报并返回同一对象。"""
    mediainfo.poster_path = "tmdb-poster"
    return mediainfo


def _douban_enrich(mediainfo):
    """模拟真实 DoubanModule：原地补充背景图并返回同一对象。"""
    mediainfo.backdrop_path = "douban-backdrop"
    return mediainfo


def _obtain_images_modules() -> list:
    """构造与真实 Fanart(0)/TheMovieDb(1)/Douban(2) 同优先级顺序的替身模块。"""
    return [
        _ImageModule("Fanart", 0, _fanart_abstain),
        _ImageModule("TheMovieDb", 1, _tmdb_enrich),
        _ImageModule("Douban", 2, _douban_enrich),
    ]


def test_obtain_images_pipeline_matches_legacy_aggregate_dispatch() -> None:
    """管道原语在 obtain_images 真实分层场景上与既有聚合分发 dispatch() 行为等价。

    Fanart 优先级最高但常无图可用，返回 None 弃权；TMDB 与 Douban 各自原地补充
    一个字段。聚合分发依赖同一 mediainfo 对象在弃权时仍被原样传下去，管道显式
    保留上一轮产出，两条路径殊途同归，最终对象都携带全部字段。
    """
    legacy_dispatcher, _, _, _, _ = _dispatcher(modules=_obtain_images_modules())
    pipeline_dispatcher, _, _, _, _ = _dispatcher(modules=_obtain_images_modules())

    legacy_media = _MediaInfoDouble()
    legacy_result = legacy_dispatcher.dispatch("obtain_images", mediainfo=legacy_media)

    pipeline_media = _MediaInfoDouble()
    pipeline_result = pipeline_dispatcher.pipeline("obtain_images", pipeline_media)

    assert legacy_result is legacy_media
    assert pipeline_result is pipeline_media
    assert legacy_result.poster_path == pipeline_result.poster_path == "tmdb-poster"
    assert legacy_result.backdrop_path == pipeline_result.backdrop_path == "douban-backdrop"


def _build_chain_with_modules(modules: list) -> tuple[ChainBase, Mock]:
    """构造真实 ChainBase 实例，模块目录同时暴露能力索引与全量扫描两条路径。

    :param modules: 运行态模块替身
    :return: (链基类实例, 宿主模块目录替身)
    """

    def providers_for(method):
        """查询路径：按能力查表。"""
        return tuple(sorted(
            (m for m in modules if callable(getattr(m, method, None))),
            key=lambda m: m.get_priority(),
        ))

    def running_modules(method):
        """广播路径：遍历全体。"""
        return iter([m for m in modules if callable(getattr(m, method, None))])

    module_manager = Mock()
    module_manager.providers_for.side_effect = providers_for
    module_manager.get_running_modules.side_effect = running_modules
    plugin_manager = Mock()
    plugin_manager.get_plugin_modules.return_value = {}

    chain = ChainBase(runtime_context=ChainRuntimeContext(
        module_manager=module_manager,
        plugin_manager=plugin_manager,
        event_manager=Mock(),
        message_oper=Mock(),
        message_helper=Mock(),
        file_cache=Mock(),
        async_file_cache=Mock(),
        message_queue_factory=lambda callback: Mock(),
        module_dispatcher_factory=ModuleInvocationDispatcher,
    ))
    return chain, module_manager


class ChainObtainImagesFacadeTest(unittest.TestCase):
    """ChainBase.obtain_images 门面经能力索引走管道原语的回归测试。"""

    def test_facade_reaches_pipeline_via_capability_index(self):
        """门面调用落到管道原语，经能力索引取候选而非全量扫描。"""
        chain, module_manager = _build_chain_with_modules(_obtain_images_modules())

        result = chain.obtain_images(_MediaInfoDouble(mtype=MediaType.MOVIE))

        self.assertEqual(result.poster_path, "tmdb-poster")
        self.assertEqual(result.backdrop_path, "douban-backdrop")
        self.assertTrue(module_manager.providers_for.called)
        self.assertFalse(module_manager.get_running_modules.called)


if __name__ == "__main__":
    unittest.main()
