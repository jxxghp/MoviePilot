import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, cast

from app.domain.classification.fields import (
    ALL_MEDIA_TYPES,
    VALUE_TYPE_OPERATORS,
    standard_field_definitions,
)
from app.schemas.category import (
    ClassificationCondition,
    ClassificationConditionGroup,
    ClassificationConditionNode,
    ClassificationFactScalar,
    ClassificationFieldDefinition,
    ClassificationMediaType,
    ClassificationPolicy,
    ClassificationRule,
    ClassificationValidationIssue,
    ClassificationValidationResult,
)

MAX_CATEGORY_DEPTH: Final[int] = 4
MAX_CATEGORY_SEGMENT_LENGTH: Final[int] = 64
MAX_CATEGORY_PATH_LENGTH: Final[int] = 240
MAX_CONDITION_DEPTH: Final[int] = 3
MAX_CONDITIONS_PER_RULE: Final[int] = 30
MAX_RULES: Final[int] = 1000
MAX_TOTAL_CONDITIONS: Final[int] = MAX_RULES * MAX_CONDITIONS_PER_RULE

_STABLE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SOURCE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_ILLEGAL_PATH_CHARACTERS = frozenset('<>:"/\\|?*')


@dataclass(frozen=True, slots=True)
class _CategoryPathViolation:
    """描述分类路径中的一个结构化违规位置和消息。"""

    message: str
    segment_index: int | None = None


def validate_classification_category_path(
    segments: Sequence[str],
) -> tuple[str, ...]:
    """校验并冻结相对于媒体类型根目录的分类路径段。"""
    violations = _category_path_violations(segments)
    if violations:
        raise ValueError(violations[0].message)
    return tuple(segments)


@dataclass
class _ValidationCollector:
    issues: list[ClassificationValidationIssue] = field(default_factory=list)

    def error(self, code: str, message: str, path: list[str | int]) -> None:
        """追加阻止策略发布的结构化错误。"""
        self.issues.append(
            ClassificationValidationIssue(
                severity="error",
                code=code,
                message=message,
                path=path,
            )
        )

    def warning(self, code: str, message: str, path: list[str | int]) -> None:
        """追加允许策略发布但需要用户确认的结构化警告。"""
        self.issues.append(
            ClassificationValidationIssue(
                severity="warning",
                code=code,
                message=message,
                path=path,
            )
        )


