"""名称解析环顺序端点：生效顺序、标识拆解、写入回读与响应模型穿透。

这一族此前只能靠通用的 ``/system/setting/{key}`` 手写 JSON 读写，而顺序即语义——谁先跑
决定谁能覆盖谁的字段，因此端点要回答的是「最终会按什么次序跑」，不是「配置里写了什么」。
本文件锁住四件一旦回退就让前端做不出拖拽排序页的事：未排到的环按声明 priority 追加、
内建环出现在列表首位、解析环标识拆得开、写入后生效顺序随之改变。
"""

import asyncio
import inspect
from typing import Any, Iterator, List, Optional
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.api.deps import (
    get_current_active_superuser,
    get_current_active_superuser_async,
)
from app.api.endpoints import metaparser as metaparser_endpoint
from app.application.metaparser import (
    BUILTIN_META_PARSER_NAME,
    MetaParserPipelineService,
)
from app.runtime.extensions.contract.extension import ExtensionDistribution
from app.runtime.extensions.registry.meta_parser import (
    configure_meta_parser_order_reader,
    meta_parser_registry,
)
from app.schemas.metaparse import (
    BUILTIN_META_PARSER,
    MetaParserOrderEntry,
    MetaParserPipeline,
    MetaParserToggle,
    ParsedMeta,
)
from app.schemas.types import SystemConfigKey


@pytest.fixture(autouse=True)
def isolate_meta_parser_registry() -> Iterator[None]:
    """快照并复原解析器注册表与顺序配置，避免用例相互污染。"""
    original = dict(meta_parser_registry._entries)
    previous_reader = configure_meta_parser_order_reader(lambda: None)
    meta_parser_registry._entries.clear()
    try:
        yield
    finally:
        configure_meta_parser_order_reader(previous_reader)
        meta_parser_registry._entries.clear()
        meta_parser_registry._entries.update(original)


class _ConfigStore:
    """顺序配置的内存替身，读端口与写端口共用同一份取值。"""

    def __init__(self) -> None:
        """创建空的顺序配置。"""
        self.value: Any = None
        self.writes: List[Any] = []

    async def write(self, key: Any, value: Any) -> bool:
        """记录一次写入并让后续读取看到新取值。

        :param key: 配置键
        :param value: 待写入取值
        :return: 恒为 True
        """
        assert key is SystemConfigKey.MetaParserOrder
        self.writes.append(value)
        self.value = value
        return True


@pytest.fixture
def config_store() -> Iterator[_ConfigStore]:
    """接管顺序配置的读写两端，让写入立刻对裁决可见。"""
    store = _ConfigStore()
    configure_meta_parser_order_reader(lambda: store.value)
    with patch(
        "app.application.metaparser.async_write_system_setting", new=store.write
    ):
        yield store


def _register(parser_id: str, priority: int = 0, owner: Optional[str] = None) -> str:
    """登记一个测试用解析环并返回其解析环标识。

    :param parser_id: 声明标识
    :param priority: 声明的默认顺序
    :param owner: 登记方实例键
    :return: 解析环标识
    """
    return meta_parser_registry.register(
        parser_id,
        lambda _request: ParsedMeta(),
        name=f"{parser_id} 解析",
        priority=priority,
        owner=owner,
        distribution=ExtensionDistribution.MARKET,
    )


def _parsers(pipeline: MetaParserPipeline) -> List[str]:
    """取出管道中各环的标识。

    :param pipeline: 管道呈现
    :return: 按生效顺序排列的解析环标识
    """
    return [ring.parser for ring in pipeline.rings]


def _ring(pipeline: MetaParserPipeline, parser: str):
    """按标识取出管道中的一环。

    :param pipeline: 管道呈现
    :param parser: 解析环标识
    :return: 该环的呈现
    """
    return next(ring for ring in pipeline.rings if ring.parser == parser)


def test_pipeline_appends_unconfigured_rings_by_declared_priority(config_store):
    """未被用户排到的环按声明 priority 追加在末尾，priority 相同再按标识排序。"""
    _register("late", priority=10)
    _register("early", priority=1)
    _register("beta", priority=1)
    _register("alpha", priority=1)

    pipeline = metaparser_endpoint.parser_pipeline(None)

    assert _parsers(pipeline) == [
        BUILTIN_META_PARSER, "alpha", "beta", "early", "late"
    ]
    assert [ring.configured for ring in pipeline.rings[1:]] == [False] * 4


