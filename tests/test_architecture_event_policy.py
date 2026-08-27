"""宿主 Event consumer 人工政策测试。"""

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

import pytest

from scripts.architecture.baseline import collect_current_event_facts
from scripts.architecture.event_facts import fingerprint_event_fact
from scripts.architecture.event_policy import (
    DEFAULT_EVENT_POLICY_PATH,
    DYNAMIC_CLASSIFICATION,
    EVENT_CONSUMER_POLICY_SCOPE,
    STATIC_CLASSIFICATION,
    EventPolicyCheck,
    check_event_consumer_policy,
    parse_args,
    validate_event_consumer_policy,
)


def _fact(
    *,
    caller: str = "app.sample",
    events: list[str] | None = None,
    dynamic: bool = False,
    invalid: bool = False,
    handler: str = "Sample.handle",
    line: int = 10,
) -> dict[str, Any]:
    """构造一条与统一 collector 相同字段的 consumer fact。"""
    fact: dict[str, Any] = {
        "caller": caller,
        "line": line,
        "qualname": "Sample",
        "method": "register",
        "receiver_kind": "canonical_instance",
        "events": events if events is not None else ["EventType.Alpha"],
        "dynamic": dynamic,
        "invalid": invalid,
        "handler": handler,
        "registration_kind": "decorator",
        "priority": "<default>",
    }
    fact["fingerprint"] = fingerprint_event_fact(fact)
    return fact


def _entry(
    fact: dict[str, Any],
    *,
    classification: str | None = None,
    owner: str | None = None,
    reason: str = "宿主显式注册并拥有该事件处理器。",
) -> dict[str, Any]:
    """把 collector fact 转换为完整 line-free policy entry。"""
    entry = {key: value for key, value in fact.items() if key != "line"}
    entry.update({
        "classification": classification
        or (
            DYNAMIC_CLASSIFICATION
            if fact["dynamic"] is True
            else STATIC_CLASSIFICATION
        ),
        "owner": owner or str(fact["caller"]),
        "reason": reason,
    })
    return entry


