from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, cast

from app.schemas.category import (
    ClassificationCondition,
    ClassificationConditionGroup,
    ClassificationConditionNode,
    ClassificationConditionTrace,
    ClassificationEvaluation,
    ClassificationEvaluationWarning,
    ClassificationFacts,
    ClassificationMediaType,
    ClassificationPolicy,
    ClassificationResult,
    ClassificationRuleTrace,
    ClassificationSelection,
)

_MISSING = object()


@dataclass(frozen=True)
class _ConditionOutcome:
    matched: bool
    missing_fields: frozenset[str]
    traces: tuple[ClassificationConditionTrace, ...]


def read_fact(facts: ClassificationFacts, path: str) -> tuple[Any, bool]:
    """
    按点路径读取分类事实

    :param facts: 标准化分类事实
    :param path: 例如 ``media.year`` 的字段路径
    :return: 二元组为字段值和是否缺失；不存在及显式 null 都视为缺失
    """
    if path.startswith("extensions."):
        return _read_extension_fact(facts, path)
    current: Any = facts
    for segment in path.split("."):
        if not segment:
            return None, True
        current = _read_segment(current, segment)
        if current is _MISSING or current is None:
            return None, True
    return current, False


def _read_extension_fact(
    facts: ClassificationFacts,
    path: str,
) -> tuple[Any, bool]:
    """按已登记来源键读取扁平扩展事实，来源和局部字段均允许包含点号。"""
    remainder = path.removeprefix("extensions.")
    for source in sorted(facts.extensions, key=len, reverse=True):
        prefix = f"{source}."
        if not remainder.startswith(prefix):
            continue
        field = remainder[len(prefix):]
        if not field:
            return None, True
        value = facts.extensions[source].get(field, _MISSING)
        return (None, True) if value is _MISSING or value is None else (value, False)
    return None, True


