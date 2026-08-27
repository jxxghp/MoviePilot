#!/usr/bin/env python3
"""校验人工审查的宿主 Event consumer 精确政策。"""

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from scripts.architecture.event_facts import fingerprint_event_fact
except ModuleNotFoundError:
    from event_facts import fingerprint_event_fact

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EVENT_POLICY_PATH = (
    PROJECT_ROOT
    / "tests"
    / "fixtures"
    / "architecture"
    / "runtime-contract-policy.json"
)
EVENT_CONSUMER_POLICY_SCOPE = {
    "root": "app",
    "excluded": ["app.plugins"],
    "receiver_contract": "canonical_event_manager_only",
}
EVENT_CONSUMER_FACT_FIELDS = (
    "caller",
    "qualname",
    "method",
    "receiver_kind",
    "events",
    "dynamic",
    "invalid",
    "handler",
    "registration_kind",
    "priority",
    "fingerprint",
)
EVENT_CONSUMER_POLICY_FIELDS = (
    *EVENT_CONSUMER_FACT_FIELDS,
    "classification",
    "owner",
    "reason",
)
STATIC_CLASSIFICATION = "approved_static_registration"
DYNAMIC_CLASSIFICATION = "approved_dynamic_exception"
_FINGERPRINT_PATTERN = re.compile(r"[0-9a-f]{64}")
_WILDCARD_CHARACTERS = frozenset("*?[")
_PLACEHOLDER_REASONS = frozenset({"todo", "tbd"})


@dataclass(frozen=True, slots=True)
class EventPolicyViolation:
    """描述一条可稳定排序和断言的 Event policy 违规。"""

    code: str
    fingerprint: str | None
    detail: str


@dataclass(frozen=True, slots=True)
class EventPolicyCheck:
    """汇总当前 consumer 事实、人工政策和全部校验结果。"""

    actual_count: int
    reviewed_count: int
    static_count: int
    dynamic_count: int
    invalid_count: int
    violations: tuple[EventPolicyViolation, ...]

    @property
    def ok(self) -> bool:
        """返回当前事实是否完整匹配人工政策。"""
        return not self.violations


def _violation(
    code: str,
    detail: str,
    fingerprint: object = None,
) -> EventPolicyViolation:
    """构造字段类型安全的违规对象。"""
    return EventPolicyViolation(
        code=code,
        fingerprint=fingerprint if isinstance(fingerprint, str) else None,
        detail=detail,
    )


def _sort_violations(
    violations: list[EventPolicyViolation],
) -> tuple[EventPolicyViolation, ...]:
    """按错误码、指纹和说明输出确定顺序。"""
    return tuple(
        sorted(
            violations,
            key=lambda item: (
                item.code,
                item.fingerprint or "",
                item.detail,
            ),
        )
    )


def _has_wildcard(value: object) -> bool:
    """判断字符串或字符串列表是否包含 glob 通配符。"""
    if isinstance(value, str):
        return any(character in value for character in _WILDCARD_CHARACTERS)
    if isinstance(value, list):
        return any(_has_wildcard(item) for item in value)
    return False


def _fact_projection(value: Mapping[str, object]) -> dict[str, object]:
    """提取 policy 与 collector 共用的完整 line-free consumer 字段。"""
    return {
        field: value[field]
        for field in EVENT_CONSUMER_FACT_FIELDS
        if field != "fingerprint" and field in value
    }