def test_pipeline_returns_effective_order_not_raw_config(config_store):
    """端点交出的是裁决后的生效顺序，配置里没排到的环也占到自己的位次。"""
    _register("configured_one")
    _register("configured_two")
    _register("unconfigured", priority=5)
    config_store.value = [{"parser": "configured_two"}, {"parser": "configured_one"}]

    pipeline = metaparser_endpoint.parser_pipeline(None)

    assert _parsers(pipeline) == [
        BUILTIN_META_PARSER, "configured_two", "configured_one", "unconfigured"
    ]
    assert _ring(pipeline, "configured_two").configured is True
    assert _ring(pipeline, "unconfigured").configured is False
    assert [ring.order for ring in pipeline.rings] == [0, 1, 2, 3]


def test_pipeline_lists_the_builtin_ring_first_and_pinned(config_store):
    """内建环出现在列表首位并标记为宿主固定，用户看得到扩展环接在什么之后。"""
    _register("llm", owner="AIMetaPlugin@alt")

    pipeline = metaparser_endpoint.parser_pipeline(None)
    builtin = pipeline.rings[0]

    assert builtin.parser == BUILTIN_META_PARSER
    assert builtin.name == BUILTIN_META_PARSER_NAME
    assert builtin.pinned is True
    assert builtin.enabled is True
    assert builtin.owner is None
    assert builtin.extension_id is None
    assert builtin.instance_id is None
    assert _ring(pipeline, "AIMetaPlugin@alt#llm").pinned is False


def test_pipeline_splits_the_ring_token_into_plugin_instance_and_parser(config_store):
    """解析环标识按插件、分身、环三段拆开呈现，用户不必自己认一串标识。"""
    _register("llm", owner="AIMetaPlugin@alt")
    _register("llm", owner="AIMetaPlugin")

    pipeline = metaparser_endpoint.parser_pipeline(None)
    alt = _ring(pipeline, "AIMetaPlugin@alt#llm")
    default = _ring(pipeline, "AIMetaPlugin#llm")

    assert (alt.extension_id, alt.instance_id, alt.parser_id) == (
        "AIMetaPlugin", "alt", "llm"
    )
    assert alt.owner == "AIMetaPlugin@alt"
    assert alt.name == "llm 解析"
    assert (default.extension_id, default.instance_id, default.parser_id) == (
        "AIMetaPlugin", "default", "llm"
    )


def test_saving_order_changes_the_effective_order(config_store):
    """保存新顺序后，生效顺序随之改变。"""
    _register("first")
    _register("second")

    before = metaparser_endpoint.parser_pipeline(None)
    after = asyncio.run(metaparser_endpoint.save_parser_order(
        [
            MetaParserOrderEntry(parser="second"),
            MetaParserOrderEntry(parser="first"),
        ],
        None,
    ))

    assert _parsers(before) == [BUILTIN_META_PARSER, "first", "second"]
    assert _parsers(after) == [BUILTIN_META_PARSER, "second", "first"]
    assert config_store.writes == [
        [{"parser": "second", "enabled": True}, {"parser": "first", "enabled": True}]
    ]
    assert _parsers(metaparser_endpoint.parser_pipeline(None)) == _parsers(after)


def test_toggling_a_ring_off_removes_it_from_execution(config_store):
    """单独停用一环后它不再参与执行，但仍留在列表里的原位次上。"""
    _register("kept")
    _register("noisy")

    pipeline = asyncio.run(metaparser_endpoint.toggle_parser(
        MetaParserToggle(parser="noisy", enabled=False), None
    ))

    assert _parsers(pipeline) == [BUILTIN_META_PARSER, "kept", "noisy"]
    assert _ring(pipeline, "noisy").enabled is False
    assert [entry.parser for entry in meta_parser_registry.resolved_entries()] == ["kept"]


def test_toggling_a_ring_back_on_restores_execution(config_store):
    """停用后再启用，该环重新参与执行。"""
    _register("kept")
    _register("noisy")
    asyncio.run(metaparser_endpoint.toggle_parser(
        MetaParserToggle(parser="noisy", enabled=False), None
    ))

    asyncio.run(metaparser_endpoint.toggle_parser(
        MetaParserToggle(parser="noisy", enabled=True), None
    ))

    assert [entry.parser for entry in meta_parser_registry.resolved_entries()] == [
        "kept", "noisy"
    ]


