from collections.abc import Iterable
from math import isfinite
from typing import Final, NotRequired, TypedDict, cast

from app.domain.classification.sources import builtin_field_source_support
from app.schemas.category import (
    ClassificationFactScalar,
    ClassificationFieldDefinition,
    ClassificationFieldOption,
    ClassificationMediaType,
    ClassificationOperator,
)

ALL_MEDIA_TYPES: Final[tuple[str, ...]] = ("电影", "电视剧", "音乐")
VIDEO_MEDIA_TYPES: Final[tuple[str, ...]] = ("电影", "电视剧")
MUSIC_MEDIA_TYPES: Final[tuple[str, ...]] = ("音乐",)

VALUE_TYPE_OPERATORS: Final[dict[str, tuple[str, ...]]] = {
    "string": (
        "equals",
        "not_equals",
        "in",
        "not_in",
        "contains",
        "starts_with",
        "ends_with",
        "exists",
        "not_exists",
    ),
    "enum": (
        "equals",
        "not_equals",
        "in",
        "not_in",
        "exists",
        "not_exists",
    ),
    "integer": (
        "equals",
        "not_equals",
        "in",
        "not_in",
        "gt",
        "gte",
        "lt",
        "lte",
        "between",
        "exists",
        "not_exists",
    ),
    "number": (
        "equals",
        "not_equals",
        "in",
        "not_in",
        "gt",
        "gte",
        "lt",
        "lte",
        "between",
        "exists",
        "not_exists",
    ),
    "year": (
        "equals",
        "not_equals",
        "in",
        "not_in",
        "gt",
        "gte",
        "lt",
        "lte",
        "between",
        "exists",
        "not_exists",
    ),
    "string_list": (
        "contains_any",
        "contains_all",
        "contains_none",
        "exists",
        "not_exists",
    ),
    "boolean": (
        "is_true",
        "is_false",
        "exists",
        "not_exists",
    ),
}


class _FieldSpec(TypedDict):
    id: str
    label: str
    group: NotRequired[str]
    description: NotRequired[str]
    value_type: str
    media_types: tuple[str, ...]
    options: NotRequired[tuple[str, ...]]


_STANDARD_FIELD_SPECS: Final[tuple[_FieldSpec, ...]] = (
    {
        "id": "identity.media_source",
        "label": "媒体来源",
        "value_type": "string",
        "media_types": ALL_MEDIA_TYPES,
    },
    {
        "id": "media.type",
        "label": "媒体类型",
        "value_type": "enum",
        "media_types": ALL_MEDIA_TYPES,
        "options": ALL_MEDIA_TYPES,
    },
    {
        "id": "media.year",
        "label": "发行年份",
        "value_type": "year",
        "media_types": ALL_MEDIA_TYPES,
    },
    {
        "id": "media.language",
        "label": "原始语言",
        "value_type": "string",
        "media_types": ALL_MEDIA_TYPES,
    },
    {
        "id": "media.countries",
        "label": "原产国家/地区",
        "description": "跨数据源统一的原产国家或地区代码",
        "value_type": "string_list",
        "media_types": ALL_MEDIA_TYPES,
    },
    {
        "id": "media.genre_keys",
        "label": "风格",
        "description": "跨数据源统一的 MoviePilot 规范风格",
        "value_type": "string_list",
        "media_types": ALL_MEDIA_TYPES,
    },
    {
        "id": "media.genre_names",
        "label": "来源风格",
        "description": "媒体数据源返回的原始风格名称",
        "value_type": "string_list",
        "media_types": ALL_MEDIA_TYPES,
    },
    {
        "id": "media.adult",
        "label": "成人内容",
        "group": "影视",
        "value_type": "boolean",
        "media_types": VIDEO_MEDIA_TYPES,
    },
    {
        "id": "media.runtime",
        "label": "片长",
        "group": "影视",
        "value_type": "integer",
        "media_types": VIDEO_MEDIA_TYPES,
    },
    {
        "id": "media.content_rating",
        "label": "内容分级",
        "group": "影视",
        "value_type": "string",
        "media_types": VIDEO_MEDIA_TYPES,
    },
    {
        "id": "media.companies",
        "label": "出品公司",
        "group": "影视",
        "value_type": "string_list",
        "media_types": VIDEO_MEDIA_TYPES,
    },
    {
        "id": "media.networks",
        "label": "电视台或平台",
        "group": "影视",
        "value_type": "string_list",
        "media_types": ("电视剧",),
    },
    {
        "id": "music.entity_type",
        "label": "音乐实体类型",
        "group": "音乐",
        "value_type": "enum",
        "media_types": MUSIC_MEDIA_TYPES,
        "options": ("recording", "album", "artist"),
    },
    {
        "id": "music.album_type",
        "label": "专辑类型",
        "group": "音乐",
        "value_type": "string",
        "media_types": MUSIC_MEDIA_TYPES,
    },
    {
        "id": "music.secondary_types",
        "label": "专辑附加类型",
        "group": "音乐",
        "value_type": "string_list",
        "media_types": MUSIC_MEDIA_TYPES,
    },
    {
        "id": "music.genres",
        "label": "音乐流派",
        "group": "音乐",
        "value_type": "string_list",
        "media_types": MUSIC_MEDIA_TYPES,
    },
    {
        "id": "music.tags",
        "label": "音乐标签",
        "group": "音乐",
        "value_type": "string_list",
        "media_types": MUSIC_MEDIA_TYPES,
    },
    {
        "id": "music.artist_country",
        "label": "艺术家国家或地区",
        "group": "音乐",
        "value_type": "string",
        "media_types": MUSIC_MEDIA_TYPES,
    },
    {
        "id": "music.release_status",
        "label": "发行状态",
        "group": "音乐",
        "value_type": "string",
        "media_types": MUSIC_MEDIA_TYPES,
    },
)