def _validate_fact_shape(
    value: object,
    *,
    source: str,
) -> list[EventPolicyViolation]:
    """校验 collector fact 或 policy entry 的公共语义字段。"""
    if not isinstance(value, Mapping):
        return [_violation("invalid_entry", f"{source} 必须是对象")]

    violations: list[EventPolicyViolation] = []
    fingerprint = value.get("fingerprint")
    missing = sorted(set(EVENT_CONSUMER_FACT_FIELDS) - set(value))
    if missing:
        violations.append(
            _violation(
                "invalid_entry",
                f"{source} 缺少字段：{', '.join(missing)}",
                fingerprint,
            )
        )
        return violations

    string_fields = (
        "caller",
        "qualname",
        "method",
        "receiver_kind",
        "handler",
        "registration_kind",
        "priority",
    )
    if any(
        not isinstance(value.get(field), str) or not str(value[field]).strip()
        for field in string_fields
    ):
        violations.append(
            _violation(
                "invalid_entry",
                f"{source} 的 consumer identity 字段必须是非空字符串",
                fingerprint,
            )
        )

    events = value.get("events")
    if (
        not isinstance(events, list)
        or any(not isinstance(event, str) or not event for event in events)
        or events != sorted(set(events))
    ):
        violations.append(
            _violation(
                "invalid_entry",
                f"{source}.events 必须是排序、去重的字符串列表",
                fingerprint,
            )
        )
    if not isinstance(value.get("dynamic"), bool) or not isinstance(
        value.get("invalid"), bool
    ):
        violations.append(
            _violation(
                "invalid_entry",
                f"{source}.dynamic/invalid 必须是布尔值",
                fingerprint,
            )
        )
    if not isinstance(fingerprint, str) or not _FINGERPRINT_PATTERN.fullmatch(
        fingerprint
    ):
        violations.append(
            _violation(
                "invalid_entry",
                f"{source}.fingerprint 必须是 64 位小写 SHA256",
                fingerprint,
            )
        )

    wildcard_fields = (
        "caller",
        "qualname",
        "method",
        "receiver_kind",
        "events",
        "handler",
        "registration_kind",
        "priority",
    )
    wildcard_names = [
        field for field in wildcard_fields if _has_wildcard(value.get(field))
    ]
    if wildcard_names:
        violations.append(
            _violation(
                "wildcard_entry",
                f"{source} 禁止通配字段：{', '.join(wildcard_names)}",
                fingerprint,
            )
        )
    caller = value.get("caller")
    if isinstance(caller, str) and (
        caller == "app.plugins" or caller.startswith("app.plugins.")
    ):
        violations.append(
            _violation(
                "plugin_scope_violation",
                f"{source} 不得包含插件副本：{caller}",
                fingerprint,
            )
        )
    if value.get("invalid") is True:
        violations.append(
            _violation(
                "invalid_consumer",
                f"{source} 的事件选择包含非法成员，不得获得政策准入",
                fingerprint,
            )
        )
    return violations


def _validate_policy_entry(
    value: object,
    *,
    index: int,
) -> list[EventPolicyViolation]:
    """校验人工 entry 的精确字段、分类、owner 和理由。"""
    source = f"policy.entries[{index}]"
    violations = _validate_fact_shape(value, source=source)
    if not isinstance(value, Mapping):
        return violations

    fingerprint = value.get("fingerprint")
    if set(value) != set(EVENT_CONSUMER_POLICY_FIELDS):
        violations.append(
            _violation(
                "invalid_entry",
                f"{source} 必须精确包含规定字段",
                fingerprint,
            )
        )
    dynamic = value.get("dynamic")
    expected_classification = (
        DYNAMIC_CLASSIFICATION if dynamic is True else STATIC_CLASSIFICATION
    )
    if value.get("classification") != expected_classification:
        violations.append(
            _violation(
                "classification_mismatch",
                f"{source}.classification 应为 {expected_classification}",
                fingerprint,
            )
        )
    owner = value.get("owner")
    if (
        not isinstance(owner, str)
        or not owner.strip()
        or owner != value.get("caller")
    ):
        violations.append(
            _violation(
                "owner_mismatch",
                f"{source}.owner 必须精确等于 caller",
                fingerprint,
            )
        )
    elif _has_wildcard(owner):
        violations.append(
            _violation(
                "wildcard_entry",
                f"{source}.owner 禁止通配符",
                fingerprint,
            )
        )
    reason = value.get("reason")
    if (
        not isinstance(reason, str)
        or not reason.strip()
        or reason.strip().lower() in _PLACEHOLDER_REASONS
    ):
        violations.append(
            _violation(
                "empty_reason",
                f"{source}.reason 必须是非占位的具体理由",
                fingerprint,
            )
        )
    return violations


