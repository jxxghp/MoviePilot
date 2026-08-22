"""媒体数据源声明的完整性与 source 路由测试。

一个数据源的展示信息与实现写在同一条声明里，登记时即判定完整性；声明里的多来源
契约方法由宿主按声明的 media_source 路由，非本来源的调用不触达实现。
"""

import asyncio
from types import SimpleNamespace
from typing import Iterator

import pytest

from app.runtime.deprecation import policy as deprecation_policy
from app.runtime.extensions.contract.declaration import (
    MediaSourceDeclaration,
    ModuleDeclaration,
)
from app.runtime.extensions.projection.dispatcher import ModuleInvocationDispatcher
from app.runtime.extensions.projection import plugin as projection_module
from app.runtime.extensions.admission.media_source import (
    media_source_declaration_violation,
)
from app.runtime.extensions.projection.plugin import PluginProjection
from app.schemas.types import MediaSource


@pytest.fixture(autouse=True)
def _clean_warning_dedup() -> Iterator[None]:
    """每个用例前后都清空各类告警去重记录，避免用例间互相掩盖。"""
    deprecation_policy.reset_warned()
    projection_module._module_source_overlap_warnings_seen.clear()
    projection_module._undeclared_media_source_hints_seen.clear()
    projection_module._sibling_contract_warnings_seen.clear()
    yield
    deprecation_policy.reset_warned()
    projection_module._module_source_overlap_warnings_seen.clear()
    projection_module._undeclared_media_source_hints_seen.clear()
    projection_module._sibling_contract_warnings_seen.clear()


class _Plugin(SimpleNamespace):
    """提供可配置插件 hook 的最小运行态插件替身。"""

    def __init__(self, enabled=True, **hooks):
        """保存启用状态、插件名称和 hook 实现。"""
        super().__init__(plugin_name=hooks.pop("plugin_name", "数据源插件"), **hooks)
        self._enabled = enabled

    def get_state(self):
        """返回预设启用状态。"""
        return self._enabled

    def get_name(self):
        """返回插件展示名称。"""
        return self.plugin_name


class _RecordingLogger:
    """记录各级日志文本的日志端口替身。"""

    def __init__(self) -> None:
        """初始化三级日志缓冲。"""
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.infos: list[str] = []

    def error(self, message: str) -> None:
        """记录错误日志。"""
        self.errors.append(message)

    def warning(self, message: str) -> None:
        """记录告警日志。"""
        self.warnings.append(message)

    def info(self, message: str) -> None:
        """记录提示日志。"""
        self.infos.append(message)


class _PluginCatalog:
    """把 PluginProjection.modules() 的产出适配为调度器消费的目录端口。"""

    def __init__(self, projection: PluginProjection) -> None:
        """保存被适配的插件能力投影。"""
        self._projection = projection

    def get_plugin_modules(self) -> dict:
        """返回当前插件模块方法表快照。"""
        return self._projection.modules()


class _EmptyModuleCatalog:
    """不提供任何宿主模块的空目录。"""

    def get_running_modules(self, _method: str):
        """始终返回空序列。"""
        return []

    def providers_for(self, _method: str):
        """始终返回空序列。"""
        return ()


def _dispatcher(projection: PluginProjection) -> ModuleInvocationDispatcher:
    """构造只接插件来源、不接宿主模块的最小调度器。"""
    return ModuleInvocationDispatcher(
        module_catalog=_EmptyModuleCatalog(),
        plugin_catalog=_PluginCatalog(projection),
        plugin_error_handler=lambda *a, **k: None,
        system_error_handler=lambda *a, **k: None,
        rate_limit_handler=lambda *a, **k: None,
    )


def _detail(**kwargs):
    """媒体数据源实现桩，回显收到的调用参数。"""
    return kwargs


# ---------------------------------------------------------------------------
# 完整性契约：写一半在登记时即被拒
# ---------------------------------------------------------------------------


