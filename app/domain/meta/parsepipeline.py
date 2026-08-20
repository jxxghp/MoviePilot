"""名称解析扩展环的领域端口。

领域层不认识扩展分发，管道实现由组合根注入；端口签名里只出现 `ParsedMeta` 及
内置类型，不出现领域富对象，因此这条扩展点在扩展改由独立进程承载时依然成立。

内建解析在管道之外先跑完并作为第一环记入溯源：门面因此永远拿得到一个可用的
`MetaBase`，扩展环只在内建结果之上继续增强或纠正。
"""

import logging
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from app.domain.meta.metabase import MetaBase
from app.schemas.media import normalize_media_source
from app.schemas.metaparse import (
    PARSED_META_FIELDS,
    MetaParseOutcome,
    MetaParseRequest,
    MetaParseTrace,
    ParsedMeta,
)
from app.schemas.types import MediaType


logger = logging.getLogger(__name__)

# 非空槽位在取值被清空时的回落值，其余槽位清空即为 None
_SLOT_EMPTY_VALUES: Dict[str, Any] = {
    "isfile": False,
    "title": "",
    "type": MediaType.UNKNOWN,
    "total_season": 0,
    "total_episode": 0,
}

# 必须成对写回的媒体主身份槽位
_MEDIA_IDENTITY_FIELDS = ("media_source", "media_id")


@runtime_checkable
class MetaParsePipelineProvider(Protocol):
    """名称解析管道的执行协议。"""

    def meta_parser_count(self) -> int:
        """
        返回当前可执行的解析环数量。

        :return: 解析环数量，为 0 时领域层跳过整条管道
        """
        ...

    def run_meta_parsers(self, request: MetaParseRequest) -> MetaParseOutcome:
        """
        按用户排定的顺序执行全部解析环。

        :param request: 含标题、路径上下文与内建环已填结果的解析请求
        :return: 各环累积出的结果与字段级溯源
        """
        ...


_pipeline: Optional[MetaParsePipelineProvider] = None


def configure_meta_parse_pipeline(pipeline: Optional[MetaParsePipelineProvider]) -> None:
    """
    注入名称解析管道实现

    :param pipeline: 管道实现，为 None 时领域层只运行内建解析
    :return: 无返回值
    """
    global _pipeline
    _pipeline = pipeline


def get_meta_parse_pipeline() -> Optional[MetaParsePipelineProvider]:
    """
    返回当前已注入的名称解析管道实现

    :return: 管道实现；组合根尚未注入时为 None
    """
    return _pipeline


def meta_to_parsed(meta: MetaBase) -> ParsedMeta:
    """
    把领域元数据对象投影为纯数据解析结果

    枚举槽位退化为其取值字符串，投影后不含任何领域对象引用。

    :param meta: 内建解析产出的元数据对象
    :return: 与该对象等价的解析结果
    """
    fields = {field: getattr(meta, field, None) for field in PARSED_META_FIELDS}
    fields["type"] = meta.type.value if meta.type else None
    fields["media_source"] = str(meta.media_source) if meta.media_source else None
    fields["apply_words"] = (
        list(meta.apply_words) if meta.apply_words is not None else None
    )
    return ParsedMeta(**fields)


def apply_parsed_meta(meta: MetaBase, parsed: ParsedMeta, trace: MetaParseTrace) -> MetaBase:
    """
    把管道产出的解析结果写回元数据对象，并挂上字段级溯源

    媒体主身份两个槽位成对写回：只写回其中一个会让来源与 ID 配不上对。

    :param meta: 内建解析产出的元数据对象
    :param parsed: 管道产出的最终解析结果
    :param trace: 本次解析的字段级溯源
    :return: 写回后的元数据对象
    """
    for field in PARSED_META_FIELDS:
        if field in _MEDIA_IDENTITY_FIELDS:
            continue
        value = _slot_value(field, getattr(parsed, field))
        if value != getattr(meta, field, None):
            setattr(meta, field, value)
    _apply_media_identity(meta, parsed)
    meta.parse_trace = trace
    return meta


def _slot_value(field: str, value: Any) -> Any:
    """
    把解析结果中的取值归一为槽位可接受的形式

    :param field: 槽位名
    :param value: 解析结果中的取值
    :return: 可直接写入槽位的取值
    """
    if value is None:
        return _SLOT_EMPTY_VALUES.get(field)
    if field == "type":
        return _media_type(value)
    if field == "apply_words":
        return list(value)
    return value


def _media_type(value: Any) -> MediaType:
    """
    把媒体类型取值归一为枚举成员

    :param value: 媒体类型取值
    :return: 对应的枚举成员；取值不是任何成员时为未知类型
    """
    try:
        return MediaType(value)
    except ValueError:
        logger.warning(f"名称解析返回了未知的媒体类型：{value!r}")
        return MediaType.UNKNOWN


def _apply_media_identity(meta: MetaBase, parsed: ParsedMeta) -> None:
    """
    成对写回媒体主身份

    来源与 ID 只剩其一时整对不写：半对身份会与上游残留的另一半拼成一个指向错误
    条目的组合，比身份缺失更难排查。

    :param meta: 元数据对象
    :param parsed: 管道产出的最终解析结果
    :return: 无返回值
    """
    source = normalize_media_source(parsed.media_source) if parsed.media_source else None
    media_id = parsed.media_id or None
    current = (meta.media_source, meta.media_id)
    if (source, media_id) == current:
        return
    if bool(source) != bool(media_id):
        logger.warning(
            f"名称解析返回的媒体身份不成对，已忽略：来源 {parsed.media_source!r}、"
            f"ID {parsed.media_id!r}"
        )
        return
    meta.media_source, meta.media_id = source, media_id


def enhance_meta(
    meta: MetaBase,
    title: str,
    subtitle: Optional[str] = None,
    custom_words: Optional[List[str]] = None,
    path: Optional[str] = None,
) -> MetaBase:
    """
    在内建解析结果之上执行扩展解析环

    没有注入管道、或一个解析环都没登记时原样返回，热路径不做任何数据转换。

    :param meta: 内建解析产出的元数据对象
    :param title: 标题、种子名或文件名
    :param subtitle: 副标题、描述
    :param custom_words: 本次识别使用的自定义识别词
    :param path: 按路径识别时的完整路径
    :return: 扩展环增强后的元数据对象
    """
    pipeline = _pipeline
    if pipeline is None:
        return meta
    try:
        if pipeline.meta_parser_count() <= 0:
            return meta
        outcome = pipeline.run_meta_parsers(MetaParseRequest(
            title=title or "",
            subtitle=subtitle,
            path=path,
            custom_words=tuple(custom_words or ()),
            parsed=meta_to_parsed(meta),
        ))
    except Exception as error:
        logger.error(f"名称解析扩展环执行出错，已回落到内建识别结果：{str(error)}")
        return meta
    return apply_parsed_meta(meta, outcome.parsed, outcome.trace)
