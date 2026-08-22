"""名称解析管道测试：契约字段集、用户顺序、字段级溯源、失败隔离与内建兜底。

名称解析从「一个写死三岔的裸函数」变成「内建环加可扩展环的管道」，本文件覆盖
从声明契约到 `MetaInfo()` 门面的整条链，并锁死两件一旦定错就要让所有社区解析器
返工的事：`ParsedMeta` 的字段集必须覆盖 `MetaBase` 的全部槽位，覆盖行为必须留下
字段级溯源。
"""

import inspect
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any, Iterator, List, Optional
from unittest.mock import patch

import pytest

from app.domain.meta.metabase import MetaBase
from app.domain.meta.parsepipeline import meta_to_parsed
from app.domain.metainfo import MetaInfo, MetaInfoPath
from app.runtime.extensions.contract.extension import ExtensionDistribution
from app.runtime.extensions.contract.declaration import MetaParserDeclaration
from app.runtime.extensions.registry.meta_parser import (
    configure_meta_parser_order_reader,
    meta_parser_registry,
    meta_parser_token,
)
from app.runtime.extensions.admission.meta_parser import (
    meta_parser_declaration_violation,
)
from app.runtime.extensions.projection.plugin import PluginProjection
from app.schemas.metaparse import (
    BUILTIN_META_PARSER,
    PARSED_META_FIELDS,
    MetaParseRequest,
    MetaParseStatus,
    ParsedMeta,
)
from app.schemas.types import MediaSource, MediaType


@pytest.fixture(autouse=True)
def isolate_meta_parser_registry() -> Iterator[None]:
    """快照并复原解析器注册表与顺序配置，避免用例相互污染。"""
    original = dict(meta_parser_registry._entries)
    previous_reader = configure_meta_parser_order_reader(lambda: None)
    try:
        yield
    finally:
        configure_meta_parser_order_reader(previous_reader)
        meta_parser_registry._entries.clear()
        meta_parser_registry._entries.update(original)


def _register(parser_id: str, invoke: Any, priority: int = 0, owner: Optional[str] = None) -> str:
    """登记一个测试用解析环并返回其解析器标识。

    :param parser_id: 声明标识
    :param invoke: 解析环实现
    :param priority: 声明的默认顺序
    :param owner: 登记方实例键
    :return: 解析器标识
    """
    return meta_parser_registry.register(
        parser_id, invoke, name=parser_id, priority=priority, owner=owner,
        distribution=ExtensionDistribution.MARKET,
    )


def _order(*items: Any) -> None:
    """接管顺序配置读取端口，用例给出的清单即用户排定的顺序。

    :param items: 顺序配置项，字符串或含 parser/enabled 的字典
    :return: 无返回值
    """
    configure_meta_parser_order_reader(lambda: list(items))


def _writes(field: str, value: Any):
    """构造一个固定写入某字段的解析环。

    :param field: 字段名
    :param value: 写入的取值
    :return: 解析环实现
    """
    def parse(_request: MetaParseRequest) -> ParsedMeta:
        return ParsedMeta(**{field: value})

    return parse


def test_parsed_meta_fields_cover_every_metabase_slot():
    """解析契约的字段集必须与 MetaBase 的槽位完全对齐。"""
    slots = {
        field.name for field in dataclass_fields(MetaBase)
        # 溯源是宿主写给用户看的诊断数据，不是解析器可声明的字段
        if field.name != "parse_trace"
    }

    assert set(PARSED_META_FIELDS) == slots


def test_parsed_meta_declares_no_field_outside_the_contract():
    """契约里除槽位外只允许 clears 一个字段，避免解析器把宿主内部结构当成字段。"""
    assert set(ParsedMeta.model_fields) == {*PARSED_META_FIELDS, "clears"}