def _policy(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """构造 exact fingerprint set policy。"""
    return {
        "schema_version": 1,
        "scope": dict(EVENT_CONSUMER_POLICY_SCOPE),
        "event_consumers": {
            "match_mode": "exact_fingerprint_set",
            "entries": entries,
        },
    }


def _codes(result: EventPolicyCheck) -> list[str]:
    """返回校验结果中的稳定错误码列表。"""
    return [violation.code for violation in result.violations]


def test_event_policy_accepts_exact_static_and_dynamic_facts() -> None:
    """静态注册和明确动态例外应按完整 fingerprint 精确匹配。"""
    static = _fact()
    dynamic = _fact(
        caller="app.workflow",
        events=[],
        dynamic=True,
        handler="self._handle_event",
        line=20,
    )

    result = validate_event_consumer_policy(
        _policy([_entry(static), _entry(dynamic)]),
        [static, dynamic],
    )

    assert result.ok
    assert result.actual_count == result.reviewed_count == 2
    assert result.static_count == 1
    assert result.dynamic_count == 1
    assert result.invalid_count == 0


def test_current_event_consumer_policy_matches_exact_reviewed_set() -> None:
    """当前 17 条宿主 consumer 必须逐条匹配人工政策且没有非法成员。"""
    facts = collect_current_event_facts()["consumers"]
    policy = json.loads(DEFAULT_EVENT_POLICY_PATH.read_text(encoding="utf-8"))
    entries = policy["event_consumers"]["entries"]

    result = check_event_consumer_policy(facts)

    assert result.ok
    assert result.actual_count == result.reviewed_count == 17
    assert result.static_count == 16
    assert result.dynamic_count == 1
    assert result.invalid_count == 0
    assert len({fact["fingerprint"] for fact in facts}) == 17
    assert {entry["fingerprint"] for entry in entries} == {
        fact["fingerprint"] for fact in facts
    }
    assert sum(
        entry["classification"] == STATIC_CLASSIFICATION for entry in entries
    ) == 16
    dynamic_entries = [
        entry
        for entry in entries
        if entry["classification"] == DYNAMIC_CLASSIFICATION
    ]
    assert dynamic_entries == [{
        "caller": "app.workflow",
        "qualname": "WorkflowManager.register_workflow_event",
        "method": "add_event_listener",
        "receiver_kind": "canonical_singleton",
        "events": [],
        "dynamic": True,
        "invalid": False,
        "handler": "self._handle_event",
        "registration_kind": "listener",
        "priority": "<default>",
        "fingerprint": (
            "b99f557080a8ddb6dc9d2870d02c4cabc4b265b0daac086369c3bf1d51073c09"
        ),
        "classification": DYNAMIC_CLASSIFICATION,
        "owner": "app.workflow",
        "reason": "工作流配置在运行期决定事件类型，receiver 与 handler 仍可静态证明。",
    }]
    assert all("line" not in entry for entry in entries)


def test_event_policy_reuses_line_free_event_fact_fingerprint() -> None:
    """源码行移动不改变摘要，任一 line-free 语义字段变化都改变摘要。"""
    fact = _fact()
    moved = {**fact, "line": 999}
    assert fingerprint_event_fact(fact) == fingerprint_event_fact(moved)

    for field, value in {
        "caller": "app.other",
        "qualname": "Other",
        "method": "add_event_listener",
        "receiver_kind": "constructed_instance",
        "events": ["EventType.Beta"],
        "dynamic": True,
        "invalid": True,
        "handler": "Other.handle",
        "registration_kind": "listener",
        "priority": "10",
    }.items():
        changed = {**fact, field: value}
        assert fingerprint_event_fact(changed) != fact["fingerprint"], field


def test_event_policy_rejects_add_remove_and_same_count_replacement() -> None:
    """新增、陈旧和同计数替换都不能通过刷新事实集合隐藏。"""
    original = _fact()
    added = _fact(handler="Sample.added", line=20)
    policy = _policy([_entry(original)])

    assert _codes(validate_event_consumer_policy(policy, [original])) == []
    assert "unreviewed_fact" in _codes(
        validate_event_consumer_policy(policy, [original, added])
    )
    assert _codes(validate_event_consumer_policy(policy, [])) == ["stale_policy"]
    assert _codes(validate_event_consumer_policy(policy, [added])) == [
        "stale_policy",
        "unreviewed_fact",
    ]


def test_event_policy_rejects_classification_and_owner_swaps() -> None:
    """事实仍相同时也不得互换静态/动态分类或责任 owner。"""
    fact = _fact()
    classification_swap = _entry(fact, classification=DYNAMIC_CLASSIFICATION)
    owner_swap = _entry(fact, owner="app.other")

    assert "classification_mismatch" in _codes(
        validate_event_consumer_policy(_policy([classification_swap]), [fact])
    )
    assert "owner_mismatch" in _codes(
        validate_event_consumer_policy(_policy([owner_swap]), [fact])
    )


def test_event_policy_rejects_invalid_schema_wildcards_and_reasons() -> None:
    """人工 policy 必须保持精确 scope、字段和可审查理由。"""
    def schema_drift(policy: dict[str, Any]) -> None:
        policy["schema_version"] = 2

    def scope_drift(policy: dict[str, Any]) -> None:
        policy["scope"]["root"] = "app.chain"

    def wildcard_handler(policy: dict[str, Any]) -> None:
        policy["event_consumers"]["entries"][0]["handler"] = "Sample.*"

    def empty_reason(policy: dict[str, Any]) -> None:
        policy["event_consumers"]["entries"][0]["reason"] = " "

    def placeholder_reason(policy: dict[str, Any]) -> None:
        policy["event_consumers"]["entries"][0]["reason"] = "TODO"

    mutations: tuple[tuple[Callable[[dict[str, Any]], None], str], ...] = (
        (schema_drift, "invalid_schema"),
        (scope_drift, "scope_mismatch"),
        (wildcard_handler, "wildcard_entry"),
        (empty_reason, "empty_reason"),
        (placeholder_reason, "empty_reason"),
    )
    fact = _fact()
    for mutate, expected_code in mutations:
        policy = _policy([_entry(fact)])
        mutate(policy)
        assert expected_code in _codes(
            validate_event_consumer_policy(policy, [fact])
        )


def test_event_policy_rejects_duplicate_policy_and_fact_fingerprints() -> None:
    """重复 identity 不能被 exact set 的字典或集合投影吞掉。"""
    fact = _fact()
    entry = _entry(fact)

    policy_result = validate_event_consumer_policy(
        _policy([entry, deepcopy(entry)]),
        [fact],
    )
    fact_result = validate_event_consumer_policy(
        _policy([entry]),
        [fact, deepcopy(fact)],
    )

    assert "duplicate_policy_fingerprint" in _codes(policy_result)
    assert "duplicate_fact_fingerprint" in _codes(fact_result)


def test_event_policy_rejects_invalid_and_plugin_consumers() -> None:
    """非法事件成员与 app.plugins 副本均不得写入人工准入。"""
    invalid = _fact(invalid=True)
    plugin = _fact(caller="app.plugins.demo")

    invalid_result = validate_event_consumer_policy(
        _policy([_entry(invalid)]),
        [invalid],
    )
    plugin_result = validate_event_consumer_policy(
        _policy([_entry(plugin)]),
        [plugin],
    )

    assert "invalid_consumer" in _codes(invalid_result)
    assert "plugin_scope_violation" in _codes(plugin_result)


def test_event_policy_rejects_tampered_fingerprint() -> None:
    """policy 不得保留旧摘要同时篡改 receiver、method 或其他事实字段。"""
    fact = _fact()
    entry = _entry(fact)
    entry["receiver_kind"] = "constructed_instance"

    result = validate_event_consumer_policy(_policy([entry]), [fact])

    assert "fingerprint_mismatch" in _codes(result)


def test_event_policy_file_check_reports_invalid_json(tmp_path: Path) -> None:
    """只读文件入口把缺失或损坏 policy 报告为结构化失败。"""
    path = tmp_path / "runtime-contract-policy.json"
    path.write_text("{", encoding="utf-8")

    result = check_event_consumer_policy([_fact()], path)

    assert not result.ok
    assert _codes(result) == ["invalid_policy_file"]


def test_event_policy_cli_has_no_write_mode() -> None:
    """人工 policy CLI 永远不暴露自动写入入口。"""
    with pytest.raises(SystemExit) as error:
        parse_args(["--write"])

    assert error.value.code == 2


def test_event_policy_violation_order_is_deterministic() -> None:
    """CI 输出必须按错误码和 fingerprint 稳定排序。"""
    first = _fact(handler="Sample.first")
    second = _fact(handler="Sample.second")

    result = validate_event_consumer_policy(_policy([]), [second, first])

    assert result.violations == tuple(
        sorted(
            result.violations,
            key=lambda item: (item.code, item.fingerprint or "", item.detail),
        )
    )
