"""模块调用调度器的同步、异步协议回归测试。"""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import Mock

import pytest

from app.runtime.extensions.module.dispatcher import ModuleInvocationDispatcher


class _PluginCatalog:
    """提供固定插件方法表的内存目录。"""

    def __init__(self, modules: dict) -> None:
        """保存测试提供的插件模块快照。"""
        self.modules = modules

    def get_plugin_modules(self) -> dict:
        """返回当前插件模块快照。"""
        return self.modules


class _ModuleCatalog:
    """提供固定宿主模块序列的内存目录。"""

    def __init__(self, modules: list) -> None:
        """保存测试提供的宿主模块。"""
        self.modules = modules

    def get_running_modules(self, _method: str) -> list:
        """返回所有测试模块，由调度器负责优先级排序。"""
        return list(self.modules)


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


def _dispatcher(
    *,
    plugins: dict | None = None,
    modules: list | None = None,
    async_runner: Callable | None = None,
) -> tuple[ModuleInvocationDispatcher, Mock, Mock, Mock]:
    """构造完全内存化的调度器及错误策略替身。"""
    plugin_error = Mock()
    system_error = Mock()
    rate_error = Mock()

    async def default_runner(func, *args, **kwargs):
        """在测试事件循环中直接运行同步函数。"""
        return func(*args, **kwargs)

    dispatcher = ModuleInvocationDispatcher(
        module_catalog=_ModuleCatalog(modules or []),
        plugin_catalog=_PluginCatalog(plugins or {}),
        plugin_error_handler=plugin_error,
        system_error_handler=system_error,
        rate_limit_handler=rate_error,
        async_function_runner=async_runner or default_runner,
    )
    return dispatcher, plugin_error, system_error, rate_error


def test_plugin_scalar_short_circuits_system_modules() -> None:
    """插件返回非空标量时不得继续执行宿主模块。"""
    system_call = Mock(return_value="system")
    dispatcher, _, _, _ = _dispatcher(
        plugins={("P1", "插件一"): {"execute": lambda: "plugin"}},
        modules=[_Module("系统", 10, system_call)],
    )

    assert dispatcher.dispatch("execute") == "plugin"
    system_call.assert_not_called()


def test_fan_out_contract_runs_every_provider_and_ignores_results() -> None:
    """副作用广播应执行全部插件和宿主 provider，并稳定返回 None。"""
    calls = []

    def record(name: str, result):
        """生成记录调用顺序并返回测试哨兵的 provider。"""
        return lambda: calls.append(name) or result

    system_20 = _Module("系统二", 20, record("system-20", "ignored-system"))
    system_10 = _Module("系统一", 10, record("system-10", None))
    setattr(system_20, "clear_cache", system_20.execute)
    setattr(system_10, "clear_cache", system_10.execute)
    dispatcher, _, _, _ = _dispatcher(
        plugins={
            ("P1", "插件一"): {"clear_cache": record("plugin-1", "ignored-plugin")},
            ("P2", "插件二"): {"clear_cache": record("plugin-2", None)},
        },
        modules=[system_20, system_10],
    )

    assert dispatcher.dispatch("clear_cache") is None
    assert calls == ["plugin-1", "plugin-2", "system-10", "system-20"]


@pytest.mark.asyncio
async def test_async_fan_out_contract_matches_sync_execution() -> None:
    """异步广播也应忽略返回值并执行全部同步或异步 provider。"""
    calls = []

    async def plugin_call():
        """记录异步插件调用并返回应被忽略的哨兵。"""
        calls.append("plugin")
        return "ignored-plugin"

    def system_call():
        """记录同步宿主调用并返回应被忽略的哨兵。"""
        calls.append("system")
        return "ignored-system"

    module = _Module("系统", 10, system_call)
    setattr(module, "clear_cache", module.execute)
    dispatcher, _, _, _ = _dispatcher(
        plugins={("P1", "插件一"): {"clear_cache": plugin_call}},
        modules=[module],
    )

    assert await dispatcher.async_dispatch("clear_cache") is None
    assert calls == ["plugin", "system"]