def test_toggling_keeps_the_ring_in_place_instead_of_moving_it_to_the_end(config_store):
    """启停连同当前可见位次一并落盘，用户改的是启停就不该看到顺序也变了。"""
    _register("aaa", priority=1)
    _register("bbb", priority=2)
    _register("ccc", priority=3)

    pipeline = asyncio.run(metaparser_endpoint.toggle_parser(
        MetaParserToggle(parser="aaa", enabled=False), None
    ))

    assert _parsers(pipeline) == [BUILTIN_META_PARSER, "aaa", "bbb", "ccc"]


def test_saving_order_keeps_entries_whose_plugin_is_currently_stopped(config_store):
    """当前未登记但此前排过的环留在原位次上，插件复跑时不会跳到末尾。"""
    _register("here")
    config_store.value = [{"parser": "stopped"}, {"parser": "here"}]

    asyncio.run(metaparser_endpoint.save_parser_order(
        [MetaParserOrderEntry(parser="here")], None
    ))

    assert config_store.writes == [
        [{"parser": "stopped", "enabled": True}, {"parser": "here", "enabled": True}]
    ]


def test_pipeline_does_not_depend_on_registration_order(config_store):
    """列举顺序只由裁决规则决定，与登记先后无关。"""
    _register("zzz", priority=1)
    _register("aaa", priority=1)
    forward = _parsers(metaparser_endpoint.parser_pipeline(None))

    meta_parser_registry._entries.clear()
    _register("aaa", priority=1)
    _register("zzz", priority=1)
    backward = _parsers(metaparser_endpoint.parser_pipeline(None))

    assert forward == backward == [BUILTIN_META_PARSER, "aaa", "zzz"]


def test_builtin_ring_cannot_be_reordered_or_switched_off(config_store):
    """内建环在管道之外先跑，排定与启停都要被拒而不是静默无效。"""
    with pytest.raises(HTTPException) as ordered:
        asyncio.run(metaparser_endpoint.save_parser_order(
            [MetaParserOrderEntry(parser=BUILTIN_META_PARSER)], None
        ))
    with pytest.raises(HTTPException) as toggled:
        asyncio.run(metaparser_endpoint.toggle_parser(
            MetaParserToggle(parser=BUILTIN_META_PARSER, enabled=False), None
        ))

    assert ordered.value.status_code == 400
    assert toggled.value.status_code == 400
    assert config_store.writes == []


def test_saving_a_duplicated_ring_is_rejected(config_store):
    """同一环在顺序里出现多次会让位次含义不明，直接退回。"""
    _register("dup")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(metaparser_endpoint.save_parser_order(
            [
                MetaParserOrderEntry(parser="dup"),
                MetaParserOrderEntry(parser="dup"),
            ],
            None,
        ))

    assert exc_info.value.status_code == 400
    assert config_store.writes == []


def test_toggling_an_unregistered_ring_maps_to_404(config_store):
    """启停一个没登记的环返回 404，不落盘一条指向不存在解析环的配置。"""
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(metaparser_endpoint.toggle_parser(
            MetaParserToggle(parser="ghost", enabled=False), None
        ))

    assert exc_info.value.status_code == 404
    assert config_store.writes == []


def test_response_model_keeps_every_field_the_pipeline_endpoint_returns(config_store):
    """管道端点返回的嵌套字段必须全部能穿过响应模型，否则会被 FastAPI 静默裁掉。"""
    _register("llm", owner="AIMetaPlugin@alt", priority=7)

    payload = metaparser_endpoint.parser_pipeline(None)
    serialized = MetaParserPipeline(**payload.model_dump()).model_dump()

    assert set(serialized) == {"rings"}
    assert len(serialized["rings"]) == 2
    ring = serialized["rings"][1]
    assert set(ring) == set(payload.rings[1].model_dump())
    assert ring["parser"] == "AIMetaPlugin@alt#llm"
    assert ring["parser_id"] == "llm"
    assert ring["extension_id"] == "AIMetaPlugin"
    assert ring["instance_id"] == "alt"
    assert ring["priority"] == 7
    assert ring["distribution"] == ExtensionDistribution.MARKET.value


def test_resolved_entries_stay_in_step_with_the_listed_order(config_store):
    """列表中启用的那些环，其相对次序必须与管道实际执行的次序一致。"""
    _register("one")
    _register("two")
    _register("three")
    config_store.value = [
        {"parser": "three"},
        {"parser": "two", "enabled": False},
        {"parser": "one"},
    ]

    pipeline = metaparser_endpoint.parser_pipeline(None)

    listed = [ring.parser for ring in pipeline.rings if ring.enabled and not ring.pinned]
    assert listed == [entry.parser for entry in meta_parser_registry.resolved_entries()]
    assert listed == ["three", "one"]


