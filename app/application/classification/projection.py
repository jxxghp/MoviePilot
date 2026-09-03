"""新版分类策略到旧 CategoryConfig 的只读兼容投影。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Optional

from app.application.classification.migration import (
    _ARCHIVED_CATEGORY_LABEL_PREFIX,
    _EXTENSION_PREFIX,
    _MEDIA_KEYS,
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
    ClassificationMediaType,
    ClassificationPolicy,
    ClassificationRule,
)

_TMDB_GENRE_IDS: Final[dict[str, str]] = {
    value: key for key, value in _TMDB_GENRE_KEYS.items()
}


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

    本迁移器生成的分类、规则和来源兜底可精确恢复；新版独有结构会保留可表达部分并返回警告。

    :param policy: 待兼容投影的新版策略
    :return: 旧配置和无法精确表达的结构化诊断
    """
    diagnostics: list[LegacyClassificationDiagnostic] = []
    source_fallbacks = _policy_source_fallbacks(policy)
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
        if source_fallbacks.get(_TMDB_SOURCE, {}).get(category.media_type) == category.id:
            rule = rules_by_category.get(category.id)
            if rule is not None and rule.id.endswith(".fallback"):
                projected[media_key][category.name] = CategoryRule.model_validate(
                    _project_fallback_metadata(rule)
                )
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


def _policy_source_fallbacks(
    policy: ClassificationPolicy,
) -> Mapping[str, Mapping[ClassificationMediaType, str]]:
    """读取新版来源专属兜底映射，并兼容并行 schema 合入前的空状态。"""
    value = getattr(policy, "source_fallbacks", {})
    return value if isinstance(value, Mapping) else {}


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