class ClassificationEvaluator:
    """
    媒体分类策略纯求值器

    求值器不读取配置或外部数据，只基于策略快照和标准化事实生成确定性结果。
    """

    @classmethod
    def evaluate(
        cls,
        policy: ClassificationPolicy,
        facts: ClassificationFacts,
        trace: bool = False,
    ) -> ClassificationEvaluation:
        """
        对事实执行有序分类和标签规则

        :param policy: 已通过发布校验的分类策略
        :param facts: 当前媒体的标准化分类事实
        :param trace: 是否返回实际执行到的规则与叶子条件轨迹
        :return: 分类结果及可选解释轨迹
        """
        media_type, _ = read_fact(facts, "media.type")
        media_source, _ = read_fact(facts, "identity.media_source")
        normalized_media_type = str(_scalar_value(media_type))
        normalized_media_source = str(_scalar_value(media_source))
        categories = {category.id: category for category in policy.categories}
        selected_category_id: Optional[str] = None
        selected_rule_id: Optional[str] = None
        labels: list[str] = []
        traces: list[ClassificationRuleTrace] = []
        missing_fields: set[str] = set()
        fact_cache: dict[str, tuple[Any, bool]] = {}

        for rule_index, rule in enumerate(policy.rules):
            if not rule.enabled:
                continue
            if normalized_media_type not in rule.media_types:
                continue
            if rule.sources and normalized_media_source not in rule.sources:
                continue
            if rule.kind == "category" and selected_category_id:
                continue

            outcome = cls._evaluate_node(
                rule.when,
                facts,
                policy.field_aliases,
                fact_cache,
                trace,
                ["rules", rule_index, "when"] if trace else [],
            )
            missing_fields.update(outcome.missing_fields)
            if outcome.matched:
                labels.extend(rule.target.labels)
                if rule.kind == "category" and rule.target.category_id:
                    selected_category_id = rule.target.category_id
                    selected_rule_id = rule.id
            if trace:
                traces.append(
                    ClassificationRuleTrace(
                        rule_id=rule.id,
                        matched=outcome.matched,
                        conditions=list(outcome.traces),
                    )
                )

        selection_source = "automatic"
        if not selected_category_id:
            source_fallbacks = policy.source_fallbacks.get(normalized_media_source, {})
            selected_category_id = source_fallbacks.get(
                cast(ClassificationMediaType, normalized_media_type)
            )
            if selected_category_id:
                selection_source = "source_fallback"
            else:
                selected_category_id = policy.fallbacks.get(
                    cast(ClassificationMediaType, normalized_media_type)
                )
                selection_source = "fallback"

        category = categories.get(selected_category_id or "")
        if category:
            labels = [*category.labels, *labels]
        labels = _stable_unique(labels)
        selection = ClassificationSelection(
            category_id=selected_category_id,
            category_path=list(category.path) if category else [],
            rule_id=selected_rule_id,
            source=selection_source,
        )
        result = ClassificationResult(
            recommended=selection,
            effective=selection,
            labels=labels,
            policy_revision=policy.revision,
            state="partial" if missing_fields else "complete",
        )
        warnings = [
            ClassificationEvaluationWarning(
                code="missing_fact",
                message=f"字段 {field_id} 缺失，相关条件按不匹配处理",
                field=field_id,
                source=normalized_media_source,
            )
            for field_id in sorted(missing_fields)
        ]
        return ClassificationEvaluation(
            facts=facts,
            result=result,
            trace=traces,
            warnings=warnings,
        )

    @classmethod
    def evaluate_condition(
        cls,
        condition: ClassificationConditionNode,
        facts: ClassificationFacts,
    ) -> bool:
        """返回单个条件树是否匹配，不构造解释轨迹。"""
        return cls._evaluate_node(condition, facts, {}, {}, False, []).matched

    @classmethod
    def trace_condition(
        cls,
        condition: ClassificationConditionNode,
        facts: ClassificationFacts,
    ) -> list[ClassificationConditionTrace]:
        """执行单个条件树并返回短路后实际求值到的叶子轨迹。"""
        return list(cls._evaluate_node(condition, facts, {}, {}, True, []).traces)

    @classmethod
    def _evaluate_node(
        cls,
        node: ClassificationConditionNode,
        facts: ClassificationFacts,
        field_aliases: Mapping[str, Mapping[str, str]],
        fact_cache: dict[str, tuple[Any, bool]],
        trace: bool,
        path: list[str | int],
    ) -> _ConditionOutcome:
        """递归求值条件叶子或条件组。"""
        if isinstance(node, ClassificationCondition):
            return cls._evaluate_leaf(
                node, facts, field_aliases, fact_cache, trace, path
            )
        return cls._evaluate_group(
            node, facts, field_aliases, fact_cache, trace, path
        )

    @staticmethod
    def _evaluate_leaf(
        condition: ClassificationCondition,
        facts: ClassificationFacts,
        field_aliases: Mapping[str, Mapping[str, str]],
        fact_cache: dict[str, tuple[Any, bool]],
        trace: bool,
        path: list[str | int],
    ) -> _ConditionOutcome:
        """执行叶子条件并应用统一缺失值语义。"""
        fact = fact_cache.get(condition.field)
        if fact is None:
            fact = read_fact(facts, condition.field)
            fact_cache[condition.field] = fact
        actual, missing = fact
        if condition.operator == "not_exists":
            matched = missing
        elif condition.operator == "exists":
            matched = not missing
        elif missing:
            matched = False
        else:
            aliases = field_aliases.get(condition.field, {})
            matched = _matches_operator(
                condition.operator,
                _apply_aliases(actual, aliases),
                _apply_aliases(condition.value, aliases),
            )
        condition_trace: tuple[ClassificationConditionTrace, ...] = ()
        if trace:
            fact_source = facts.field_sources.get(condition.field)
            condition_trace = (
                ClassificationConditionTrace(
                    field=condition.field,
                    operator=condition.operator,
                    expected=condition.value,
                    actual=None if missing else actual,
                    source=(
                        fact_source.model_copy(deep=True)
                        if fact_source is not None
                        else None
                    ),
                    matched=matched,
                    path=list(path),
                ),
            )
        # not_exists 显式以缺失为目标，不应把预期缺失标记为事实不完整。
        missing_fields = (
            frozenset() if not missing or condition.operator == "not_exists" else frozenset({condition.field})
        )
        return _ConditionOutcome(matched, missing_fields, condition_trace)

    @classmethod
    def _evaluate_group(
        cls,
        group: ClassificationConditionGroup,
        facts: ClassificationFacts,
        field_aliases: Mapping[str, Mapping[str, str]],
        fact_cache: dict[str, tuple[Any, bool]],
        trace: bool,
        path: list[str | int],
    ) -> _ConditionOutcome:
        """按 all、any、not 语义递归执行条件组并保留短路边界。"""
        group_kind, children = _group_children(group)
        traces: list[ClassificationConditionTrace] = []
        missing_fields: set[str] = set()

        if group_kind == "not":
            outcome = cls._evaluate_node(
                children[0],
                facts,
                field_aliases,
                fact_cache,
                trace,
                [*path, "not"] if trace else path,
            )
            # not 不能把普通字段缺失反转成命中；匹配缺失必须显式使用 not_exists。
            matched = False if outcome.missing_fields else not outcome.matched
            return _ConditionOutcome(matched, outcome.missing_fields, outcome.traces)

        if group_kind == "all":
            matched = True
            for index, child in enumerate(children):
                outcome = cls._evaluate_node(
                    child,
                    facts,
                    field_aliases,
                    fact_cache,
                    trace,
                    [*path, "all", index] if trace else path,
                )
                traces.extend(outcome.traces)
                missing_fields.update(outcome.missing_fields)
                if not outcome.matched:
                    matched = False
                    break
            return _ConditionOutcome(
                matched,
                frozenset(missing_fields),
                tuple(traces),
            )

        matched = False
        for index, child in enumerate(children):
            outcome = cls._evaluate_node(
                child,
                facts,
                field_aliases,
                fact_cache,
                trace,
                [*path, "any", index] if trace else path,
            )
            traces.extend(outcome.traces)
            missing_fields.update(outcome.missing_fields)
            if outcome.matched:
                matched = True
                break
        return _ConditionOutcome(matched, frozenset(missing_fields), tuple(traces))