def test_contract_rejects_display_only_declaration() -> None:
    """只报展示信息、不带实现的声明必须被拒，理由须点名 methods。"""
    declaration = MediaSourceDeclaration(media_source="acme.video", name="Acme Video")

    violation = media_source_declaration_violation(declaration)

    assert violation is not None
    assert "methods" in violation


def test_contract_rejects_empty_method_table() -> None:
    """methods 给了空映射与压根没给一样，都不构成可调用的实现。"""
    declaration = MediaSourceDeclaration(
        media_source="acme.video", name="Acme Video", methods={}
    )

    violation = media_source_declaration_violation(declaration)

    assert violation is not None
    assert "methods" in violation


@pytest.mark.parametrize(
    ("declaration", "missing"),
    [
        (
            MediaSourceDeclaration(name="Acme Video", methods={"media_detail": _detail}),
            "media_source",
        ),
        (
            MediaSourceDeclaration(
                media_source="acme.video", methods={"media_detail": _detail}
            ),
            "name",
        ),
    ],
    ids=["media_source_missing", "name_missing"],
)
def test_contract_rejects_implementation_only_declaration(declaration, missing) -> None:
    """只挂实现、不报展示信息的声明必须被拒，理由须点名缺失字段。"""
    violation = media_source_declaration_violation(declaration)

    assert violation is not None
    assert missing in violation


def test_contract_accepts_complete_declaration() -> None:
    """展示信息与实现都给全的声明合规。"""
    declaration = MediaSourceDeclaration(
        media_source="acme.video",
        name="Acme Video",
        media_types=("电影",),
        methods={"media_detail": _detail},
    )

    assert media_source_declaration_violation(declaration) is None


def test_display_only_declaration_never_reaches_source_list() -> None:
    """写一半的声明在登记时即被拒，来源列表不会出现它。"""
    log = _RecordingLogger()
    plugin = _Plugin(
        provides_media_sources=lambda: [
            MediaSourceDeclaration(media_source="acme.video", name="Acme Video")
        ]
    )
    projection = PluginProjection({"Demo": plugin}, log=log)

    assert projection.provided_media_sources() == {"Demo": []}
    assert projection.media_sources() == []
    assert any("methods" in message for message in log.errors)


# ---------------------------------------------------------------------------
# 完整声明的登记与取用
# ---------------------------------------------------------------------------


def test_complete_declaration_feeds_both_source_list_and_dispatch_table() -> None:
    """一条完整声明同时进入来源列表与分发方法表。"""
    plugin = _Plugin(
        provides_media_sources=lambda: [
            MediaSourceDeclaration(
                media_source="acme.video",
                name="Acme Video",
                media_types=("电影", "电视剧"),
                methods={"media_detail": _detail},
            )
        ]
    )
    projection = PluginProjection({"Demo": plugin})

    sources = projection.media_sources()
    modules = projection.modules()

    assert sources == [
        {
            "name": "Acme Video",
            "media_source": "acme.video",
            "plugin_id": "Demo",
            "capabilities": ["detail"],
            "media_types": ["电影", "电视剧"],
        }
    ]
    assert "media_detail" in modules[("Demo", "数据源插件")]


def test_declared_method_answers_unicast_for_its_own_source() -> None:
    """声明里的实现须能被单播分发触达并应答本来源的请求。"""
    plugin = _Plugin(
        provides_media_sources=lambda: [
            MediaSourceDeclaration(
                media_source="acme.video",
                name="Acme Video",
                methods={"media_detail": lambda **kwargs: {"claimed": kwargs["media_id"]}},
            )
        ]
    )
    dispatcher = _dispatcher(PluginProjection({"Demo": plugin}))

    result = dispatcher.unicast("media_detail", source="acme.video", media_id="42")

    assert result == {"claimed": "42"}


# ---------------------------------------------------------------------------
# source 路由与让出语义
# ---------------------------------------------------------------------------