def test_meta_to_parsed_keeps_enum_slots_as_plain_data():
    """投影出的解析结果只含普通数据类型，枚举退化为取值字符串。"""
    meta = MetaInfo("Some.Show.S01E02.1080p")
    meta.media_source, meta.media_id = MediaSource.TMDB, "1399"

    parsed = meta_to_parsed(meta)

    assert parsed.type == MediaType.TV.value
    assert parsed.media_source == MediaSource.TMDB.value
    assert all(
        isinstance(getattr(parsed, field), (str, int, bool, list, type(None)))
        for field in PARSED_META_FIELDS
    )


def test_pipeline_runs_in_user_configured_order():
    """管道按用户排定的顺序执行，后跑的一环覆盖先跑的一环。"""
    _register("first", _writes("cn_name", "先手"))
    _register("second", _writes("cn_name", "后手"))
    _order("first", "second")

    assert MetaInfo("Some.Show.2020").cn_name == "后手"


def test_reordering_user_config_changes_the_result():
    """同一组解析器改一次顺序，结果随之改变。"""
    _register("first", _writes("cn_name", "先手"))
    _register("second", _writes("cn_name", "后手"))
    _order("second", "first")

    assert MetaInfo("Some.Show.2020").cn_name == "先手"


def test_declared_priority_is_only_the_default_order():
    """未被用户排到的解析器按声明 priority 排序，用户排过的一律以配置为准。"""
    _register("low", _writes("cn_name", "低优先"), priority=10)
    _register("high", _writes("cn_name", "高优先"), priority=1)

    # 用户没排过：priority 小的先跑，后跑的 low 覆盖 high
    assert MetaInfo("Some.Show.2020").cn_name == "低优先"

    # 用户排过：声明的 priority 不再参与，顺序完全由配置决定
    _order("low", "high")
    assert MetaInfo("Some.Show.2020").cn_name == "高优先"


def test_user_config_can_switch_off_a_single_parser():
    """顺序配置里标记停用的解析环整环不执行。"""
    _register("noisy", _writes("cn_name", "不该出现"))
    _register("kept", _writes("year", "2021"))
    _order({"parser": "noisy", "enabled": False}, {"parser": "kept", "enabled": True})

    meta = MetaInfo("Some.Show.2020")

    assert meta.cn_name != "不该出现"
    assert meta.year == "2021"


def test_downstream_parser_overrides_upstream_field_and_records_provenance():
    """下游环可以改写内建环填错的字段，覆盖必须留下来源与原值。"""
    seen = {}

    def fix_name(request: MetaParseRequest) -> ParsedMeta:
        seen.update(year=request.parsed.year, en_name=request.parsed.en_name)
        # 内建把片名当年份填进了 year，改名的同时把错填的年份抹掉
        return ParsedMeta(en_name="Nineteen Seventeen", clears=("year",))

    _register("fixer", fix_name)

    meta = MetaInfo("1917.2019.1080p")

    assert seen == {"year": "2019", "en_name": "1917"}
    assert meta.en_name == "Nineteen Seventeen"
    assert meta.year is None
    trace = meta.parse_trace
    assert trace.origin("en_name") == "fixer"
    assert [
        (revision.parser, revision.value) for revision in trace.overridden("en_name")
    ] == [(BUILTIN_META_PARSER, "1917")]
    assert trace.origin("year") == "fixer"
    assert [
        (revision.parser, revision.value) for revision in trace.overridden("year")
    ] == [(BUILTIN_META_PARSER, "2019")]
    assert trace.runs[0].status == MetaParseStatus.CLAIMED


def test_provenance_records_every_ring_outcome():
    """每一环是认领、弃权还是出错都进溯源，用户据此定位该关掉谁。"""
    _register("claimed", _writes("customization", "REMUX"))
    _register("abstained", lambda _request: None)
    _register("broken", _mode_error)
    _order("claimed", "abstained", "broken")

    runs = MetaInfo("Some.Show.2020").parse_trace.runs

    assert [(run.parser, run.status) for run in runs] == [
        ("claimed", MetaParseStatus.CLAIMED),
        ("abstained", MetaParseStatus.ABSTAINED),
        ("broken", MetaParseStatus.FAILED),
    ]
    assert runs[0].fields == ("customization",)
    assert runs[2].error