class ClassificationPolicyValidator:
    """
    媒体分类策略纯校验器

    校验器只检查策略、标准字段目录和调用方提供的扩展字段，不访问配置、插件或持久化状态。
    """

    @classmethod
    def validate(
        cls,
        policy: ClassificationPolicy,
        extra_fields: Iterable[ClassificationFieldDefinition] = (),
    ) -> ClassificationValidationResult:
        """
        校验分类策略是否满足发布约束

        :param policy: 待发布的完整分类策略
        :param extra_fields: 当前已注册来源声明的扩展字段
        :return: 包含 error 和 warning 的结构化校验结果
        """
        collector = _ValidationCollector()
        field_definitions = (*standard_field_definitions(), *tuple(extra_fields))
        cls._validate_fields(field_definitions, collector)
        fields = {definition.id: definition for definition in field_definitions}
        categories = cls._validate_categories(policy, collector)
        cls._validate_rules(policy, categories, fields, collector)
        cls._validate_fallbacks(policy, categories, collector)
        cls._validate_aliases(policy, fields, collector)
        cls._warn_overlapping_rules(policy, collector)
        return ClassificationValidationResult(
            valid=not any(issue.severity == "error" for issue in collector.issues),
            issues=collector.issues,
        )

    @classmethod
    def _validate_fields(
        cls,
        extra_fields: Sequence[ClassificationFieldDefinition],
        collector: _ValidationCollector,
    ) -> None:
        """校验扩展字段的 ID、类型、操作符和来源命名空间。"""
        standard_ids = {definition.id for definition in standard_field_definitions()}
        seen_ids: set[str] = set()
        for index, definition in enumerate(extra_fields):
            path: list[str | int] = ["extra_fields", index]
            if definition.id in seen_ids:
                collector.error(
                    "duplicate_field_id",
                    f"字段 ID {definition.id} 重复或覆盖了标准字段",
                    [*path, "id"],
                )
            seen_ids.add(definition.id)
            if definition.value_type not in VALUE_TYPE_OPERATORS:
                collector.error(
                    "unsupported_value_type",
                    f"字段 {definition.id} 使用了不支持的值类型 {definition.value_type}",
                    [*path, "value_type"],
                )
            else:
                allowed = set(VALUE_TYPE_OPERATORS[definition.value_type])
                unsupported = [operator for operator in definition.operators if operator not in allowed]
                if unsupported:
                    collector.error(
                        "unsupported_field_operator",
                        f"字段 {definition.id} 声明了不兼容操作符：{', '.join(unsupported)}",
                        [*path, "operators"],
                    )
            cls._validate_media_types(
                definition.media_types,
                [*path, "media_types"],
                collector,
            )
            if not definition.id.startswith("extensions."):
                if definition.id not in standard_ids:
                    collector.error(
                        "invalid_extension_field_id",
                        f"非标准字段 {definition.id} 必须位于 extensions 命名空间",
                        [*path, "id"],
                    )
                continue
            extension_sources = [
                source for source, support in definition.source_support.items() if support == "extension"
            ]
            if len(extension_sources) != 1:
                collector.error(
                    "invalid_extension_source",
                    f"扩展字段 {definition.id} 必须声明且只能声明一个 extension 来源",
                    [*path, "source_support"],
                )
                continue
            source = extension_sources[0]
            prefix = f"extensions.{source}."
            if not _SOURCE_ID_PATTERN.fullmatch(source) or not definition.id.startswith(prefix):
                collector.error(
                    "extension_namespace_mismatch",
                    f"扩展字段 {definition.id} 必须位于 {prefix} 命名空间",
                    [*path, "id"],
                )
            suffix = definition.id[len(prefix) :]
            if not suffix or any(not _SOURCE_ID_PATTERN.fullmatch(segment) for segment in suffix.split(".")):
                collector.error(
                    "invalid_extension_field_id",
                    f"扩展字段 {definition.id} 的来源内字段路径无效",
                    [*path, "id"],
                )

    @classmethod
    def _validate_categories(
        cls,
        policy: ClassificationPolicy,
        collector: _ValidationCollector,
    ) -> dict[str, Any]:
        """校验分类 ID、名称、路径和同媒体类型路径唯一性。"""
        categories: dict[str, Any] = {}
        paths: dict[tuple[str, tuple[str, ...]], str] = {}
        for index, category in enumerate(policy.categories):
            path: list[str | int] = ["categories", index]
            if category.id in categories:
                collector.error(
                    "duplicate_category_id",
                    f"分类 ID {category.id} 重复",
                    [*path, "id"],
                )
            else:
                categories[category.id] = category
            if not _STABLE_ID_PATTERN.fullmatch(category.id):
                collector.error(
                    "invalid_category_id",
                    f"分类 ID {category.id} 只能包含小写字母、数字、点、下划线和连字符",
                    [*path, "id"],
                )
            if not category.name.strip():
                collector.error(
                    "empty_category_name",
                    "分类名称不能为空",
                    [*path, "name"],
                )
            cls._validate_category_path(category.path, path, collector)
            normalized_path = tuple(_normalize_path_segment(item) for item in category.path)
            path_key = (category.media_type, normalized_path)
            existing = paths.get(path_key)
            if normalized_path and existing:
                collector.error(
                    "duplicate_category_path",
                    f"分类 {category.id} 与 {existing} 在同一媒体类型下使用了相同路径",
                    [*path, "path"],
                )
            elif normalized_path:
                paths[path_key] = category.id
        return categories

    @classmethod
    def _validate_category_path(
        cls,
        segments: Sequence[str],
        base_path: list[str | int],
        collector: _ValidationCollector,
    ) -> None:
        """校验分类相对路径的层级、段长度和跨平台文件名安全性。"""
        for violation in _category_path_violations(segments):
            path = [*base_path, "path"]
            if violation.segment_index is not None:
                path.append(violation.segment_index)
            collector.error(
                "invalid_category_path",
                violation.message,
                path,
            )

    @classmethod
    def _validate_rules(
        cls,
        policy: ClassificationPolicy,
        categories: Mapping[str, Any],
        fields: Mapping[str, ClassificationFieldDefinition],
        collector: _ValidationCollector,
    ) -> None:
        """校验规则身份、目标引用、条件复杂度和字段契约。"""
        if len(policy.rules) > MAX_RULES:
            collector.error(
                "max_rules_exceeded",
                f"策略最多允许 {MAX_RULES} 条规则",
                ["rules"],
            )
        seen_ids: set[str] = set()
        total_conditions = 0
        for index, rule in enumerate(policy.rules):
            path: list[str | int] = ["rules", index]
            if rule.id in seen_ids:
                collector.error(
                    "duplicate_rule_id",
                    f"规则 ID {rule.id} 重复",
                    [*path, "id"],
                )
            seen_ids.add(rule.id)
            if not _STABLE_ID_PATTERN.fullmatch(rule.id):
                collector.error(
                    "invalid_rule_id",
                    f"规则 ID {rule.id} 只能包含小写字母、数字、点、下划线和连字符",
                    [*path, "id"],
                )
            if not rule.name.strip():
                collector.error("empty_rule_name", "规则名称不能为空", [*path, "name"])
            cls._validate_media_types(rule.media_types, [*path, "media_types"], collector)
            for source_index, source in enumerate(rule.sources):
                if not _SOURCE_ID_PATTERN.fullmatch(source):
                    collector.error(
                        "invalid_rule_source",
                        f"规则来源 {source} 不是合法数据源标识",
                        [*path, "sources", source_index],
                    )
            cls._validate_target(rule, categories, path, collector)
            condition_count = cls._validate_condition_node(
                rule.when,
                rule,
                fields,
                [*path, "when"],
                1,
                collector,
            )
            total_conditions += condition_count
            if condition_count == 0:
                collector.error(
                    "empty_rule_condition",
                    "普通规则必须至少包含一个叶子条件；兜底分类请使用 fallbacks",
                    [*path, "when"],
                )
            if condition_count > MAX_CONDITIONS_PER_RULE:
                collector.error(
                    "max_conditions_exceeded",
                    f"每条规则最多允许 {MAX_CONDITIONS_PER_RULE} 个叶子条件",
                    [*path, "when"],
                )
        if total_conditions > MAX_TOTAL_CONDITIONS:
            collector.error(
                "too_many_policy_conditions",
                f"策略最多允许 {MAX_TOTAL_CONDITIONS} 个叶子条件",
                ["rules"],
            )

    @classmethod
    def _validate_target(
        cls,
        rule: ClassificationRule,
        categories: Mapping[str, Any],
        base_path: list[str | int],
        collector: _ValidationCollector,
    ) -> None:
        """校验分类规则和标签规则各自允许的目标结构。"""
        category_id = rule.target.category_id
        if rule.kind == "label":
            if category_id:
                collector.error(
                    "label_rule_category_target",
                    "标签规则不能指定目标分类",
                    [*base_path, "target", "category_id"],
                )
            if not rule.target.labels:
                collector.error(
                    "empty_label_target",
                    "标签规则至少需要输出一个标签",
                    [*base_path, "target", "labels"],
                )
            return
        if not category_id:
            collector.error(
                "missing_category_target",
                "分类规则必须指定目标分类 ID",
                [*base_path, "target", "category_id"],
            )
            return
        category = categories.get(category_id)
        if not category:
            collector.error(
                "target_category_not_found",
                f"规则引用了不存在的分类 {category_id}",
                [*base_path, "target", "category_id"],
            )
            return
        if not category.enabled:
            collector.error(
                "disabled_category_target",
                f"规则引用的分类 {category_id} 已禁用",
                [*base_path, "target", "category_id"],
            )
        unsupported_types = [media_type for media_type in rule.media_types if media_type != category.media_type]
        if unsupported_types:
            collector.error(
                "target_media_type_mismatch",
                f"规则媒体类型与目标分类 {category_id} 的媒体类型不一致",
                [*base_path, "target", "category_id"],
            )

    @classmethod
    def _validate_condition_node(
        cls,
        node: ClassificationConditionNode,
        rule: ClassificationRule,
        fields: Mapping[str, ClassificationFieldDefinition],
        path: list[str | int],
        depth: int,
        collector: _ValidationCollector,
    ) -> int:
        """递归校验条件树并返回叶子条件数量。"""
        if isinstance(node, ClassificationCondition):
            cls._validate_condition(node, rule, fields, path, collector)
            return 1
        if depth > MAX_CONDITION_DEPTH:
            collector.error(
                "max_depth_exceeded",
                f"条件组最多允许 {MAX_CONDITION_DEPTH} 层",
                path,
            )
        group_name, children = _group_children(node)
        count = 0
        for index, child in enumerate(children):
            count += cls._validate_condition_node(
                child,
                rule,
                fields,
                [*path, group_name, index] if group_name != "not" else [*path, "not"],
                depth + 1,
                collector,
            )
        return count

    @classmethod
    def _validate_condition(
        cls,
        condition: ClassificationCondition,
        rule: ClassificationRule,
        fields: Mapping[str, ClassificationFieldDefinition],
        path: list[str | int],
        collector: _ValidationCollector,
    ) -> None:
        """校验叶子条件的字段、操作符、媒体类型、来源范围和值结构。"""
        definition = fields.get(condition.field)
        if not definition:
            collector.error(
                "unknown_field",
                f"条件引用了未登记字段 {condition.field}",
                [*path, "field"],
            )
            return
        if condition.operator not in definition.operators:
            collector.error(
                "unsupported_operator",
                f"字段 {condition.field} 不支持操作符 {condition.operator}",
                [*path, "operator"],
            )
        unsupported_types = [media_type for media_type in rule.media_types if media_type not in definition.media_types]
        if unsupported_types:
            collector.error(
                "field_media_type_mismatch",
                f"字段 {condition.field} 不适用于媒体类型：{', '.join(unsupported_types)}",
                [*path, "field"],
            )
        cls._validate_condition_value(condition, definition, path, collector)
        cls._validate_extension_scope(condition.field, rule, definition, path, collector)
        cls._warn_partial_support(condition.field, rule, definition, path, collector)

    @classmethod
    def _validate_condition_value(
        cls,
        condition: ClassificationCondition,
        definition: ClassificationFieldDefinition,
        path: list[str | int],
        collector: _ValidationCollector,
    ) -> None:
        """按字段值类型校验条件值和范围结构。"""
        operator = condition.operator
        value = condition.value
        if operator in {"exists", "not_exists", "is_true", "is_false"}:
            if value is not None:
                collector.error(
                    "unexpected_condition_value",
                    f"操作符 {operator} 不接受条件值",
                    [*path, "value"],
                )
            return
        if operator in {"in", "not_in"}:
            if not _is_non_empty_scalar_list(value):
                collector.error(
                    "empty_membership_value",
                    f"操作符 {operator} 需要非空标量列表",
                    [*path, "value"],
                )
            else:
                members = cast(list[ClassificationFactScalar], value)
                if definition.value_type not in {"string", "enum"} or all(
                    isinstance(item, str) for item in members
                ):
                    return
                collector.error(
                    "invalid_condition_value",
                    f"字段 {condition.field} 的成员值必须是字符串",
                    [*path, "value"],
                )
            return
        if operator in {"contains_any", "contains_all", "contains_none"}:
            if not _is_non_empty_scalar_list(value):
                collector.error(
                    "invalid_condition_value",
                    f"操作符 {operator} 需要非空字符串列表",
                    [*path, "value"],
                )
                return
            members = cast(list[ClassificationFactScalar], value)
            if not all(isinstance(item, str) for item in members):
                collector.error(
                    "invalid_condition_value",
                    f"操作符 {operator} 需要非空字符串列表",
                    [*path, "value"],
                )
            return
        if operator == "between":
            if not isinstance(value, list) or len(value) != 2 or not all(_is_number(item) for item in value):
                collector.error(
                    "invalid_condition_value",
                    "between 需要包含两个数字的闭区间",
                    [*path, "value"],
                )
                return
            bounds = cast(list[int | float], value)
            if bounds[0] > bounds[1]:
                collector.error(
                    "invalid_between_range",
                    "between 的范围起点不能大于终点",
                    [*path, "value"],
                )
            return
        if definition.value_type in {"integer", "year"}:
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif definition.value_type == "number":
            valid = _is_number(value)
        elif definition.value_type in {"string", "enum"}:
            valid = isinstance(value, str)
        else:
            valid = value is not None
        if not valid:
            collector.error(
                "invalid_condition_value",
                f"条件值与字段 {condition.field} 的类型 {definition.value_type} 不匹配",
                [*path, "value"],
            )
        if definition.value_type == "enum" and valid:
            options = {_option_value(item) for item in definition.options}
            if options and value not in options:
                collector.error(
                    "unknown_enum_value",
                    f"条件值 {value} 不在字段 {condition.field} 的可选值中",
                    [*path, "value"],
                )

    @classmethod
    def _validate_extension_scope(
        cls,
        field_id: str,
        rule: ClassificationRule,
        definition: ClassificationFieldDefinition,
        path: list[str | int],
        collector: _ValidationCollector,
    ) -> None:
        """确保来源扩展字段只被其声明来源范围内的规则引用。"""
        extension_sources = [source for source, support in definition.source_support.items() if support == "extension"]
        if not field_id.startswith("extensions.") or not extension_sources:
            return
        source = extension_sources[0]
        if rule.sources and source not in rule.sources:
            collector.error(
                "extension_namespace_mismatch",
                f"字段 {field_id} 属于来源 {source}，但规则未包含该来源",
                [*path, "field"],
            )
        elif not rule.sources:
            collector.warning(
                "extension_field_all_sources",
                f"字段 {field_id} 只由来源 {source} 提供，其它来源下条件不会命中",
                [*path, "field"],
            )

    @classmethod
    def _warn_partial_support(
        cls,
        field_id: str,
        rule: ClassificationRule,
        definition: ClassificationFieldDefinition,
        path: list[str | int],
        collector: _ValidationCollector,
    ) -> None:
        """提示规则范围内只提供部分事实覆盖的数据源。"""
        scoped_sources = rule.sources or list(definition.source_support)
        partial_sources = [source for source in scoped_sources if definition.source_support.get(source) == "partial"]
        if partial_sources:
            collector.warning(
                "partial_field_support",
                f"字段 {field_id} 在以下来源仅部分可用：{', '.join(partial_sources)}",
                [*path, "field"],
            )

    @classmethod
    def _validate_fallbacks(
        cls,
        policy: ClassificationPolicy,
        categories: Mapping[str, Any],
        collector: _ValidationCollector,
    ) -> None:
        """确保每种媒体类型的全局兜底引用同类型的可用分类。"""
        for media_type in ALL_MEDIA_TYPES:
            category_id = policy.fallbacks.get(cast(ClassificationMediaType, media_type))
            path: list[str | int] = ["fallbacks", media_type]
            if not category_id:
                collector.error(
                    "missing_fallback",
                    f"媒体类型 {media_type} 未配置兜底分类",
                    path,
                )
                continue
            category = categories.get(category_id)
            if not category:
                collector.error(
                    "unknown_fallback_category",
                    f"兜底分类 {category_id} 不存在",
                    path,
                )
            elif category.media_type != media_type:
                collector.error(
                    "fallback_media_type_mismatch",
                    f"兜底分类 {category_id} 不属于媒体类型 {media_type}",
                    path,
                )
            elif not category.enabled:
                collector.error(
                    "disabled_fallback_category",
                    f"兜底分类 {category_id} 已禁用",
                    path,
                )

    @classmethod
    def _validate_aliases(
        cls,
        policy: ClassificationPolicy,
        fields: Mapping[str, ClassificationFieldDefinition],
        collector: _ValidationCollector,
    ) -> None:
        """校验字段别名只引用已登记字段且不形成直接或间接循环。"""
        for field_id, aliases in policy.field_aliases.items():
            path: list[str | int] = ["field_aliases", field_id]
            if field_id not in fields:
                collector.error(
                    "unknown_alias_field",
                    f"字段别名引用了未登记字段 {field_id}",
                    path,
                )
            for alias, canonical in aliases.items():
                if not alias or not canonical:
                    collector.error(
                        "empty_field_alias",
                        "字段别名和规范值均不能为空",
                        [*path, alias],
                    )
                elif _alias_has_cycle(alias, aliases):
                    collector.error(
                        "cyclic_field_alias",
                        f"字段 {field_id} 的别名 {alias} 形成循环映射",
                        [*path, alias],
                    )

    @classmethod
    def _validate_media_types(
        cls,
        media_types: Sequence[str],
        path: list[str | int],
        collector: _ValidationCollector,
    ) -> None:
        """校验媒体类型集合非空、不重复且属于分类体系支持范围。"""
        if not media_types:
            collector.error("empty_media_types", "媒体类型不能为空", path)
            return
        if len(set(media_types)) != len(media_types):
            collector.error("duplicate_media_types", "媒体类型不能重复", path)
        unsupported = [item for item in media_types if item not in ALL_MEDIA_TYPES]
        if unsupported:
            collector.error(
                "unsupported_media_type",
                f"不支持的媒体类型：{', '.join(unsupported)}",
                path,
            )

    @classmethod
    def _warn_overlapping_rules(
        cls,
        policy: ClassificationPolicy,
        collector: _ValidationCollector,
    ) -> None:
        """对条件完全相同且适用范围重叠的主分类规则给出顺序提示。"""
        seen: dict[str, ClassificationRule] = {}
        for index, rule in enumerate(policy.rules):
            if not rule.enabled or rule.kind != "category":
                continue
            fingerprint = json.dumps(
                rule.when.model_dump(mode="json", by_alias=True),
                ensure_ascii=False,
                sort_keys=True,
            )
            previous = seen.get(fingerprint)
            if previous and _rules_overlap(previous, rule):
                collector.warning(
                    "overlapping_category_rules",
                    f"规则 {rule.id} 与 {previous.id} 条件相同，将按列表顺序首条命中",
                    ["rules", index, "when"],
                )
            else:
                seen[fingerprint] = rule


