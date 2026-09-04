"""新旧分类策略的只读兼容投影。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Optional, TypeAlias

from app.application.classification.migration import (
    _ARCHIVED_CATEGORY_LABEL_PREFIX,
    _EXTENSION_PREFIX,
    _MEDIA_KEYS,
    _SAFE_FIELD_SEGMENT,
    _TMDB_GENRE_KEYS,
    _TMDB_SOURCE,
    LegacyClassificationDiagnostic,
    LegacyDiagnosticPathPart,
    LegacyMediaKey,
)
from app.schemas.category import (
    CategoryConfig,
    CategoryRule,
    ClassificationCondition,
    ClassificationConditionNode,
    ClassificationFactScalar,
    ClassificationFactValue,
    ClassificationFieldDefinition,
    ClassificationPolicy,
    ClassificationRule,
)

_TMDB_GENRE_IDS: Final[dict[str, str]] = {value: key for key, value in _TMDB_GENRE_KEYS.items()}

LegacyPolicyOrFields: TypeAlias = ClassificationPolicy | Iterable[ClassificationFieldDefinition]
"""受控 TMDB 扩展事实可以从策略或字段声明中发现。"""


@dataclass(frozen=True, slots=True)
class LegacyCategoryProjectionResult:
    """承载新版策略到旧 CategoryConfig 的兼容投影和损失诊断。"""

    config: CategoryConfig
    diagnostics: tuple[LegacyClassificationDiagnostic, ...]

    @property
    def exact(self) -> bool:
        """返回投影是否未发现无法由旧配置表达的策略结构。"""
        return not self.diagnostics


def project_policy_to_legacy_category_projection(
    policy: ClassificationPolicy,
) -> LegacyCategoryProjectionResult:
    """
    把新版策略尽可能投影为旧 CategoryConfig

    本迁移器生成的分类、规则和全局兜底可精确恢复；新版独有结构会保留可表达部分并返回警告。

    :param policy: 待兼容投影的新版策略
    :return: 旧配置和无法精确表达的结构化诊断
    """
    diagnostics: list[LegacyClassificationDiagnostic] = []
    rules_by_category = _category_rules(policy)
    projected: dict[LegacyMediaKey, dict[str, Optional[CategoryRule]]] = {
        "movie": {},
        "tv": {},
    }

    for category_index, category in enumerate(policy.categories):
        media_key = _MEDIA_KEYS.get(category.media_type)
        if media_key is None or not category.id.startswith(f"legacy.{media_key}."):
            continue
        category_path: list[LegacyDiagnosticPathPart] = ["categories", category_index]
        if policy.fallbacks.get(category.media_type) == category.id:
            rule = rules_by_category.get(category.id)
            if rule is not None and rule.id.endswith(".fallback"):
                projected[media_key][category.name] = CategoryRule.model_validate(_project_fallback_metadata(rule))
            else:
                projected[media_key][category.name] = None
            continue
        rule = rules_by_category.get(category.id)
        if rule is None:
            diagnostics.append(
                _projection_warning(
                    "missing_legacy_category_rule",
                    f"分类 {category.name} 没有可投影的 TMDB 分类规则",
                    category_path,
                )
            )
            projected[media_key][category.name] = None
            continue
        if _is_disabled_placeholder_rule(rule):
            projected[media_key][category.name] = None
            continue
        raw_rule, rule_diagnostics = _project_rule(rule, policy)
        diagnostics.extend(rule_diagnostics)
        projected[media_key][category.name] = CategoryRule.model_validate(raw_rule)

    return LegacyCategoryProjectionResult(
        config=CategoryConfig(movie=projected["movie"], tv=projected["tv"]),
        diagnostics=tuple(diagnostics),
    )


def project_policy_to_legacy_category_config(
    policy: ClassificationPolicy,
) -> CategoryConfig:
    """把新版策略按旧 API 契约直接投影为 CategoryConfig。"""
    return project_policy_to_legacy_category_projection(policy).config


def project_classification_policy_to_legacy_config(
    policy: ClassificationPolicy,
) -> CategoryConfig:
    """兼容调用指定的新版策略到旧 CategoryConfig 投影入口。"""
    return project_policy_to_legacy_category_config(policy)


def build_legacy_tmdb_extension_facts(
    policy_or_field_defs: LegacyPolicyOrFields,
    tmdb_info: Mapping[str, object],
) -> dict[str, dict[str, ClassificationFactValue]]:
    """
    为策略实际使用的受控字段构造 TMDB 旧比较字符串列表

    假值保持缺失，列表与标量严格沿用 CategoryHelper 的字符串化和大写规则。

    :param policy_or_field_defs: 分类策略或迁移器生成的动态字段声明
    :param tmdb_info: 当前 TMDB 详情映射
    :return: 可直接传给分类事实构造器的 extensions 映射
    """
    values: dict[str, ClassificationFactValue] = {}
    for field_name in _controlled_tmdb_fields(policy_or_field_defs):
        projected = _project_legacy_tmdb_field(field_name, tmdb_info)
        if projected is not None:
            values[field_name] = projected
    return {_TMDB_SOURCE: values} if values else {}


def resolve_legacy_tmdb_category(
    config: CategoryConfig,
    *,
    media_type: str,
    media_source: str,
    tmdb_info: object,
) -> str:
    """策略不可用时按旧 CategoryHelper 语义解析 TMDB 目录分类。"""
    if media_source != _TMDB_SOURCE or not isinstance(tmdb_info, Mapping):
        return ""
    if media_type == "电影":
        categories = config.movie or {}
    elif media_type == "电视剧":
        categories = config.tv or {}
    else:
        return ""
    return _resolve_legacy_category_mapping(categories, tmdb_info)


def _category_rules(policy: ClassificationPolicy) -> dict[str, ClassificationRule]:
    """按目标分类 ID 索引 TMDB 主分类规则，保持首条规则优先。"""
    result: dict[str, ClassificationRule] = {}
    for rule in policy.rules:
        category_id = rule.target.category_id or _archived_category_id(rule)
        if category_id and _TMDB_SOURCE in rule.sources:
            result.setdefault(category_id, rule)
    return result


def _archived_category_id(rule: ClassificationRule) -> Optional[str]:
    """从 disabled 归档标签规则中恢复原目标分类 ID。"""
    if rule.enabled or rule.kind != "label":
        return None
    for raw_label in rule.target.labels:
        label = str(raw_label)
        if label.startswith(_ARCHIVED_CATEGORY_LABEL_PREFIX):
            return label.removeprefix(_ARCHIVED_CATEGORY_LABEL_PREFIX)
    return None


def _is_disabled_placeholder_rule(rule: ClassificationRule) -> bool:
    """识别迁移器为后续空兜底或非法规则保留的禁用占位规则。"""
    condition = rule.when
    return (
        not rule.enabled
        and isinstance(condition, ClassificationCondition)
        and condition.field == "identity.media_source"
        and condition.operator == "equals"
        and condition.value == _TMDB_SOURCE
    )


def _project_fallback_metadata(rule: ClassificationRule) -> dict[str, None]:
    """从禁用兜底元数据规则恢复显式全空字段映射。"""
    fields: dict[str, None] = {}
    for condition in _condition_leaves(rule.when):
        if condition.field.startswith(_EXTENSION_PREFIX) and condition.operator == "not_exists":
            fields[condition.field.removeprefix(_EXTENSION_PREFIX)] = None
    return fields


def _project_rule(
    rule: ClassificationRule,
    policy: ClassificationPolicy,
) -> tuple[dict[str, str], list[LegacyClassificationDiagnostic]]:
    """把迁移器条件树恢复为旧字段字符串，并标记无法精确表达的节点。"""
    diagnostics: list[LegacyClassificationDiagnostic] = []
    tokens_by_field: dict[str, list[str]] = {}
    for condition in _condition_leaves(rule.when):
        field_name, values, negative = _project_condition(
            condition,
            policy,
            diagnostics,
            rule.id,
        )
        if field_name is None:
            continue
        if condition.operator == "exists":
            tokens_by_field.setdefault(field_name, []).append(",")
            continue
        if not values:
            continue
        token = _render_legacy_token(values)
        rendered = f"!{token}" if negative else token
        field_tokens = tokens_by_field.setdefault(field_name, [])
        if rendered not in field_tokens:
            field_tokens.append(rendered)
    return (
        {field_name: ",".join(tokens) for field_name, tokens in tokens_by_field.items()},
        diagnostics,
    )


def _condition_leaves(node: ClassificationConditionNode) -> list[ClassificationCondition]:
    """按策略顺序展开条件树叶子，供旧兼容投影使用。"""
    if isinstance(node, ClassificationCondition):
        return [node]
    if node.all is not None:
        children = node.all
    elif node.any is not None:
        children = node.any
    elif node.not_ is not None:
        children = [node.not_]
    else:
        children = []
    return [leaf for child in children for leaf in _condition_leaves(child)]


def _project_condition(
    condition: ClassificationCondition,
    policy: ClassificationPolicy,
    diagnostics: list[LegacyClassificationDiagnostic],
    rule_id: str,
) -> tuple[Optional[str], list[str], bool]:
    """把一个迁移器叶子恢复为旧字段、值集合和排除标志。"""
    if condition.operator not in {"contains_any", "contains_none", "exists"}:
        diagnostics.append(
            _projection_warning(
                "unsupported_policy_condition",
                f"规则 {rule_id} 的操作符 {condition.operator} 无法精确投影到旧配置",
                ["rules", rule_id, "when"],
            )
        )
        return None, [], False
    values = [str(value) for value in condition.value] if isinstance(condition.value, list) else []
    if condition.field == "media.genre_keys":
        projected_values: list[str] = []
        for value in values:
            genre_id = _TMDB_GENRE_IDS.get(value)
            if genre_id is None:
                diagnostics.append(
                    _projection_warning(
                        "unknown_standard_genre_key",
                        f"规范类型键 {value} 没有可用的 TMDB 旧 genre ID",
                        ["rules", rule_id, "when"],
                    )
                )
                continue
            projected_values.append(genre_id)
        return "genre_ids", projected_values, condition.operator == "contains_none"
    if condition.field.startswith(_EXTENSION_PREFIX):
        field_name = condition.field.removeprefix(_EXTENSION_PREFIX)
        aliases = policy.field_aliases.get(condition.field, {})
        original_values = [_original_alias_value(value, aliases) for value in values]
        return field_name, original_values, condition.operator == "contains_none"
    diagnostics.append(
        _projection_warning(
            "unsupported_policy_field",
            f"规则 {rule_id} 的字段 {condition.field} 无法投影到旧 TMDB 配置",
            ["rules", rule_id, "when"],
        )
    )
    return None, [], False


def _original_alias_value(value: str, aliases: Mapping[str, str]) -> str:
    """优先恢复迁移条件中保留的原始大小写值。"""
    for alias, canonical in aliases.items():
        if canonical == value.upper():
            return alias
    return value


def _render_legacy_token(values: Sequence[str]) -> str:
    """把一个迁移时保留边界的值集合恢复为单个旧逗号项。"""
    if len(values) == 1:
        return values[0]
    if values and all(value.isdigit() for value in values):
        numbers = [int(value) for value in values]
        if numbers == list(range(numbers[0], numbers[-1] + 1)):
            return f"{numbers[0]}-{numbers[-1]}"
    return "-".join(values)


def _projection_warning(
    code: str,
    message: str,
    path: Sequence[LegacyDiagnosticPathPart],
) -> LegacyClassificationDiagnostic:
    """构造一条兼容投影警告。"""
    return LegacyClassificationDiagnostic(
        severity="warning",
        code=code,
        message=message,
        path=tuple(path),
    )


def _resolve_legacy_category_mapping(
    categories: Mapping[str, Optional[CategoryRule]],
    tmdb_info: Mapping[str, object],
) -> str:
    """复现旧分类的顺序、假值、范围和排除条件语义。"""
    if not tmdb_info or not categories:
        return ""
    for name, rule in categories.items():
        if rule is None:
            return name
        matched = True
        for field_name, expected in rule.model_dump(exclude_none=True).items():
            if not expected:
                continue
            actual = (
                tmdb_info.get("release_date") or tmdb_info.get("first_air_date")
                if field_name == "release_year"
                else tmdb_info.get(field_name)
            )
            if field_name == "release_year" and actual:
                actual = str(actual)[:4]
            if not actual:
                matched = False
                continue
            actual_values = _legacy_actual_values(field_name, actual)
            positive, negative = _legacy_expected_values(str(expected))
            if positive and not set(positive).intersection(actual_values):
                matched = False
            if negative and set(negative).intersection(actual_values):
                matched = False
        if matched:
            return name
    return ""


def _legacy_actual_values(field_name: str, value: object) -> list[str]:
    """按旧实现把 TMDB 标量、列表和制作国家转换为大写字符串。"""
    if field_name == "production_countries" and isinstance(value, list):
        return [str(item.get("iso_3166_1")).upper() for item in value if isinstance(item, Mapping)]
    if isinstance(value, list):
        return [str(item).upper() for item in value]
    return [str(value).upper()]


def _legacy_expected_values(value: str) -> tuple[list[str], list[str]]:
    """按旧实现展开逗号项、数字范围及排除项。"""
    expanded: list[str] = []
    for token in (item for item in value.split(",") if item):
        if "-" not in token:
            expanded.append(token)
            continue
        begin, end = token.split("-", 1)
        prefix = ""
        if begin.startswith("!"):
            prefix = "!"
            begin = begin[1:]
        if begin.isdigit() and end.isdigit():
            expanded.extend(f"{prefix}{item}" for item in range(int(begin), int(end) + 1))
        else:
            expanded.extend((f"{prefix}{begin}", f"{prefix}{end}"))
    values = [item.upper() for item in expanded]
    return (
        [item for item in values if not item.startswith("!")],
        [item[1:] for item in values if item.startswith("!")],
    )


def _controlled_tmdb_fields(
    policy_or_field_defs: LegacyPolicyOrFields,
) -> tuple[str, ...]:
    """从策略条件或动态字段声明中提取安全且去重的 TMDB 一级字段。"""
    field_ids: list[str] = []
    if isinstance(policy_or_field_defs, ClassificationPolicy):
        for rule in policy_or_field_defs.rules:
            field_ids.extend(_condition_field_ids(rule.when))
    else:
        for definition in policy_or_field_defs:
            if definition.source_support.get(_TMDB_SOURCE) == "extension":
                field_ids.append(definition.id)

    fields: list[str] = []
    for field_id in field_ids:
        if not field_id.startswith(_EXTENSION_PREFIX):
            continue
        field_name = field_id.removeprefix(_EXTENSION_PREFIX)
        if _SAFE_FIELD_SEGMENT.fullmatch(field_name):
            _append_unique(fields, field_name)
    return tuple(fields)


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


def _project_legacy_tmdb_field(
    field_name: str,
    tmdb_info: Mapping[str, object],
) -> Optional[list[ClassificationFactScalar]]:
    """把一个受控 TMDB 字段投影为旧算法实际比较的大写字符串列表。"""
    if field_name == "release_year":
        info_value = tmdb_info.get("release_date") or tmdb_info.get("first_air_date")
        if not info_value:
            return None
        return [str(info_value)[:4].upper()]

    info_value = tmdb_info.get(field_name)
    if not info_value:
        return None
    if field_name == "production_countries":
        if not isinstance(info_value, list):
            return None
        return [str(item.get("iso_3166_1")).upper() for item in info_value if isinstance(item, Mapping)]
    if isinstance(info_value, list):
        return [str(item).upper() for item in info_value]
    return [str(info_value).upper()]


def _append_unique(values: list[str], value: str) -> None:
    """按首次出现顺序追加非重复字符串。"""
    if value not in values:
        values.append(value)