def test_foreign_source_call_never_touches_the_implementation() -> None:
    """非本来源的调用由宿主直接让出，实现不被触达。"""
    calls: list[dict] = []

    def _never(**kwargs):
        """记录被触达的调用，本用例中不应被调用。"""
        calls.append(kwargs)
        return []

    plugin = _Plugin(
        provides_media_sources=lambda: [
            MediaSourceDeclaration(
                media_source="acme.video", name="Acme Video", methods={"media_detail": _never}
            )
        ]
    )
    dispatcher = _dispatcher(PluginProjection({"Demo": plugin}))

    result = dispatcher.unicast("media_detail", source=MediaSource.TMDB, media_id="42")

    assert result is None
    assert calls == []


def test_implementation_returning_empty_list_cannot_block_other_sources() -> None:
    """实现误返回空列表也只影响本来源：其它来源的调用压根不进这条实现。"""
    plugin = _Plugin(
        provides_media_sources=lambda: [
            MediaSourceDeclaration(
                media_source="acme.video",
                name="Acme Video",
                methods={"person_credits": lambda **kwargs: []},
            )
        ]
    )
    dispatcher = _dispatcher(PluginProjection({"Demo": plugin}))

    assert dispatcher.unicast("person_credits", source="acme.video", person_id="1") == []
    assert dispatcher.unicast("person_credits", source=MediaSource.Douban, person_id="1") is None


def test_call_without_source_is_yielded() -> None:
    """调用未带 source 时无从判断归属，按弃权协议让出。"""
    plugin = _Plugin(
        provides_media_sources=lambda: [
            MediaSourceDeclaration(
                media_source="acme.video", name="Acme Video", methods={"media_detail": _detail}
            )
        ]
    )
    dispatcher = _dispatcher(PluginProjection({"Demo": plugin}))

    assert dispatcher.unicast("media_detail", media_id="42") is None


def test_builtin_source_alias_routes_to_the_same_implementation() -> None:
    """声明与调用两侧的来源标识都按 MediaSource 归一，别名不影响命中。"""
    plugin = _Plugin(
        provides_media_sources=lambda: [
            MediaSourceDeclaration(
                media_source="themoviedb",
                name="接管 TMDB",
                methods={"media_detail": lambda **kwargs: "claimed"},
            )
        ]
    )
    dispatcher = _dispatcher(PluginProjection({"Demo": plugin}))

    assert dispatcher.unicast("media_detail", source=MediaSource.TMDB, media_id="1") == "claimed"


def test_two_sources_on_one_instance_route_independently() -> None:
    """同一实例声明两个数据源时，两份同名契约实现互不覆盖，各自按来源应答。"""
    plugin = _Plugin(
        provides_media_sources=lambda: [
            MediaSourceDeclaration(
                media_source="acme.video",
                name="Acme Video",
                methods={"media_detail": lambda **kwargs: "video"},
            ),
            MediaSourceDeclaration(
                media_source="acme.music",
                name="Acme Music",
                methods={"media_detail": lambda **kwargs: "music"},
            ),
        ]
    )
    dispatcher = _dispatcher(PluginProjection({"Demo": plugin}))

    assert dispatcher.unicast("media_detail", source="acme.video", media_id="1") == "video"
    assert dispatcher.unicast("media_detail", source="acme.music", media_id="1") == "music"


def test_async_contract_method_routes_and_awaits() -> None:
    """async_ 变体与同名同步方法共用路由规则，且保持协程形态供异步分发直接 await。"""

    async def _async_detail(**kwargs):
        """异步实现桩。"""
        return "async-claimed"

    plugin = _Plugin(
        provides_media_sources=lambda: [
            MediaSourceDeclaration(
                media_source="acme.video",
                name="Acme Video",
                methods={"async_media_detail": _async_detail},
            )
        ]
    )
    dispatcher = _dispatcher(PluginProjection({"Demo": plugin}))

    claimed = asyncio.run(
        dispatcher.async_unicast("async_media_detail", source="acme.video", media_id="1")
    )
    yielded = asyncio.run(
        dispatcher.async_unicast("async_media_detail", source=MediaSource.Douban, media_id="1")
    )

    assert claimed == "async-claimed"
    assert yielded is None