def _group_children(
    group: ClassificationConditionGroup,
) -> tuple[str, list[ClassificationConditionNode]]:
    """读取严格条件组中的操作符和子节点。"""
    if group.all is not None:
        return "all", group.all
    if group.any is not None:
        return "any", group.any
    if group.not_ is None:
        return "not", []
    return "not", [group.not_]


def _path_segment_is_illegal(segment: str) -> bool:
    """判断路径段是否触发目录穿越、控制字符或跨平台非法文件名约束。"""
    if segment in {".", ".."} or segment != segment.strip():
        return True
    if segment.endswith((".", " ")):
        return True
    if any(character in _ILLEGAL_PATH_CHARACTERS for character in segment):
        return True
    if any(ord(character) < 32 for character in segment):
        return True
    basename = segment.split(".", 1)[0].upper()
    return basename in _WINDOWS_RESERVED_NAMES


def _category_path_violations(
    segments: Sequence[str],
) -> tuple[_CategoryPathViolation, ...]:
    """返回分类路径的全部违规，供发布校验和运行时防御复用。"""
    if isinstance(segments, (str, bytes)):
        return (_CategoryPathViolation("分类目录路径必须使用路径段数组"),)
    values = tuple(segments)
    if not values:
        return (_CategoryPathViolation("分类目录路径不能为空"),)
    violations: list[_CategoryPathViolation] = []
    if len(values) > MAX_CATEGORY_DEPTH:
        violations.append(
            _CategoryPathViolation(
                f"分类目录最多允许 {MAX_CATEGORY_DEPTH} 级"
            )
        )
    for index, segment in enumerate(values):
        if not isinstance(segment, str) or not segment or not segment.strip():
            violations.append(_CategoryPathViolation("目录路径段不能为空", index))
            continue
        if len(segment) > MAX_CATEGORY_SEGMENT_LENGTH:
            violations.append(
                _CategoryPathViolation(
                    f"目录路径段最多允许 {MAX_CATEGORY_SEGMENT_LENGTH} 个字符",
                    index,
                )
            )
        if _path_segment_is_illegal(segment):
            violations.append(
                _CategoryPathViolation(
                    f"目录路径段 {segment} 包含目录穿越或平台非法文件名",
                    index,
                )
            )
    if all(isinstance(segment, str) for segment in values) and len(
        "/".join(values)
    ) > MAX_CATEGORY_PATH_LENGTH:
        violations.append(
            _CategoryPathViolation(
                f"分类目录总长度最多允许 {MAX_CATEGORY_PATH_LENGTH} 个字符"
            )
        )
    return tuple(violations)