def test_list_results_merge_in_plugin_then_priority_order() -> None:
    """列表结果应先按插件顺序合并，再按宿主优先级继续合并。"""
    calls = []

    def result(value: str) -> Callable:
        """生成记录调用顺序并返回单项列表的模块函数。"""
        return lambda: calls.append(value) or [value]

    dispatcher, _, _, _ = _dispatcher(
        plugins={
            ("P1", "插件一"): {"execute": result("plugin-1")},
            ("P2", "插件二"): {"execute": result("plugin-2")},
        },
        modules=[
            _Module("慢模块", 20, result("system-20")),
            _Module("快模块", 10, result("system-10")),
        ],
    )

    assert dispatcher.dispatch("execute") == [
        "plugin-1",
        "plugin-2",
        "system-10",
        "system-20",
    ]
    assert calls == ["plugin-1", "plugin-2", "system-10", "system-20"]


def test_result_shape_diagnosis_does_not_break_system_dispatch() -> None:
    """provider 结果形状异常时只记录诊断，不得击穿宿主模块调度。"""
    module = _Module("异常结果模块", 10, lambda: "unexpected")
    setattr(module, "search_medias", module.execute)
    dispatcher, _, system_error, _ = _dispatcher(modules=[module])

    assert dispatcher.dispatch("search_medias") == "unexpected"
    system_error.assert_not_called()


def test_system_signature_relay_passes_previous_result() -> None:
    """单参数宿主方法应接收上一模块的非列表结果。"""
    class FirstModule:
        """产生首个字典结果的测试模块。"""

        @staticmethod
        def get_name() -> str:
            """返回测试模块名。"""
            return "第一步"

        @staticmethod
        def get_priority() -> int:
            """返回第一优先级。"""
            return 10

        @staticmethod
        def execute() -> dict:
            """产生首个模块结果。"""
            return {"value": 1}

    class SecondModule:
        """消费上一结果的测试模块。"""

        @staticmethod
        def get_name() -> str:
            """返回测试模块名。"""
            return "第二步"

        @staticmethod
        def get_priority() -> int:
            """返回第二优先级。"""
            return 20

        @staticmethod
        def execute(previous: dict) -> dict:
            """接收上一模块结果并生成下一结果。"""
            return {"value": previous["value"] + 1}

    dispatcher, _, _, _ = _dispatcher(
        modules=[SecondModule(), FirstModule()]
    )

    assert dispatcher.dispatch("execute") == {"value": 2}


def test_explicit_pipeline_contract_relays_previous_result() -> None:
    """图片补全契约应按优先级把上一 provider 结果交给下一 provider。"""
    class ImageModule:
        """在统一媒体对象上记录当前图片 provider。"""

        def __init__(self, name: str, priority: int) -> None:
            """保存 provider 名称和优先级。"""
            self._name = name
            self._priority = priority

        def get_name(self) -> str:
            """返回测试模块名。"""
            return self._name

        def get_priority(self) -> int:
            """返回测试优先级。"""
            return self._priority

        def obtain_images(self, mediainfo: dict) -> dict:
            """追加当前 provider 名称并返回同一媒体结果。"""
            return {
                **mediainfo,
                "providers": [*mediainfo.get("providers", []), self._name],
            }

    dispatcher, _, _, _ = _dispatcher(
        modules=[
            ImageModule("fanart", 20),
            ImageModule("tmdb", 10),
        ]
    )

    assert dispatcher.dispatch("obtain_images", mediainfo={}) == {
        "providers": ["tmdb", "fanart"]
    }


def test_first_non_empty_contract_stops_legacy_signature_relay() -> None:
    """显式首个非空契约不得再把结果交给后续宿主 provider 改写。"""
    class FirstModule:
        """返回首个识别结果的宿主模块。"""

        @staticmethod
        def get_name() -> str:
            """返回测试模块名。"""
            return "第一识别源"

        @staticmethod
        def get_priority() -> int:
            """返回第一优先级。"""
            return 10

        @staticmethod
        def recognize_media() -> str:
            """返回首个非空识别结果。"""
            return "first"

    class RelayCompatibleModule:
        """模拟可接受上一结果的旧式宿主模块。"""

        @staticmethod
        def get_name() -> str:
            """返回测试模块名。"""
            return "旧式接力源"

        @staticmethod
        def get_priority() -> int:
            """返回第二优先级。"""
            return 20

        @staticmethod
        def recognize_media(previous: str) -> str:
            """若被调用则改写上一结果。"""
            return f"relayed:{previous}"

    dispatcher, _, _, _ = _dispatcher(
        modules=[RelayCompatibleModule(), FirstModule()]
    )

    assert dispatcher.dispatch("recognize_media") == "first"