def test_non_contract_method_is_mounted_without_routing() -> None:
    """不按 source 收窄的方法原样挂载，调用不带 source 也照常触达。"""
    plugin = _Plugin(
        provides_media_sources=lambda: [
            MediaSourceDeclaration(
                media_source="acme.video",
                name="Acme Video",
                methods={"recognize_media": lambda **kwargs: "recognized"},
            )
        ]
    )
    dispatcher = _dispatcher(PluginProjection({"Demo": plugin}))

    assert dispatcher.unicast("recognize_media", title="x") == "recognized"


# ---------------------------------------------------------------------------
# 与 provides_modules() 并存
# ---------------------------------------------------------------------------


def test_provides_modules_only_plugin_registers_unchanged() -> None:
    """非数据源用途、只写 provides_modules() 的插件不因本规则被拒绝登记。"""
    log = _RecordingLogger()
    plugin = _Plugin(
        provides_modules=lambda: [ModuleDeclaration(methods={"storage_manage": _detail})]
    )
    projection = PluginProjection({"Demo": plugin}, log=log)
    dispatcher = _dispatcher(projection)

    assert "storage_manage" in projection.modules()[("Demo", "数据源插件")]
    assert dispatcher.unicast("storage_manage", path="/tmp") == {"path": "/tmp"}
    assert log.errors == []
    assert log.infos == []


def test_provides_modules_multi_source_method_without_declaration_hints_once() -> None:
    """挂了多来源契约方法却没声明数据源时提示一次，实现照常可用。"""
    log = _RecordingLogger()
    plugin = _Plugin(
        provides_modules=lambda: [
            ModuleDeclaration(methods={"media_detail": lambda **kwargs: "taken-over"})
        ]
    )
    projection = PluginProjection({"Demo": plugin}, log=log)
    dispatcher = _dispatcher(projection)

    projection.modules()
    projection.modules()

    assert dispatcher.unicast("media_detail", source=MediaSource.TMDB, media_id="1") == "taken-over"
    hints = [message for message in log.infos if "media_detail" in message]
    assert len(hints) == 1
    assert "provides_media_sources" in hints[0]


def test_media_source_lane_wins_over_provides_modules_on_overlap() -> None:
    """两条来源挂载同一方法名时数据源声明优先，并就重叠告警一次。"""
    log = _RecordingLogger()
    plugin = _Plugin(
        provides_modules=lambda: [
            ModuleDeclaration(methods={"media_detail": lambda **kwargs: "generic"})
        ],
        provides_media_sources=lambda: [
            MediaSourceDeclaration(
                media_source="acme.video",
                name="Acme Video",
                methods={"media_detail": lambda **kwargs: "declared"},
            )
        ],
    )
    projection = PluginProjection({"Demo": plugin}, log=log)
    dispatcher = _dispatcher(projection)

    projection.modules()
    projection.modules()

    assert dispatcher.unicast("media_detail", source="acme.video", media_id="1") == "declared"
    overlaps = [message for message in log.warnings if "同时挂载方法名" in message]
    assert len(overlaps) == 1
    assert "provides_media_sources()" in overlaps[0]


def test_legacy_hooks_remain_a_working_pair() -> None:
    """旧的 get_media_source()/get_module() 两半写法仍原样可用，不受本规则影响。"""
    plugin = _Plugin(
        get_media_source=lambda: [{"name": "旧式来源", "media_source": "legacy.source"}],
        get_module=lambda: {"media_detail": lambda **kwargs: "legacy"},
    )
    projection = PluginProjection({"Demo": plugin})
    dispatcher = _dispatcher(projection)

    sources = projection.media_sources()

    assert [item["media_source"] for item in sources] == ["legacy.source"]
    assert dispatcher.unicast("media_detail", source="legacy.source", media_id="1") == "legacy"