def _fingerprint_errors(
    value: Mapping[str, object],
    *,
    source: str,
) -> list[EventPolicyViolation]:
    """使用统一 Event fact 摘要函数验证 line-free identity。"""
    fingerprint = value.get("fingerprint")
    if not isinstance(fingerprint, str):
        return []
    expected = fingerprint_event_fact(_fact_projection(value))
    if fingerprint == expected:
        return []
    return [
        _violation(
            "fingerprint_mismatch",
            f"{source}.fingerprint 与完整 consumer identity 不匹配",
            fingerprint,
        )
    ]


def _duplicate_fingerprint_errors(
    values: Sequence[Mapping[str, object]],
    *,
    code: str,
    source: str,
) -> list[EventPolicyViolation]:
    """拒绝会被集合比较吞掉的重复 consumer identity。"""
    fingerprints: list[str] = [
        fingerprint
        for value in values
        if isinstance((fingerprint := value.get("fingerprint")), str)
    ]
    duplicates = sorted({item for item in fingerprints if fingerprints.count(item) > 1})
    return [
        _violation(code, f"{source} 存在重复 fingerprint", fingerprint)
        for fingerprint in duplicates
    ]


def validate_event_consumer_policy(
    policy: Mapping[str, object],
    facts: Sequence[Mapping[str, object]],
) -> EventPolicyCheck:
    """按 exact fingerprint set 校验当前 consumer 事实与人工政策。"""
    violations: list[EventPolicyViolation] = []
    actual_facts = list(facts)
    valid_actual: list[Mapping[str, object]] = []
    for index, fact in enumerate(actual_facts):
        fact_errors = _validate_fact_shape(fact, source=f"facts[{index}]")
        violations.extend(fact_errors)
        if not any(error.code == "invalid_entry" for error in fact_errors):
            violations.extend(_fingerprint_errors(fact, source=f"facts[{index}]"))
            valid_actual.append(fact)

    if not isinstance(policy, Mapping) or set(policy) != {
        "schema_version",
        "scope",
        "event_consumers",
    }:
        violations.append(
            _violation("invalid_schema", "policy 顶层字段不符合 schema v1")
        )
    if isinstance(policy, Mapping) and policy.get("schema_version") != 1:
        violations.append(
            _violation("invalid_schema", "policy.schema_version 必须为 1")
        )
    if not isinstance(policy, Mapping) or policy.get("scope") != EVENT_CONSUMER_POLICY_SCOPE:
        violations.append(
            _violation("scope_mismatch", "policy.scope 必须精确匹配宿主扫描范围")
        )

    policy_section = policy.get("event_consumers") if isinstance(policy, Mapping) else None
    if not isinstance(policy_section, Mapping) or set(policy_section) != {
        "match_mode",
        "entries",
    }:
        violations.append(
            _violation("invalid_schema", "policy.event_consumers 字段不完整")
        )
        entries: list[object] = []
    else:
        if policy_section.get("match_mode") != "exact_fingerprint_set":
            violations.append(
                _violation(
                    "invalid_schema",
                    "policy.event_consumers.match_mode 必须为 exact_fingerprint_set",
                )
            )
        raw_entries = policy_section.get("entries")
        if not isinstance(raw_entries, list):
            violations.append(
                _violation("invalid_schema", "policy.event_consumers.entries 必须是列表")
            )
            entries = []
        else:
            entries = raw_entries

    valid_entries: list[Mapping[str, object]] = []
    for index, entry in enumerate(entries):
        entry_errors = _validate_policy_entry(entry, index=index)
        violations.extend(entry_errors)
        if isinstance(entry, Mapping) and not any(
            error.code == "invalid_entry" for error in entry_errors
        ):
            violations.extend(
                _fingerprint_errors(entry, source=f"policy.entries[{index}]")
            )
            valid_entries.append(entry)

    violations.extend(
        _duplicate_fingerprint_errors(
            valid_actual,
            code="duplicate_fact_fingerprint",
            source="collector facts",
        )
    )
    violations.extend(
        _duplicate_fingerprint_errors(
            valid_entries,
            code="duplicate_policy_fingerprint",
            source="policy entries",
        )
    )

    actual_by_fingerprint = {
        str(fact["fingerprint"]): fact
        for fact in valid_actual
        if isinstance(fact.get("fingerprint"), str)
    }
    policy_by_fingerprint = {
        str(entry["fingerprint"]): entry
        for entry in valid_entries
        if isinstance(entry.get("fingerprint"), str)
    }
    for fingerprint in sorted(actual_by_fingerprint.keys() - policy_by_fingerprint.keys()):
        violations.append(
            _violation(
                "unreviewed_fact",
                "当前 consumer 事实未经过人工政策审查",
                fingerprint,
            )
        )
    for fingerprint in sorted(policy_by_fingerprint.keys() - actual_by_fingerprint.keys()):
        violations.append(
            _violation(
                "stale_policy",
                "人工政策引用的 consumer 事实已经消失或被替换",
                fingerprint,
            )
        )
    for fingerprint in sorted(actual_by_fingerprint.keys() & policy_by_fingerprint.keys()):
        if _fact_projection(actual_by_fingerprint[fingerprint]) != _fact_projection(
            policy_by_fingerprint[fingerprint]
        ):
            violations.append(
                _violation(
                    "fingerprint_mismatch",
                    "相同 fingerprint 对应的 consumer identity 不一致",
                    fingerprint,
                )
            )

    static_count = sum(fact.get("dynamic") is False for fact in actual_facts)
    dynamic_count = sum(fact.get("dynamic") is True for fact in actual_facts)
    invalid_count = sum(fact.get("invalid") is True for fact in actual_facts)
    return EventPolicyCheck(
        actual_count=len(actual_facts),
        reviewed_count=len(entries),
        static_count=static_count,
        dynamic_count=dynamic_count,
        invalid_count=invalid_count,
        violations=_sort_violations(violations),
    )


