"""旧版分类迁移的值编译和条件树基础工具。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from app.domain.classification.vocabulary import classification_field_options
from app.schemas.category import (
    ClassificationCondition,
    ClassificationConditionGroup,
    ClassificationConditionNode,
    ClassificationFieldDefinition,
    ClassificationMediaType,
)

_TMDB_SOURCE: Final[str] = "themoviedb"
_EXTENSION_PREFIX: Final[str] = f"extensions.{_TMDB_SOURCE}."
_LEGACY_FIELD_PRESENTATION: Final[dict[str, tuple[str, str]]] = {
    "genre_ids": ("风格（旧规则）", "media.genre_keys"),
    "origin_country": ("原产国家/地区（旧规则）", "media.countries"),
}


@dataclass(frozen=True, slots=True)
class _LegacyToken:
    """保留旧字段展开后的同向值集合及其排除语义。"""

    negative: bool
    values: tuple[str, ...]


def _legacy_field_definition(
    field_id: str,
    media_types: list[ClassificationMediaType],
) -> ClassificationFieldDefinition:
    """构造不会出现在新规则选择器中的旧 TMDB 字段说明。"""
    field_name = field_id.removeprefix(_EXTENSION_PREFIX)
    presentation = _LEGACY_FIELD_PRESENTATION.get(field_name)
    label = presentation[0] if presentation else f"TMDB {field_name}"
    replacement_field = presentation[1] if presentation else None
    replacement_hint = f"；新增条件请使用{presentation[0].replace('（旧规则）', '')}" if presentation else ""
    return ClassificationFieldDefinition(
        id=field_id,
        label=label,
        group="旧规则",
        description=(f"从旧分类配置迁移，保留原有匹配方式{replacement_hint}"),
        value_type="string_list",
        operators=["contains_any", "contains_none", "exists", "not_exists"],
        media_types=media_types,
        source_support={_TMDB_SOURCE: "extension"},
        options=classification_field_options("media.countries") if field_name == "origin_country" else [],
        selectable=False,
        replacement_field=replacement_field,
    )


def _parse_legacy_tokens(value: str) -> tuple[tuple[_LegacyToken, ...], bool]:
    """沿用旧范围展开语义，并合并同向枚举，避免值数量膨胀为叶子数量。"""
    raw_tokens = [item for item in value.split(",") if item]
    values_by_sign: dict[bool, list[str]] = {}
    requires_exists = not raw_tokens
    for raw_token in raw_tokens:
        expanded = _expand_legacy_token(raw_token)
        if not expanded:
            requires_exists = True
            continue
        for expanded_value in expanded:
            negative = expanded_value.startswith("!")
            plain_value = expanded_value[1:] if negative else expanded_value
            values_by_sign.setdefault(negative, []).append(plain_value)
    return tuple(_LegacyToken(negative, tuple(values)) for negative, values in values_by_sign.items()), requires_exists


def _expand_legacy_token(value: str) -> tuple[str, ...]:
    """复现旧代码对数字闭区间和非数字连字符端点的展开。"""
    if "-" not in value:
        return (value,)
    value_begin, value_end = value.split("-", 1)
    prefix = ""
    if value_begin.startswith("!"):
        prefix = "!"
        value_begin = value_begin[1:]
    if value_begin.isdigit() and value_end.isdigit():
        return tuple(f"{prefix}{item}" for item in range(int(value_begin), int(value_end) + 1))
    return (f"{prefix}{value_begin}", f"{prefix}{value_end}")


def _legacy_list_condition(
    field_id: str,
    tokens: Sequence[_LegacyToken],
    requires_exists: bool,
) -> ClassificationConditionNode:
    """把旧列表成员条件编译为正项 OR、负项逐组排除的条件树。"""
    positives = [
        ClassificationCondition(
            field=field_id,
            operator="contains_any",
            value=list(token.values),
        )
        for token in tokens
        if not token.negative
    ]
    negatives = [
        ClassificationCondition(
            field=field_id,
            operator="contains_none",
            value=list(token.values),
        )
        for token in tokens
        if token.negative
    ]
    nodes: list[ClassificationConditionNode] = []
    if positives:
        nodes.append(_any_or_single(positives))
    nodes.extend(negatives)
    if not nodes and requires_exists:
        return ClassificationCondition(field=field_id, operator="exists")
    return _all_or_single(nodes)


def _all_or_single(
    nodes: Sequence[ClassificationConditionNode],
) -> ClassificationConditionNode:
    """合并相邻 all 组并避免为单节点额外增加条件树深度。"""
    flattened: list[ClassificationConditionNode] = []
    for node in nodes:
        if isinstance(node, ClassificationConditionGroup) and node.all is not None:
            flattened.extend(node.all)
        else:
            flattened.append(node)
    if len(flattened) == 1:
        return flattened[0]
    return ClassificationConditionGroup(all=flattened)


def _any_or_single(
    nodes: Sequence[ClassificationConditionNode],
) -> ClassificationConditionNode:
    """合并相邻 any 组并避免为单节点额外增加条件树深度。"""
    flattened: list[ClassificationConditionNode] = []
    for node in nodes:
        if isinstance(node, ClassificationConditionGroup) and node.any is not None:
            flattened.extend(node.any)
        else:
            flattened.append(node)
    if len(flattened) == 1:
        return flattened[0]
    return ClassificationConditionGroup(any=flattened)