def test_ordered_list_contract_bypasses_legacy_signature_relay() -> None:
    """显式列表聚合契约应按原参数调用并保留 provider 顺序。"""
    class SearchModule:
        """区分原参数调用与旧式结果接力的搜索模块。"""

        @staticmethod
        def get_name() -> str:
            """返回测试模块名。"""
            return "系统搜索源"

        @staticmethod
        def get_priority() -> int:
            """返回稳定优先级。"""
            return 10

        @staticmethod
        def search_medias(previous: list | None = None) -> list[str]:
            """原参数调用返回系统结果，接力调用返回可检测哨兵。"""
            return ["relayed"] if previous is not None else ["system"]

    dispatcher, _, _, _ = _dispatcher(
        plugins={
            ("P1", "插件一"): {"search_medias": lambda: ["plugin"]},
        },
        modules=[SearchModule()],
    )

    assert dispatcher.dispatch("search_medias") == ["plugin", "system"]


def test_ordered_mapping_contract_merges_system_downloader_results() -> None:
    """未指定下载器时应按宿主优先级合并各 provider 的 Tracker 映射。"""
    class TrackerModule:
        """返回单个下载器 Tracker 映射的测试模块。"""

        def __init__(self, name: str, priority: int) -> None:
            """保存下载器名称和 provider 优先级。"""
            self._name = name
            self._priority = priority

        def get_name(self) -> str:
            """返回测试模块名。"""
            return self._name

        def get_priority(self) -> int:
            """返回测试优先级。"""
            return self._priority

        def get_torrent_trackers(
            self,
            hash_string: str,
            downloader: str | None = None,
        ) -> dict[str, list[str]]:
            """返回当前测试下载器的 Tracker 映射。"""
            assert hash_string == "hash"
            assert downloader is None
            return {self._name: [f"https://{self._name}.test/announce"]}

    dispatcher, _, _, _ = _dispatcher(
        modules=[
            TrackerModule("transmission", 20),
            TrackerModule("qbittorrent", 10),
        ]
    )

    assert dispatcher.dispatch(
        "get_torrent_trackers",
        hash_string="hash",
        downloader=None,
    ) == {
        "qbittorrent": ["https://qbittorrent.test/announce"],
        "transmission": ["https://transmission.test/announce"],
    }


def test_plugin_mapping_keeps_existing_host_short_circuit() -> None:
    """插件返回 Tracker 映射后仍应保持插件优先，不再调用宿主 provider。"""
    system_call = Mock(return_value={"system": ["https://system.test"]})
    module = _Module("系统", 10, system_call)
    setattr(module, "get_torrent_trackers", module.execute)
    dispatcher, _, _, _ = _dispatcher(
        plugins={
            ("P1", "插件一"): {
                "get_torrent_trackers": lambda **_kwargs: {
                    "plugin": ["https://plugin.test"]
                }
            },
        },
        modules=[module],
    )

    assert dispatcher.dispatch(
        "get_torrent_trackers",
        hash_string="hash",
        downloader=None,
    ) == {"plugin": ["https://plugin.test"]}
    system_call.assert_not_called()


def test_module_exception_uses_error_policy_and_continues() -> None:
    """普通异常应交给错误策略，后续空结果模块仍可继续运行。"""
    def broken():
        """模拟模块执行失败。"""
        raise RuntimeError("broken")

    dispatcher, _, system_error, _ = _dispatcher(
        modules=[
            _Module("失败模块", 10, broken),
            _Module("后续模块", 20, lambda: "ok"),
        ],
    )

    assert dispatcher.dispatch("execute") == "ok"
    system_error.assert_called_once()


@pytest.mark.asyncio
async def test_async_dispatch_awaits_coroutines_and_offloads_sync_functions() -> None:
    """异步路径应直接等待协程，并通过注入执行器运行同步方法。"""
    offloaded = []

    async def async_runner(func, *args, **kwargs):
        """记录被移出事件循环的同步函数。"""
        offloaded.append(func)
        return func(*args, **kwargs)

    async def plugin_call():
        """返回插件列表结果。"""
        return ["plugin"]

    sync_module = _Module("同步模块", 10, lambda: ["system"])
    dispatcher, _, _, _ = _dispatcher(
        plugins={("P1", "插件一"): {"execute": plugin_call}},
        modules=[sync_module],
        async_runner=async_runner,
    )

    assert await dispatcher.async_dispatch("execute") == ["plugin", "system"]
    assert offloaded == [sync_module.execute]