def check_event_consumer_policy(
    facts: Sequence[Mapping[str, object]],
    policy_path: Path = DEFAULT_EVENT_POLICY_PATH,
) -> EventPolicyCheck:
    """读取人工 policy 文件并校验给定的当前 consumer facts。"""
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        facts_list = list(facts)
        return EventPolicyCheck(
            actual_count=len(facts_list),
            reviewed_count=0,
            static_count=sum(fact.get("dynamic") is False for fact in facts_list),
            dynamic_count=sum(fact.get("dynamic") is True for fact in facts_list),
            invalid_count=sum(fact.get("invalid") is True for fact in facts_list),
            violations=(
                _violation(
                    "invalid_policy_file",
                    f"无法读取 Event consumer policy：{error}",
                ),
            ),
        )
    if not isinstance(policy, Mapping):
        policy = {}
    return validate_event_consumer_policy(policy, facts)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """解析只读 Event policy 检查参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--policy",
        type=Path,
        default=DEFAULT_EVENT_POLICY_PATH,
        help="人工 Event consumer policy 路径",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """收集当前事实并只读检查人工政策，不提供任何写入入口。"""
    args = parse_args(argv)
    try:
        from scripts.architecture.baseline import collect_current_event_facts
    except ImportError:
        from baseline import collect_current_event_facts

    current = collect_current_event_facts()
    consumers: Any = current.get("consumers") if isinstance(current, Mapping) else None
    if not isinstance(consumers, list):
        print("当前 Event facts 缺少 consumers 列表", file=sys.stderr)
        return 1
    result = check_event_consumer_policy(consumers, args.policy)
    if result.ok:
        print(
            "Event consumer policy 通过："
            f"{result.actual_count} 条事实，"
            f"{result.static_count} 条静态注册，"
            f"{result.dynamic_count} 条动态例外"
        )
        return 0
    for violation in result.violations:
        suffix = f" [{violation.fingerprint}]" if violation.fingerprint else ""
        print(f"{violation.code}{suffix}: {violation.detail}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