def _normalize_path_segment(segment: str) -> str:
    """按 Unicode NFC 和大小写折叠构造跨平台路径唯一键。"""
    return unicodedata.normalize("NFC", segment).casefold()


def _is_number(value: Any) -> bool:
    """判断值是否为分类规则支持的整数或浮点数，布尔值不属于数字。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_non_empty_scalar_list(value: Any) -> bool:
    """判断值是否为不含 null 或嵌套对象的非空标量列表。"""
    return isinstance(value, list) and bool(value) and all(isinstance(item, (str, int, float, bool)) for item in value)


def _option_value(option: Any) -> Any:
    """兼容简单枚举值和包含 value 字段的前端选项目录。"""
    if isinstance(option, Mapping):
        return option.get("value")
    if hasattr(option, "value"):
        return option.value
    return option


def _alias_has_cycle(alias: str, aliases: Mapping[str, str]) -> bool:
    """检测从一个别名出发是否会回到已经访问过的别名。"""
    visited: set[str] = set()
    current = alias
    while current in aliases:
        if current in visited:
            return True
        visited.add(current)
        current = aliases[current]
    return False


def _rules_overlap(left: ClassificationRule, right: ClassificationRule) -> bool:
    """判断两条规则的媒体类型和来源范围是否存在交集。"""
    if not set(left.media_types).intersection(right.media_types):
        return False
    if not left.sources or not right.sources:
        return True
    return bool(set(left.sources).intersection(right.sources))