def _mode_error(_request: MetaParseRequest) -> ParsedMeta:
    """执行即抛异常的解析环。"""
    raise RuntimeError("解析器炸了")


def test_failing_parser_only_skips_itself():
    """一环抛错只跳过这一环，链条继续且最终仍返回可用结果。"""
    _register("broken", _mode_error)
    _register("later", _writes("resource_pix", "2160p"))
    _order("broken", "later")

    meta = MetaInfo("Some.Show.2020.1080p")

    assert meta.resource_pix == "2160p"
    assert meta.name


def test_parser_returning_wrong_type_is_skipped_not_fatal():
    """交回不合契约的结果按失败处理，同样只跳过这一环。"""
    _register("wrong", lambda _request: {"cn_name": "字典不是契约"})
    _register("later", _writes("cn_name", "接着跑"))
    _order("wrong", "later")

    meta = MetaInfo("Some.Show.2020")

    assert meta.cn_name == "接着跑"
    assert meta.parse_trace.runs[0].status == MetaParseStatus.FAILED


def test_empty_result_counts_as_claimed_and_none_counts_as_abstained():
    """让出协议：只有 None 算未认领，交回空结果算已认领但没改动。"""
    _register("empty", lambda _request: ParsedMeta())
    _register("abstained", lambda _request: None)
    _order("empty", "abstained")

    runs = MetaInfo("Some.Show.2020").parse_trace.runs

    assert runs[0].status == MetaParseStatus.CLAIMED
    assert runs[0].fields == ()
    assert runs[1].status == MetaParseStatus.ABSTAINED


@pytest.mark.parametrize(
    "title",
    [
        "Some.Show.2020.1080p.BluRay.x264-GROUP",
        "权力的游戏 S01E05 1080p",
        "[Group] Anime Name - 03 [1080p][HEVC][10bit]",
        "Movie.Name.2019.Part1.2160p.WEB-DL.DDP5.1.HDR",
    ],
)
def test_claiming_nothing_leaves_every_slot_untouched(title):
    """空贡献不得改动任何槽位：结果与不跑管道时逐字段一致。"""
    expected = MetaInfo(title).to_dict()

    _register("noop", lambda _request: ParsedMeta())

    assert MetaInfo(title).to_dict() == expected


def test_builtin_ring_never_abstains():
    """内建环恒不弃权：无论输入多离谱，门面永远返回 MetaBase 而不是 None。"""
    _register("abstained", lambda _request: None)

    for title in ["", "   ", "????", "不知道这是什么"]:
        meta = MetaInfo(title)
        assert isinstance(meta, MetaBase)


def test_builtin_result_survives_a_pipeline_that_fails_entirely():
    """管道整体不可用时回落到内建识别结果，而不是让识别失败。"""
    _register("broken", _mode_error)

    with patch(
        "app.domain.meta.parsepipeline.MetaParseRequest", side_effect=RuntimeError("请求构造失败")
    ):
        meta = MetaInfo("Some.Show.2020.1080p.BluRay.x264")

    assert isinstance(meta, MetaBase)
    assert meta.resource_pix == "1080p"


def test_media_identity_is_applied_as_a_pair():
    """媒体身份成对写回，只给出半对时整对忽略。"""
    _register("half", lambda _request: ParsedMeta(media_id="1399"))
    assert MetaInfo("Some.Show.2020").media_id is None

    meta_parser_registry.unregister(meta_parser_token("half", None))
    _register("pair", lambda _request: ParsedMeta(
        media_source=MediaSource.TMDB.value, media_id="1399"
    ))
    meta = MetaInfo("Some.Show.2020")

    assert (meta.media_source, meta.media_id) == (MediaSource.TMDB, "1399")


