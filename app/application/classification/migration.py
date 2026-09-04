"""旧版 TMDB 分类配置到新版分类策略的纯迁移。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal, Optional, TypeAlias, Union, cast

from app.schemas.category import (
    CategoryConfig,
    CategoryRule,
    ClassificationCategory,
    ClassificationCondition,
    ClassificationConditionGroup,
    ClassificationConditionNode,
    ClassificationFieldDefinition,
    ClassificationMediaType,
    ClassificationPolicy,
    ClassificationRule,
    ClassificationRuleKind,
    ClassificationTarget,
)

LegacyMediaKey: TypeAlias = Literal["movie", "tv"]
"""旧分类配置支持的一级媒体类型键。"""

LegacyDiagnosticSeverity: TypeAlias = Literal["error", "warning"]
"""旧配置迁移诊断的严重级别。"""

LegacyDiagnosticPathPart: TypeAlias = Union[str, int]
"""旧配置迁移诊断路径允许的段类型。"""

_TMDB_SOURCE: Final[str] = "themoviedb"
_EXTENSION_PREFIX: Final[str] = f"extensions.{_TMDB_SOURCE}."
_ARCHIVED_CATEGORY_LABEL_PREFIX: Final[str] = "legacy-category:"
_SAFE_FIELD_SEGMENT = re.compile(r"^[a-z][a-z0-9_]*$")
_ILLEGAL_PATH_CHARACTERS: Final[frozenset[str]] = frozenset('<>:"/\\|?*')
_MEDIA_TYPES: Final[dict[LegacyMediaKey, ClassificationMediaType]] = {
    "movie": "电影",
    "tv": "电视剧",
}
_MEDIA_KEYS: Final[dict[ClassificationMediaType, LegacyMediaKey]] = {
    "电影": "movie",
    "电视剧": "tv",
}
_COMMON_FALLBACKS: Final[dict[ClassificationMediaType, str]] = {
    "电影": "movie.uncategorized",
    "电视剧": "tv.uncategorized",
    "音乐": "music.uncategorized",
}
_TMDB_GENRE_KEYS: Final[dict[str, str]] = {
    "12": "adventure",
    "14": "fantasy",
    "16": "animation",
    "18": "drama",
    "27": "horror",
    "28": "action",
    "35": "comedy",
    "36": "history",
    "37": "western",
    "53": "thriller",
    "80": "crime",
    "99": "documentary",
    "878": "science_fiction",
    "9648": "mystery",
    "10402": "music",
    "10749": "romance",
    "10751": "family",
    "10752": "war",
    "10762": "kids",
    "10764": "reality",
    "10767": "talk",
    "10770": "tv_movie",
}
_LEGACY_FIELD_PRESENTATION: Final[dict[str, tuple[str, str]]] = {
    "genre_ids": ("风格（旧规则）", "media.genre_keys"),
    "origin_country": ("原产国家/地区（旧规则）", "media.countries"),
}


@dataclass(frozen=True, slots=True)
class LegacyClassificationDiagnostic:
    """描述旧配置迁移或兼容投影中的结构化错误与警告。"""

    severity: LegacyDiagnosticSeverity
    code: str
    message: str
    path: tuple[LegacyDiagnosticPathPart, ...] = ()


@dataclass(frozen=True, slots=True)
class LegacyClassificationMigrationResult:
    """承载旧配置迁移后的策略、动态字段目录和全部诊断。"""

    policy: ClassificationPolicy
    extra_fields: tuple[ClassificationFieldDefinition, ...]
    issues: tuple[LegacyClassificationDiagnostic, ...]

    @property
    def valid(self) -> bool:
        """返回迁移结果是否不存在阻止自动发布的错误。"""
        return not any(item.severity == "error" for item in self.issues)

    @property
    def field_definitions(self) -> tuple[ClassificationFieldDefinition, ...]:
        """兼容返回迁移生成的动态字段声明。"""
        return self.extra_fields

    @property
    def diagnostics(self) -> tuple[LegacyClassificationDiagnostic, ...]:
        """兼容返回迁移生成的结构化诊断。"""
        return self.issues

    @property
    def publishable(self) -> bool:
        """兼容返回迁移结果是否允许自动发布。"""
        return self.valid


@dataclass(frozen=True, slots=True)
class _LegacyToken:
    """保留一个旧逗号项展开后的值集合及其排除语义。"""

    negative: bool
    values: tuple[str, ...]


@dataclass(slots=True)
class _MigrationContext:
    """在一次纯迁移中收集动态字段、值别名和结构化诊断。"""

    field_media_types: dict[str, list[ClassificationMediaType]] = field(default_factory=dict)
    field_aliases: dict[str, dict[str, str]] = field(default_factory=dict)
    diagnostics: list[LegacyClassificationDiagnostic] = field(default_factory=list)

    def add_diagnostic(
        self,
        severity: LegacyDiagnosticSeverity,
        code: str,
        message: str,
        path: Sequence[LegacyDiagnosticPathPart],
    ) -> None:
        """追加一条保持稳定路径的迁移诊断。"""
        self.diagnostics.append(
            LegacyClassificationDiagnostic(
                severity=severity,
                code=code,
                message=message,
                path=tuple(path),
            )
        )

    def register_extension_field(
        self,
        field_name: str,
        media_type: ClassificationMediaType,
    ) -> str:
        """登记一个受控 TMDB 旧比较字段并合并适用影视类型。"""
        field_id = f"{_EXTENSION_PREFIX}{field_name}"
        media_types = self.field_media_types.setdefault(field_id, [])
        if media_type not in media_types:
            media_types.append(media_type)
        return field_id

    def register_aliases(self, field_id: str, values: Iterable[str]) -> None:
        """把旧配置原始大小写映射到旧算法使用的大写比较值。"""
        aliases = self.field_aliases.setdefault(field_id, {})
        for value in values:
            normalized = value.upper()
            if value != normalized:
                aliases[value] = normalized

    def build_field_definitions(self) -> tuple[ClassificationFieldDefinition, ...]:
        """按首次出现顺序构造仅供已有规则解析的退役字段声明。"""
        return tuple(
            _legacy_field_definition(field_id, media_types) for field_id, media_types in self.field_media_types.items()
        )


def migrate_legacy_category_config(
    config: Union[CategoryConfig, Mapping[str, object]],
) -> LegacyClassificationMigrationResult:
    """
    把内存中的旧分类配置转换为按媒体类型匹配的新版策略草稿

    :param config: 已校验的 CategoryConfig 或保持 YAML 顺序的映射
    :return: 包含策略、动态字段声明和结构化诊断的纯迁移结果
    """
    context = _MigrationContext()
    root = _legacy_root_mapping(config)
    _diagnose_unknown_top_level_keys(root, context)
    categories: list[ClassificationCategory] = []
    rules: list[ClassificationRule] = []
    fallbacks = dict(_COMMON_FALLBACKS)

    for media_key, media_type in _MEDIA_TYPES.items():
        _migrate_media_categories(
            media_key=media_key,
            media_type=media_type,
            raw_categories=root.get(media_key),
            categories=categories,
            rules=rules,
            fallbacks=fallbacks,
            context=context,
        )

    categories.extend(_common_fallback_categories(categories))
    policy_payload: dict[str, object] = {
        "schema_version": 2,
        "revision": 1,
        "mode": "first_match",
        "categories": categories,
        "rules": rules,
        "fallbacks": fallbacks,
        "field_aliases": {field_id: aliases for field_id, aliases in context.field_aliases.items() if aliases},
    }
    policy = ClassificationPolicy.model_validate(policy_payload)
    return LegacyClassificationMigrationResult(
        policy=policy,
        extra_fields=context.build_field_definitions(),
        issues=tuple(context.diagnostics),
    )


def legacy_extension_fields_from_policy(
    policy: ClassificationPolicy,
) -> tuple[ClassificationFieldDefinition, ...]:
    """按策略条件顺序重建可直接注册的 TMDB 旧比较扩展字段声明。"""
    context = _MigrationContext()
    for rule in policy.rules:
        for field_id in _condition_field_ids(rule.when):
            if not field_id.startswith(_EXTENSION_PREFIX):
                continue
            field_name = field_id.removeprefix(_EXTENSION_PREFIX)
            if not _SAFE_FIELD_SEGMENT.fullmatch(field_name):
                continue
            for media_type in rule.media_types:
                context.register_extension_field(field_name, media_type)
    return context.build_field_definitions()


def _legacy_root_mapping(
    config: Union[CategoryConfig, Mapping[str, object]],
) -> Mapping[str, object]:
    """把强类型旧配置投影为不访问文件的有序根映射。"""
    if isinstance(config, CategoryConfig):
        return {
            "movie": config.movie or {},
            "tv": config.tv or {},
        }
    return config


def _diagnose_unknown_top_level_keys(
    root: Mapping[str, object],
    context: _MigrationContext,
) -> None:
    """拒绝旧契约未定义的一级媒体类型，避免静默丢失配置。"""
    for key in root:
        if key in _MEDIA_TYPES:
            continue
        context.add_diagnostic(
            "error",
            "unsupported_legacy_media_type",
            f"旧分类配置一级键 {key!s} 不受支持",
            [str(key)],
        )


def _migrate_media_categories(
    *,
    media_key: LegacyMediaKey,
    media_type: ClassificationMediaType,
    raw_categories: object,
    categories: list[ClassificationCategory],
    rules: list[ClassificationRule],
    fallbacks: dict[ClassificationMediaType, str],
    context: _MigrationContext,
) -> None:
    """按单个旧媒体类型的原始顺序迁移分类、规则和全局兜底。"""
    if raw_categories is None:
        return
    if not isinstance(raw_categories, Mapping):
        context.add_diagnostic(
            "error",
            "invalid_legacy_category_map",
            f"旧分类配置 {media_key} 必须是分类名称映射",
            [media_key],
        )
        return

    fallback_seen = False
    for category_index, (raw_name, raw_rule) in enumerate(raw_categories.items()):
        name = str(raw_name)
        base_path: list[LegacyDiagnosticPathPart] = [media_key, name]
        category_id = _stable_category_id(media_key, name)
        unreachable = fallback_seen
        _diagnose_category_name(raw_name, name, base_path, context)
        categories.append(
            ClassificationCategory(
                id=category_id,
                media_type=media_type,
                name=name,
                path=_legacy_category_path(name),
                enabled=not unreachable,
            )
        )

        if unreachable:
            context.add_diagnostic(
                "warning",
                "unreachable_legacy_category",
                f"分类 {name} 位于首个空兜底之后，按旧顺序永远不可达，已保留并禁用",
                base_path,
            )

        rule_mapping = _legacy_rule_mapping(raw_rule)
        if _is_legacy_fallback(raw_rule, rule_mapping):
            if not fallback_seen:
                fallbacks[media_type] = category_id
                fallback_seen = True
            if rule_mapping is not None:
                rules.append(
                    _fallback_metadata_rule(
                        category_id=category_id,
                        category_name=name,
                        media_type=media_type,
                        priority=len(rules),
                        rule_mapping=rule_mapping,
                        path=base_path,
                        archived=unreachable,
                        context=context,
                    )
                )
            elif unreachable:
                rules.append(
                    _disabled_placeholder_rule(
                        category_id=category_id,
                        category_name=name,
                        media_type=media_type,
                        priority=len(rules),
                        archived=True,
                    )
                )
            continue

        if rule_mapping is None:
            context.add_diagnostic(
                "error",
                "invalid_legacy_category_rule",
                f"分类 {name} 的规则必须是字段映射或空值",
                base_path,
            )
            rules.append(
                _disabled_placeholder_rule(
                    category_id=category_id,
                    category_name=name,
                    media_type=media_type,
                    priority=len(rules),
                    archived=unreachable,
                )
            )
            continue

        nodes: list[ClassificationConditionNode] = []
        category_has_error = False
        for raw_field, raw_value in rule_mapping.items():
            if not raw_value:
                continue
            field_path = [*base_path, str(raw_field)]
            before_errors = _error_count(context.diagnostics)
            node = _migrate_legacy_field(
                raw_field=raw_field,
                raw_value=raw_value,
                media_type=media_type,
                path=field_path,
                context=context,
            )
            category_has_error = category_has_error or _error_count(context.diagnostics) > before_errors
            if node is not None:
                nodes.append(node)

        if not nodes:
            nodes.append(_tmdb_identity_condition())
            category_has_error = True
        rule_kind, target = _retained_rule_output(category_id, unreachable)
        rules.append(
            ClassificationRule(
                id=f"{category_id}.rule",
                name=name,
                kind=rule_kind,
                enabled=not unreachable and not category_has_error,
                priority=category_index,
                media_types=[media_type],
                sources=[_TMDB_SOURCE],
                when=_all_or_single(nodes),
                target=target,
            )
        )


def _legacy_rule_mapping(raw_rule: object) -> Optional[Mapping[object, object]]:
    """把 CategoryRule 或原始映射统一为只读字段映射。"""
    if isinstance(raw_rule, CategoryRule):
        return cast(
            Mapping[object, object],
            raw_rule.model_dump(exclude_none=False, exclude_unset=True),
        )
    if isinstance(raw_rule, Mapping):
        return raw_rule
    return None


def _is_legacy_fallback(
    raw_rule: object,
    rule_mapping: Optional[Mapping[object, object]],
) -> bool:
    """复现旧实现对空项及所有字段均为空映射的立即命中语义。"""
    if not raw_rule:
        return True
    return rule_mapping is not None and all(not value for value in rule_mapping.values())


def _diagnose_category_name(
    raw_name: object,
    name: str,
    path: Sequence[LegacyDiagnosticPathPart],
    context: _MigrationContext,
) -> None:
    """在仍保留分类的同时标记无法安全投影为目录路径的名称。"""
    invalid = (
        not isinstance(raw_name, str)
        or not name
        or name != name.strip()
        or any(_legacy_path_segment_is_invalid(segment) for segment in _legacy_category_path(name))
    )
    if invalid:
        context.add_diagnostic(
            "error",
            "invalid_legacy_category_name",
            f"分类名称 {name!r} 不能安全投影为目录路径",
            path,
        )


def _legacy_category_path(name: str) -> list[str]:
    """把旧分类名中的斜杠还原为目录层级，同时保留原始显示名称。"""
    return name.split("/")


def _legacy_path_segment_is_invalid(segment: str) -> bool:
    """判断旧分类名拆出的目录段是否违反跨平台路径安全约束。"""
    illegal_characters = _ILLEGAL_PATH_CHARACTERS - frozenset({"/"})
    return (
        not segment
        or segment in {".", ".."}
        or segment != segment.strip()
        or segment.endswith((".", " "))
        or any(character in illegal_characters or ord(character) < 32 for character in segment)
    )


def _migrate_legacy_field(
    *,
    raw_field: object,
    raw_value: object,
    media_type: ClassificationMediaType,
    path: Sequence[LegacyDiagnosticPathPart],
    context: _MigrationContext,
) -> Optional[ClassificationConditionNode]:
    """把一个旧字段编译为保持正值 OR、排除值 AND 的条件树。"""
    if not isinstance(raw_field, str) or not _SAFE_FIELD_SEGMENT.fullmatch(raw_field):
        context.add_diagnostic(
            "error",
            "invalid_legacy_field",
            f"旧字段 {raw_field!s} 不是安全的小写 TMDB 一级字段段",
            path,
        )
        return None
    if not isinstance(raw_value, str):
        context.add_diagnostic(
            "error",
            "unsupported_legacy_value",
            f"旧字段 {raw_field} 的值必须是逗号分隔字符串",
            path,
        )
        return None

    tokens, requires_exists = _parse_legacy_tokens(raw_value)
    if raw_field == "genre_ids":
        return _migrate_genre_tokens(
            tokens=tokens,
            requires_exists=requires_exists,
            media_type=media_type,
            context=context,
        )

    field_id = context.register_extension_field(raw_field, media_type)
    context.register_aliases(
        field_id,
        (value for token in tokens for value in token.values),
    )
    return _legacy_list_condition(field_id, tokens, requires_exists)


def _legacy_field_definition(
    field_id: str,
    media_types: list[ClassificationMediaType],
) -> ClassificationFieldDefinition:
    """构造不会出现在新规则选择器中的旧 TMDB 字段说明。"""
    field_name = field_id.removeprefix(_EXTENSION_PREFIX)
    presentation = _LEGACY_FIELD_PRESENTATION.get(field_name)
    label = presentation[0] if presentation else f"TMDB {field_name}"
    replacement_field = presentation[1] if presentation else None
    replacement_hint = f"；新规则请使用 {replacement_field}" if replacement_field else ""
    return ClassificationFieldDefinition(
        id=field_id,
        label=label,
        group="旧规则",
        description=(f"仅用于保持已迁移 category.yaml 的原始比较语义{replacement_hint}"),
        value_type="string_list",
        operators=["contains_any", "contains_none", "exists", "not_exists"],
        media_types=media_types,
        source_support={_TMDB_SOURCE: "extension"},
        selectable=False,
        replacement_field=replacement_field,
    )


def _parse_legacy_tokens(value: str) -> tuple[tuple[_LegacyToken, ...], bool]:
    """逐项复现旧逗号、排除前缀和连字符范围展开算法。"""
    raw_tokens = [item for item in value.split(",") if item]
    parsed: list[_LegacyToken] = []
    requires_exists = not raw_tokens
    for raw_token in raw_tokens:
        expanded = _expand_legacy_token(raw_token)
        if not expanded:
            requires_exists = True
            continue
        grouped: list[_LegacyToken] = []
        for expanded_value in expanded:
            negative = expanded_value.startswith("!")
            plain_value = expanded_value[1:] if negative else expanded_value
            if grouped and grouped[-1].negative == negative:
                previous = grouped[-1]
                grouped[-1] = _LegacyToken(negative, (*previous.values, plain_value))
            else:
                grouped.append(_LegacyToken(negative, (plain_value,)))
        parsed.extend(grouped)
    return tuple(parsed), requires_exists


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


def _migrate_genre_tokens(
    *,
    tokens: Sequence[_LegacyToken],
    requires_exists: bool,
    media_type: ClassificationMediaType,
    context: _MigrationContext,
) -> ClassificationConditionNode:
    """已知正向 Genre ID 使用规范风格，其余条件保留原始视图。"""
    positive_nodes: list[ClassificationConditionNode] = []
    negative_nodes: list[ClassificationConditionNode] = []
    extension_field_id: Optional[str] = None
    for token in tokens:
        if token.negative:
            if extension_field_id is None:
                extension_field_id = context.register_extension_field("genre_ids", media_type)
            context.register_aliases(extension_field_id, token.values)
            negative_nodes.append(
                ClassificationCondition(
                    field=extension_field_id,
                    operator="contains_none",
                    value=list(token.values),
                )
            )
            continue
        known_keys: list[str] = []
        unknown_ids: list[str] = []
        for value in token.values:
            if genre_key := _TMDB_GENRE_KEYS.get(value.upper()):
                _append_unique(known_keys, genre_key)
            else:
                _append_unique(unknown_ids, value)
        token_nodes: list[ClassificationConditionNode] = []
        if known_keys:
            token_nodes.append(
                ClassificationCondition(
                    field="media.genre_keys",
                    operator="contains_any",
                    value=known_keys,
                )
            )
        if unknown_ids:
            if extension_field_id is None:
                extension_field_id = context.register_extension_field("genre_ids", media_type)
            context.register_aliases(extension_field_id, unknown_ids)
            token_nodes.append(
                ClassificationCondition(
                    field=extension_field_id,
                    operator="contains_any",
                    value=unknown_ids,
                )
            )
        if token_nodes:
            positive_nodes.append(_any_or_single(token_nodes))

    nodes: list[ClassificationConditionNode] = []
    if positive_nodes:
        nodes.append(_any_or_single(positive_nodes))
    nodes.extend(negative_nodes)
    if not nodes and requires_exists:
        extension_field_id = context.register_extension_field("genre_ids", media_type)
        return ClassificationCondition(field=extension_field_id, operator="exists")
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


def _tmdb_identity_condition() -> ClassificationCondition:
    """为不可迁移但需保留的禁用规则构造稳定占位条件。"""
    return ClassificationCondition(
        field="identity.media_source",
        operator="equals",
        value=_TMDB_SOURCE,
    )


def _disabled_placeholder_rule(
    *,
    category_id: str,
    category_name: str,
    media_type: ClassificationMediaType,
    priority: int,
    archived: bool,
) -> ClassificationRule:
    """保留不可达或非法旧分类的稳定规则身份，同时禁止执行。"""
    rule_kind, target = _retained_rule_output(category_id, archived)
    return ClassificationRule(
        id=f"{category_id}.rule",
        name=category_name,
        kind=rule_kind,
        enabled=False,
        priority=priority,
        media_types=[media_type],
        sources=[_TMDB_SOURCE],
        when=_tmdb_identity_condition(),
        target=target,
    )


def _fallback_metadata_rule(
    *,
    category_id: str,
    category_name: str,
    media_type: ClassificationMediaType,
    priority: int,
    rule_mapping: Mapping[object, object],
    path: Sequence[LegacyDiagnosticPathPart],
    archived: bool,
    context: _MigrationContext,
) -> ClassificationRule:
    """用禁用规则保留全空字段映射，运行时由全局兜底处理。"""
    nodes: list[ClassificationConditionNode] = []
    for raw_field in rule_mapping:
        field_path = [*path, str(raw_field)]
        if not isinstance(raw_field, str) or not _SAFE_FIELD_SEGMENT.fullmatch(raw_field):
            context.add_diagnostic(
                "error",
                "invalid_legacy_field",
                f"旧字段 {raw_field!s} 不是安全的小写 TMDB 一级字段段",
                field_path,
            )
            continue
        field_id = context.register_extension_field(raw_field, media_type)
        nodes.append(ClassificationCondition(field=field_id, operator="not_exists"))
    rule_kind, target = _retained_rule_output(category_id, archived)
    return ClassificationRule(
        id=f"{category_id}.fallback",
        name=category_name,
        kind=rule_kind,
        enabled=False,
        priority=priority,
        media_types=[media_type],
        sources=[_TMDB_SOURCE],
        when=_all_or_single(nodes) if nodes else _tmdb_identity_condition(),
        target=target,
    )


def _retained_rule_output(
    category_id: str,
    archived: bool,
) -> tuple[ClassificationRuleKind, ClassificationTarget]:
    """为不可达规则生成不引用 disabled 分类的稳定归档输出。"""
    if archived:
        return (
            "label",
            ClassificationTarget(labels=[f"{_ARCHIVED_CATEGORY_LABEL_PREFIX}{category_id}"]),
        )
    return "category", ClassificationTarget(category_id=category_id)


def _stable_category_id(media_key: LegacyMediaKey, name: str) -> str:
    """以媒体类型和原始名称生成不依赖本地字符集的稳定 ASCII ID。"""
    digest = hashlib.sha256(f"{media_key}\0{name}".encode("utf-8")).hexdigest()[:16]
    return f"legacy.{media_key}.{digest}"


def _common_fallback_categories(
    legacy_categories: Sequence[ClassificationCategory],
) -> list[ClassificationCategory]:
    """构造不受来源限制且不与同类型旧目录冲突的稳定未分类目录。"""
    occupied = {(category.media_type, tuple(category.path)) for category in legacy_categories}
    categories: list[ClassificationCategory] = []
    for media_type, category_id in _COMMON_FALLBACKS.items():
        path = ["未分类"]
        if (media_type, tuple(path)) in occupied:
            path.append("通用")
        categories.append(
            ClassificationCategory(
                id=category_id,
                media_type=media_type,
                name="未分类",
                path=path,
            )
        )
    return categories


def _error_count(diagnostics: Sequence[LegacyClassificationDiagnostic]) -> int:
    """返回当前迁移诊断中的错误数量。"""
    return sum(item.severity == "error" for item in diagnostics)


def _condition_field_ids(node: ClassificationConditionNode) -> list[str]:
    """按条件树顺序提取全部叶子字段 ID。"""
    if isinstance(node, ClassificationCondition):
        return [node.field]
    if node.all is not None:
        children = node.all
    elif node.any is not None:
        children = node.any
    elif node.not_ is not None:
        children = [node.not_]
    else:
        children = []
    return [field_id for child in children for field_id in _condition_field_ids(child)]


def _append_unique(values: list[str], value: str) -> None:
    """按首次出现顺序追加非重复字符串。"""
    if value not in values:
        values.append(value)