@pytest.mark.asyncio
async def test_async_ordered_list_contract_uses_same_aggregation_policy() -> None:
    """异步 dispatcher 应与同步路径共享显式列表聚合语义。"""
    class SearchModule:
        """提供异步路径下可识别调用方式的同步 provider。"""

        @staticmethod
        def get_name() -> str:
            """返回测试模块名。"""
            return "异步系统搜索源"

        @staticmethod
        def get_priority() -> int:
            """返回稳定优先级。"""
            return 10

        @staticmethod
        def search_medias(previous: list | None = None) -> list[str]:
            """原参数调用返回系统结果，接力调用返回可检测哨兵。"""
            return ["relayed"] if previous is not None else ["system"]

    async def plugin_search() -> list[str]:
        """返回插件搜索结果。"""
        return ["plugin"]

    dispatcher, _, _, _ = _dispatcher(
        plugins={
            ("P1", "插件一"): {"search_medias": plugin_search},
        },
        modules=[SearchModule()],
    )

    assert await dispatcher.async_dispatch("search_medias") == [
        "plugin",
        "system",
    ]


def test_plugin_non_mapping_module_decl_is_reported_and_skipped() -> None:
    """插件把方法表声明成 list 时走错误策略，且不影响后续健康插件。"""
    dispatcher, plugin_error, _, _ = _dispatcher(
        plugins={
            ("Bad", "坏插件"): ["not-a-mapping"],
            ("Good", "好插件"): {"execute": lambda: "ok"},
        },
    )

    assert dispatcher.dispatch("execute") == "ok"
    plugin_error.assert_called_once()


def test_unknown_plugin_method_records_legacy_abi_hit(monkeypatch) -> None:
    """未知第三方方法继续执行，同时记录可迁移的 legacy ABI 来源。"""
    hits = []
    monkeypatch.setattr(
        "app.runtime.extensions.module.dispatcher.record_metric",
        lambda name, **labels: hits.append((name, labels)),
    )
    dispatcher, _, _, _ = _dispatcher(
        plugins={("P1", "插件一"): {"third_party_custom": lambda: "ok"}},
    )

    assert dispatcher.dispatch("third_party_custom") == "ok"
    assert hits == [
        (
            "module.contract.legacy_hit",
            {
                "method": "third_party_custom",
                "caller_type": "plugin",
                "abi_source": "third_party_plugin",
            },
        )
    ]


def test_unknown_host_method_records_legacy_abi_hit(monkeypatch) -> None:
    """宿主临时新增而未登记的方法保持执行并留下迁移信号。"""
    hits = []
    monkeypatch.setattr(
        "app.runtime.extensions.module.dispatcher.record_metric",
        lambda name, **labels: hits.append((name, labels)),
    )

    class LegacyModule:
        """提供未进入清单的宿主兼容方法。"""

        @staticmethod
        def get_name() -> str:
            """返回测试模块名称。"""
            return "旧模块"

        @staticmethod
        def get_priority() -> int:
            """返回稳定测试优先级。"""
            return 1

        @staticmethod
        def third_party_host() -> str:
            """返回兼容方法结果。"""
            return "ok"

    dispatcher, _, _, _ = _dispatcher(modules=[LegacyModule()])

    assert dispatcher.dispatch("third_party_host") == "ok"
    assert hits == [
        (
            "module.contract.legacy_hit",
            {
                "method": "third_party_host",
                "caller_type": "system",
                "abi_source": "host_module",
            },
        )
    ]


@pytest.mark.asyncio
async def test_async_plugin_non_mapping_module_decl_is_reported_and_skipped() -> None:
    """异步路径下坏插件同样被隔离，嵌套补丁场景不再冒泡击穿调度。"""
    dispatcher, plugin_error, _, _ = _dispatcher(
        plugins={
            ("Bad", "坏插件"): ["not-a-mapping"],
            ("Good", "好插件"): {"execute": lambda: "ok"},
        },
    )

    assert await dispatcher.async_dispatch("execute") == "ok"
    plugin_error.assert_called_once()