def test_clearing_a_non_optional_slot_falls_back_to_its_default():
    """清空非空槽位回落到槽位默认值，不会把 int 槽位写成 None。"""
    _register("cleaner", lambda _request: ParsedMeta(clears=("total_episode", "title")))

    meta = MetaInfo("Some.Show.S01E02")

    assert meta.total_episode == 0
    assert meta.title == ""


def test_unknown_clear_target_is_ignored():
    """要求清空契约外的字段只忽略该项，其余贡献照常生效。"""
    _register("odd", lambda _request: ParsedMeta(year="2020", clears=("not_a_field",)))

    assert MetaInfo("Some.Show").year == "2020"


def test_parser_sees_upstream_result_and_path_context():
    """解析环拿到的是上游累积结果与本次识别的上下文。"""
    seen: List[MetaParseRequest] = []

    def record(request: MetaParseRequest) -> None:
        seen.append(request)
        return None

    _register("recorder", record)
    MetaInfoPath(Path("/media/Movies/Some.Show (2020)/Some.Show.2020.1080p.mkv"))

    assert seen[0].path == "/media/Movies/Some.Show (2020)/Some.Show.2020.1080p.mkv"
    assert seen[0].title == "Some.Show.2020.1080p.mkv"
    assert seen[0].parsed.year == "2020"


def test_pipeline_does_not_touch_the_hot_path_without_parsers():
    """一个解析环都没登记时不做任何数据转换，识别热路径原样保持。"""
    with patch(
        "app.domain.meta.parsepipeline.meta_to_parsed", side_effect=AssertionError("不该转换")
    ):
        meta = MetaInfo("Some.Show.2020.1080p")

    assert meta.resource_pix == "1080p"
    assert meta.parse_trace is None


def test_rust_fast_path_still_answers_first():
    """Rust 快路仍然优先应答，Python 解析器不参与，扩展环在其结果上继续。"""
    parsed = {
        "kind": "video", "title": "Rust Show", "type": MediaType.TV.value,
        "cn_name": None, "en_name": "Rust Show", "year": "2020", "begin_season": 1,
    }
    _register("later", _writes("resource_pix", "2160p"))

    with patch("app.adapters.system.rust.parse_metainfo", return_value=parsed) as rust_parse, \
            patch(
                "app.domain.metainfo._build_meta_info",
                side_effect=AssertionError("Rust 命中时不应回落到 Python 解析"),
            ):
        meta = MetaInfo("Rust.Show.2020")

    assert rust_parse.called
    assert meta.en_name == "Rust Show"
    assert meta.resource_pix == "2160p"
    assert meta.parse_trace.origin("en_name") == BUILTIN_META_PARSER


def test_rust_path_fast_path_still_answers_first():
    """按路径识别时 Rust 快路同样优先，逐级目录解析不再重复执行。"""
    parsed = {"kind": "video", "en_name": "Rust Show", "type": MediaType.MOVIE.value}

    with patch("app.adapters.system.rust.parse_metainfo_path", return_value=parsed) as rust_parse, \
            patch(
                "app.domain.metainfo._merged_path_meta",
                side_effect=AssertionError("Rust 命中时不应逐级回落到 Python 解析"),
            ):
        meta = MetaInfoPath(Path("/media/Movies/Rust.Show.2020/Rust.Show.2020.1080p.mkv"))

    assert rust_parse.called
    assert meta.en_name == "Rust Show"