def _read_segment(value: Any, segment: str) -> Any:
    """读取映射或模型属性，禁止把对象私有属性暴露为分类事实。"""
    if segment.startswith("_"):
        return _MISSING
    if isinstance(value, Mapping):
        return value.get(segment, _MISSING)
    return getattr(value, segment, _MISSING)


def _matches_operator(operator: str, actual: Any, expected: Any) -> bool:
    """执行单个非缺失字段操作符。"""
    actual = _comparable_value(actual)
    expected = _comparable_value(expected)
    if operator == "equals":
        return bool(actual == expected)
    if operator == "not_equals":
        return bool(actual != expected)
    if operator == "in":
        return _contains_value(expected, actual)
    if operator == "not_in":
        return not _contains_value(expected, actual)
    if operator == "contains":
        return isinstance(actual, str) and isinstance(expected, str) and expected in actual
    if operator == "starts_with":
        return isinstance(actual, str) and isinstance(expected, str) and actual.startswith(expected)
    if operator == "ends_with":
        return isinstance(actual, str) and isinstance(expected, str) and actual.endswith(expected)
    if operator in {"gt", "gte", "lt", "lte"}:
        return _compare_order(operator, actual, expected)
    if operator == "between":
        return _between(actual, expected)
    if operator in {"contains_any", "contains_all", "contains_none"}:
        return _compare_collection(operator, actual, expected)
    if operator == "is_true":
        return actual is True
    if operator == "is_false":
        return actual is False
    return False


def _compare_order(operator: str, actual: Any, expected: Any) -> bool:
    """执行可比较标量的大小关系，类型不兼容时按不匹配处理。"""
    if isinstance(actual, bool) or isinstance(expected, bool):
        return False
    try:
        if operator == "gt":
            return bool(actual > expected)
        if operator == "gte":
            return bool(actual >= expected)
        if operator == "lt":
            return bool(actual < expected)
        return bool(actual <= expected)
    except TypeError:
        return False


def _between(actual: Any, expected: Any) -> bool:
    """判断标量是否位于闭区间。"""
    if isinstance(actual, bool) or not _is_collection(expected) or len(expected) != 2:
        return False
    lower, upper = expected
    if isinstance(lower, bool) or isinstance(upper, bool):
        return False
    try:
        return bool(lower <= actual <= upper)
    except TypeError:
        return False


def _compare_collection(operator: str, actual: Any, expected: Any) -> bool:
    """按成员相等语义执行列表包含操作符。"""
    if not _is_collection(actual) or not _is_collection(expected):
        return False
    actual_values = list(actual)
    expected_values = list(expected)
    memberships = [item in actual_values for item in expected_values]
    if operator == "contains_any":
        return any(memberships)
    if operator == "contains_all":
        return all(memberships)
    return not any(memberships)


def _contains_value(container: Any, value: Any) -> bool:
    """只允许显式集合执行 in/not_in，避免字符串被误当成字符集合。"""
    return _is_collection(container) and value in container


def _is_collection(value: Any) -> bool:
    """判断值是否为规则支持的非字符串序列或集合。"""
    return isinstance(value, (list, tuple, set, frozenset))


def _comparable_value(value: Any) -> Any:
    """递归展开枚举值，保留列表结构用于集合操作。"""
    if isinstance(value, Enum):
        return value.value
    if _is_collection(value):
        return [_comparable_value(item) for item in value]
    return value


def _apply_aliases(value: Any, aliases: Mapping[str, str]) -> Any:
    """递归将字段别名投影为策略声明的规范值。"""
    if _is_collection(value):
        return [_apply_aliases(item, aliases) for item in value]
    if isinstance(value, str):
        return aliases.get(value, value)
    return value


def _group_children(
    group: ClassificationConditionGroup,
) -> tuple[str, list[ClassificationConditionNode]]:
    """读取已经通过 schema 保证互斥的条件组操作符和子节点。"""
    if group.all is not None:
        return "all", group.all
    if group.any is not None:
        return "any", group.any
    if group.not_ is None:
        return "not", []
    return "not", [group.not_]


def _scalar_value(value: Any) -> Any:
    """将枚举标量规范为其持久化值。"""
    return value.value if isinstance(value, Enum) else value


def _stable_unique(values: Sequence[str]) -> list[str]:
    """按首次出现顺序去重标签。"""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