def operators_for_value_type(value_type: str) -> tuple[str, ...]:
    """
    返回值类型允许使用的确定性操作符集合

    :param value_type: 字段值类型
    :return: 按 UI 展示顺序排列的操作符；未知类型返回空元组
    """
    return VALUE_TYPE_OPERATORS.get(value_type, ())


def get_standard_classification_fields() -> tuple[ClassificationFieldDefinition, ...]:
    """返回前端与校验器共享的标准分类字段目录。"""
    return standard_field_definitions()


def standard_field_definitions() -> tuple[ClassificationFieldDefinition, ...]:
    """构造包含内置来源覆盖信息的标准分类字段目录。"""
    definitions: list[ClassificationFieldDefinition] = []
    for spec in _STANDARD_FIELD_SPECS:
        definitions.append(_build_field_definition(spec))
    return tuple(definitions)


def merge_field_definitions(
    extra_fields: Iterable[ClassificationFieldDefinition] = (),
) -> tuple[ClassificationFieldDefinition, ...]:
    """
    合并标准字段和扩展字段，字段 ID 重复时保留标准字段或先出现的扩展字段

    重复字段的错误归属于策略校验器；目录合并保持稳定顺序，避免前端字段列表抖动。
    """
    merged: list[ClassificationFieldDefinition] = []
    seen_ids: set[str] = set()
    for definition in (*standard_field_definitions(), *tuple(extra_fields)):
        field_id = str(getattr(definition, "id", ""))
        if field_id in seen_ids:
            continue
        seen_ids.add(field_id)
        merged.append(definition)
    return tuple(merged)


def field_definition_map(
    extra_fields: Iterable[ClassificationFieldDefinition] = (),
) -> dict[str, ClassificationFieldDefinition]:
    """返回按字段 ID 索引的标准与扩展字段定义。"""
    return {str(getattr(definition, "id", "")): definition for definition in merge_field_definitions(extra_fields)}


def classification_fact_matches_definition(
    value: object,
    definition: ClassificationFieldDefinition,
) -> bool:
    """按字段值类型和封闭枚举目录校验一个有限 JSON 事实。"""
    if value is None:
        return True
    if definition.value_type == "string":
        valid = isinstance(value, str)
    elif definition.value_type == "enum":
        valid = _is_json_scalar(value)
    elif definition.value_type in {"integer", "year"}:
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif definition.value_type == "number":
        valid = (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and (not isinstance(value, float) or isfinite(value))
        )
    elif definition.value_type == "boolean":
        valid = isinstance(value, bool)
    else:
        valid = isinstance(value, list) and all(
            isinstance(item, str) for item in value
        )
    if not valid:
        return False
    if definition.allow_custom_values or not definition.options:
        return True
    allowed: set[ClassificationFactScalar] = {
        option.value for option in definition.options
    }
    if isinstance(value, list):
        return all(item in allowed for item in value)
    return cast(ClassificationFactScalar, value) in allowed


def _is_json_scalar(value: object) -> bool:
    """判断值是否为协议允许且可稳定序列化的有限 JSON 标量。"""
    if isinstance(value, float):
        return isfinite(value)
    return value is None or isinstance(value, (str, int, bool))


def _build_field_definition(values: _FieldSpec) -> ClassificationFieldDefinition:
    """将只读字段规格转换为严格 schema。"""
    return ClassificationFieldDefinition(
        id=values["id"],
        label=values["label"],
        group=values.get("group", "通用"),
        description=values.get("description"),
        value_type=values["value_type"],
        operators=[
            cast(ClassificationOperator, operator) for operator in operators_for_value_type(values["value_type"])
        ],
        media_types=[cast(ClassificationMediaType, media_type) for media_type in values["media_types"]],
        options=[
            ClassificationFieldOption(value=option, label=option)
            for option in values.get("options", ())
        ],
        allow_custom_values=values["value_type"] != "enum",
        source_support=builtin_field_source_support(values["id"]),
    )