def test_facade_signature_and_return_type_are_unchanged():
    """门面签名与返回类型不变，99 处既有调用点不受影响。"""
    signature = inspect.signature(MetaInfo)

    assert list(signature.parameters) == ["title", "subtitle", "custom_words", "force_video"]
    assert signature.return_annotation is MetaBase
    assert [
        (name, parameter.default)
        for name, parameter in signature.parameters.items()
    ] == [("title", inspect.Parameter.empty), ("subtitle", None),
          ("custom_words", None), ("force_video", False)]

    path_signature = inspect.signature(MetaInfoPath)
    assert list(path_signature.parameters) == ["path", "custom_words", "force_video"]
    assert path_signature.return_annotation is MetaBase


def test_music_branch_is_out_of_the_video_parsing_contract():
    """音频文件仍直接产出音乐元数据，不进入影视解析管道。"""
    _register("noisy", _writes("cn_name", "不该出现"))

    meta = MetaInfo("Artist - Song.flac")

    assert meta.type == MediaType.MUSIC
    assert meta.cn_name != "不该出现"


def test_schema_metainfo_model_and_domain_metainfo_function_are_distinct():
    """同名的识别门面函数与序列化模型必须始终可区分，避免 import 写错。"""
    from app.domain.metainfo import MetaInfo as domain_metainfo
    from app.schemas.context import MetaInfo as schema_metainfo
    from pydantic import BaseModel

    assert inspect.isfunction(domain_metainfo)
    assert inspect.isclass(schema_metainfo) and issubclass(schema_metainfo, BaseModel)
    assert domain_metainfo is not schema_metainfo
    assert isinstance(domain_metainfo("Some.Show.2020"), MetaBase)


def test_parse_trace_stays_out_of_the_serialized_dict():
    """溯源不进入 to_dict()，既有序列化面与 API 响应形状不变。"""
    _register("writer", _writes("year", "2020"))

    meta = MetaInfo("Some.Show")

    assert meta.parse_trace is not None
    assert "parse_trace" not in meta.to_dict()


class _MetaParserPlugin:
    """声明名称解析器的最小插件桩，用于直接驱动 PluginProjection。"""

    plugin_name = "解析插件"

    def __init__(self, enabled: bool = True, declarations: Any = None, raise_error: bool = False):
        """记录桩的启用状态与要交出的声明。"""
        self._enabled = enabled
        self._declarations = declarations
        self._raise_error = raise_error

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    def provides_meta_parsers(self):
        """返回声明的名称解析器，或按需抛出异常模拟插件实现出错。"""
        if self._raise_error:
            raise RuntimeError("声明解析器时出错")
        return self._declarations


def _parse(_request: MetaParseRequest) -> ParsedMeta:
    """契约合规的解析环实现。"""
    return ParsedMeta(cn_name="插件识别")


async def _async_parse(_request: MetaParseRequest) -> ParsedMeta:
    """协程实现的解析环，识别是同步链路，不该被登记。"""
    return ParsedMeta()


def _two_argument_parse(_request: MetaParseRequest, _extra: Any) -> ParsedMeta:
    """必填两个参数的解析环，宿主只传一个请求对象。"""
    return ParsedMeta()


def test_projection_accepts_a_valid_declaration():
    """契约合规的声明应被接受，字段原样保留。"""
    declaration = MetaParserDeclaration(parser_id="llm", name="大模型识别", impl=_parse)
    projection = PluginProjection({"DemoPlugin": _MetaParserPlugin(declarations=[declaration])})

    assert projection.provided_meta_parsers()["DemoPlugin"] == [declaration]