def test_declared_parser_id_survives_a_separator_inside_the_instance_id(config_store):
    """实例标识里含分隔符时，声明标识仍按实例键长度切分，不会切错位置。"""
    token = _register("llm", owner="AIMetaPlugin@a#b")

    ring = _ring(metaparser_endpoint.parser_pipeline(None), token)

    assert ring.parser_id == "llm"
    assert ring.owner == "AIMetaPlugin@a#b"
    assert ring.instance_id == "a#b"


def test_service_reads_the_same_registry_the_pipeline_runs(config_store):
    """应用服务与解析管道读同一份登记，端点看到的就是识别时会跑的那些环。"""
    _register("only")

    pipeline = MetaParserPipelineService().list_pipeline()

    assert [ring.parser for ring in pipeline.rings if not ring.pinned] == [
        entry.parser for entry in meta_parser_registry.resolved_entries()
    ]


def test_pipeline_endpoints_require_superuser():
    """顺序改的是全局识别行为，读写都必须限管理员。"""
    def dependency(func: Any) -> Any:
        """读取端点参数上声明的依赖函数。"""
        return inspect.signature(func).parameters["_"].default.dependency

    assert dependency(metaparser_endpoint.parser_pipeline) is get_current_active_superuser
    assert dependency(metaparser_endpoint.save_parser_order) is get_current_active_superuser_async
    assert dependency(metaparser_endpoint.toggle_parser) is get_current_active_superuser_async


def test_generic_system_setting_endpoint_still_reads_the_same_key(config_store):
    """专属端点写下的顺序，通用 /system/setting/{key} 那条路仍然读得到。"""
    from app.api.endpoints import system as system_endpoint

    _register("first")
    _register("second")
    asyncio.run(metaparser_endpoint.save_parser_order(
        [MetaParserOrderEntry(parser="second"), MetaParserOrderEntry(parser="first")],
        None,
    ))

    with patch(
        "app.api.endpoints.system.read_system_setting",
        return_value=config_store.value,
    ):
        response = asyncio.run(
            system_endpoint.get_setting(SystemConfigKey.MetaParserOrder.value, None)
        )

    assert response.success is True
    assert response.data["value"] == [
        {"parser": "second", "enabled": True}, {"parser": "first", "enabled": True}
    ]


@pytest.mark.parametrize("locale", ["en-US", "zh-TW"])
def test_rejection_details_have_translations(config_store, locale: str):
    """端点退回的中文说明经 detail 走本地化，须有译文，否则前端切语言后回退中文。

    这些说明由应用层抛出、端点以 ``str(error)`` 转交，接口文案的静态扫描看不到它们。
    """
    from app.runtime.localization import LocaleHelper

    _register("dup")
    raised: List[str] = []
    for call in (
        lambda: metaparser_endpoint.save_parser_order(
            [MetaParserOrderEntry(parser=BUILTIN_META_PARSER)], None
        ),
        lambda: metaparser_endpoint.save_parser_order(
            [MetaParserOrderEntry(parser="dup"), MetaParserOrderEntry(parser="dup")],
            None,
        ),
        lambda: metaparser_endpoint.toggle_parser(
            MetaParserToggle(parser=BUILTIN_META_PARSER, enabled=False), None
        ),
        lambda: metaparser_endpoint.toggle_parser(
            MetaParserToggle(parser="ghost", enabled=False), None
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(call())
        raised.append(str(exc_info.value.detail))

    assert len(raised) == 4
    for detail in raised:
        assert LocaleHelper.translate_text(detail, locale=locale) != detail


@pytest.mark.parametrize("locale", ["en-US", "zh-TW"])
def test_builtin_ring_name_has_a_translation(config_store, locale: str):
    """内建环的展示名称是用户可见文案，须有译文。"""
    from app.runtime.localization import LocaleHelper

    name = metaparser_endpoint.parser_pipeline(None).rings[0].name

    assert LocaleHelper.translate_text(name, locale=locale) != name


def test_unregistered_ring_disappears_from_the_pipeline(config_store):
    """插件停用后其解析环不再出现在列表里，配置里的那条不会凭空造出一环。"""
    _register("gone")
    config_store.value = [{"parser": "gone"}]

    meta_parser_registry.unregister("gone")

    assert _parsers(metaparser_endpoint.parser_pipeline(None)) == [BUILTIN_META_PARSER]