@pytest.mark.parametrize(
    "declaration",
    [
        MetaParserDeclaration(parser_id="", name="N", impl=_parse),
        MetaParserDeclaration(parser_id="不合法的标识", name="N", impl=_parse),
        MetaParserDeclaration(parser_id="has space", name="N", impl=_parse),
        MetaParserDeclaration(parser_id="llm", name="", impl=_parse),
        MetaParserDeclaration(parser_id="llm", name="N"),
        MetaParserDeclaration(parser_id="llm", name="N", impl="not-callable"),
        MetaParserDeclaration(parser_id="llm", name="N", impl=_async_parse),
        MetaParserDeclaration(parser_id="llm", name="N", impl=_two_argument_parse),
        MetaParserDeclaration(parser_id="llm", name="N", impl=_parse, priority="1"),
        MetaParserDeclaration(parser_id="llm", name="N", impl=_parse, priority=True),
    ],
    ids=[
        "empty_parser_id", "non_ascii_parser_id", "parser_id_with_space", "empty_name",
        "missing_impl", "impl_not_callable", "coroutine_impl", "two_argument_impl",
        "priority_not_int", "priority_bool",
    ],
)
def test_contract_rejects_malformed_declarations(declaration):
    """畸形声明一律拒绝登记，不留到识别时才失败。"""
    assert meta_parser_declaration_violation(declaration) is not None

    projection = PluginProjection({"DemoPlugin": _MetaParserPlugin(declarations=[declaration])})
    assert projection.provided_meta_parsers()["DemoPlugin"] == []


def test_contract_rejects_a_bare_implementation():
    """解析环无法从裸实现推出标识与顺序，不套用「实现即声明」的兼容回落。"""
    assert meta_parser_declaration_violation(_parse) is not None


def test_projection_skips_an_instance_whose_hook_raises():
    """取声明时抛异常只跳过该实例，其余实例不受影响。"""
    projection = PluginProjection({
        "BrokenPlugin": _MetaParserPlugin(raise_error=True),
        "GoodPlugin": _MetaParserPlugin(
            declarations=[MetaParserDeclaration(parser_id="llm", name="N", impl=_parse)]
        ),
    })

    declared = projection.provided_meta_parsers()

    assert "BrokenPlugin" not in declared
    assert len(declared["GoodPlugin"]) == 1


def test_projection_ignores_disabled_instances():
    """停用实例的声明不参与登记。"""
    projection = PluginProjection({
        "DemoPlugin": _MetaParserPlugin(
            enabled=False,
            declarations=[MetaParserDeclaration(parser_id="llm", name="N", impl=_parse)],
        )
    })

    assert projection.provided_meta_parsers() == {}


def test_two_instances_of_one_plugin_are_two_parsers():
    """解析环绑在声明它的分身上，同一插件的两个分身各成一环。"""
    first = _register("llm", _writes("cn_name", "分身一"), owner="DemoPlugin")
    second = _register("llm", _writes("cn_name", "分身二"), owner="DemoPlugin@alt")

    assert first != second
    assert {entry.parser for entry in meta_parser_registry.entries()} == {first, second}

    _order(second, first)
    assert MetaInfo("Some.Show").cn_name == "分身一"


def test_unregister_owner_removes_only_its_own_parsers():
    """按登记方回收只摘除该实例的解析环。"""
    _register("llm", _parse, owner="DemoPlugin")
    kept = _register("llm", _parse, owner="OtherPlugin")

    removed = meta_parser_registry.unregister_owner("DemoPlugin")

    assert removed == (meta_parser_token("llm", "DemoPlugin"),)
    assert [entry.parser for entry in meta_parser_registry.entries()] == [kept]


def test_malformed_order_config_falls_back_to_declared_order():
    """顺序配置整体不可用时按声明顺序执行，单条畸形只跳过该条。"""
    _register("first", _writes("cn_name", "先手"), priority=1)
    _register("second", _writes("cn_name", "后手"), priority=2)

    configure_meta_parser_order_reader(lambda: "not-a-list")
    assert MetaInfo("Some.Show").cn_name == "后手"

    _order({"no_parser_field": 1}, "second", "first")
    assert MetaInfo("Some.Show").cn_name == "先手"


def test_order_config_ignores_unknown_parsers():
    """顺序配置里已卸载的解析器只被忽略，不影响其余环。"""
    _register("present", _writes("year", "2020"))
    _order("已卸载的解析器", "present")

    assert MetaInfo("Some.Show").year == "2020"
