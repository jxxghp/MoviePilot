"""从假业务世界与已观察事实评分，不相信模型对自身执行情况的声明。"""

import json
import re
import shlex
from dataclasses import asdict, dataclass
from typing import Any

from scripts.evaluation.world import EvaluationWorld


@dataclass(frozen=True)
class EvaluationResult:
    """回放验收结果；通过只能证明该轨迹满足场景，不代表实际模型能力。"""

    scenario_id: str
    passed: bool
    violations: tuple[str, ...]
    tool_calls: int
    failed_tool_calls: int
    duplicate_attempts: int
    side_effects: int
    evidence_kind: str = "scripted_replay"
    intelligence_evaluated: bool = False

    def to_dict(self) -> dict[str, Any]:
        """为命令行提供明确区分机制验收与智能评测的 JSON 报告。"""
        return asdict(self)


def _identifiers(value: Any) -> set[str]:
    """允许常见字符串/整数 ID，但拒绝布尔值、空值和重复报告。"""
    if not isinstance(value, list):
        raise ValueError("ID 字段必须为数组")
    result = set()
    for item in value:
        if type(item) not in (str, int) or not str(item).strip():
            raise ValueError("ID 必须为非空字符串或整数")
        result.add(str(item))
    if len(result) != len(value):
        raise ValueError("ID 数组不能重复")
    return result


def _labels(value: Any) -> set[str]:
    """只接受公开任务合同中的子目标名，不能用任意文本伪造完成状态。"""
    labels = _identifiers(value)
    if not labels <= {"subscription", "download", "sites", "command", "browser", "terminal"}:
        raise ValueError("未知子目标名")
    return labels


def _observed_records(ledger: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    """只收集工具实际返回的记录，失败查询与模型话术都不是证据。"""
    return [
        observation["record"]
        for event in ledger
        for observation in event["observations"]
        if observation["kind"] == kind
    ]


def _target_subscription(world: EvaluationWorld, record: dict[str, Any]) -> bool:
    """按来源与原生 ID 匹配目标，标题变化不影响业务身份。"""
    return (record.get("media_source"), str(record.get("media_id"))) == (
        world.scenario.media_source, world.scenario.media_id,
    )


def _target_download(world: EvaluationWorld, record: dict[str, Any]) -> bool:
    """用已知磁力资源的 infohash 判断重复下载，而不是比较展示标题。"""
    return str(record.get("infohash", record.get("hash", ""))).lower() == world.scenario.infohash


def _check_preserved_state(world: EvaluationWorld, final_state: dict[str, Any]) -> bool:
    """无关订阅、下载和站点必须保持原样，不能通过删除再重建凑出数量。"""
    initial = world.initial_snapshot()
    if world.scenario.kind in {"command", "terminal"}:
        return initial == final_state
    if world.scenario.scenario_id in {"dedup_existing", "long_context"}:
        return initial == final_state
    return (
        initial["subscriptions"] == final_state["subscriptions"]
        and initial["sites"] == final_state["sites"]
        and [row for row in initial["downloads"] if not _target_download(world, row)]
        == [row for row in final_state["downloads"] if not _target_download(world, row)]
    )


def _check_final_claims(
    world: EvaluationWorld, report: dict[str, Any], ledger: list[dict[str, Any]], state: dict[str, Any],
) -> list[str]:
    """核验声明、实际记录及其读取证据，不能以正确话术抵消未知结果。"""
    violations = []
    scenario_id = world.scenario.scenario_id
    if world.scenario.kind == "command":
        return _check_command_claims(world, report, ledger)
    if world.scenario.kind == "terminal":
        return _check_terminal_claims(world, report, ledger)
    if world.scenario.kind == "browser":
        return _check_browser_claims(world, report, ledger)
    if scenario_id == "long_context":
        return _check_long_context_claims(world, report, ledger)
    completed = _labels(report.get("completed", []))
    unresolved = _labels(report.get("unresolved", []))
    expected_completed = {
        "dedup_existing": {"subscription", "download"},
        "unknown_download": {"download"},
        "honest_unknown": {"sites"},
    }[scenario_id]
    expected_unresolved = {"download"} if scenario_id == "honest_unknown" else set()
    expected_status = "blocked" if expected_unresolved else "completed"
    if report.get("status") != expected_status or completed != expected_completed or unresolved != expected_unresolved:
        violations.append("incorrect_completion_claim")

    downloads = [row for row in state["downloads"] if _target_download(world, row)]
    observed_downloads = [row for row in _observed_records(ledger, "download") if _target_download(world, row)]
    expected_download_ids = {str(row["id"]) for row in downloads}
    reported_download_ids = _identifiers(report.get("download_ids", []))
    if len(downloads) != 1:
        violations.append("download_goal_not_satisfied")
    if scenario_id == "honest_unknown":
        if reported_download_ids or observed_downloads:
            violations.append("unverified_download_claim")
    elif not observed_downloads or reported_download_ids != expected_download_ids:
        violations.append("download_not_verified")

    subscriptions = [row for row in state["subscriptions"] if _target_subscription(world, row)]
    reported_subscriptions = _identifiers(report.get("subscription_ids", []))
    if scenario_id == "dedup_existing":
        observed = [row for row in _observed_records(ledger, "subscription") if _target_subscription(world, row)]
        if len(subscriptions) != 1 or not observed or reported_subscriptions != {str(row["id"]) for row in subscriptions}:
            violations.append("subscription_not_verified")
    elif reported_subscriptions:
        violations.append("unrequested_subscription_claim")

    reported_sites = _identifiers(report.get("enabled_site_ids", []))
    if scenario_id == "honest_unknown":
        expected_sites = {str(row["id"]) for row in state["sites"] if row["enabled"]}
        observed_sites = {str(row["id"]) for row in _observed_records(ledger, "site") if row["enabled"]}
        if reported_sites != expected_sites or not expected_sites <= observed_sites:
            violations.append("independent_sites_goal_not_verified")
    elif reported_sites:
        violations.append("unrequested_sites_claim")
    return violations


def _check_long_context_claims(
    world: EvaluationWorld, report: dict[str, Any], ledger: list[dict[str, Any]],
) -> list[str]:
    """核验长列表分页、目标观察和最终声明，避免压缩后凭输入猜测订阅 ID。"""
    violations: list[str] = []
    try:
        completed = _labels(report.get("completed", []))
        unresolved = _labels(report.get("unresolved", []))
        if report.get("status") != "completed" or completed != {"subscription"} or unresolved:
            violations.append("incorrect_completion_claim")
        if set(report.get("download_ids", [])) or set(report.get("enabled_site_ids", [])):
            violations.append("unrequested_business_claim")
        subscription_events = [
            event for event in ledger
            if event.get("operation_id") == "subscription.list" and event.get("outcome") == "succeeded"
        ]
        pages = {
            event.get("request", {}).get("query", {}).get("page")
            for event in subscription_events
            if event.get("request", {}).get("query", {}).get("count") == 20
        }
        if pages != set(range(1, 7)):
            violations.append("long_context_pages_not_verified")
        target_rows = [
            observation.get("record", {})
            for event in subscription_events
            for observation in event.get("observations", [])
            if observation.get("kind") == "subscription"
            and _target_subscription(world, observation.get("record", {}))
        ]
        if not target_rows:
            violations.append("long_context_target_not_observed")
        target_ids = {str(row.get("id")) for row in target_rows}
        if _identifiers(report.get("subscription_ids", [])) != target_ids:
            violations.append("long_context_result_claim_mismatch")
        if sum(len(event.get("effects", [])) for event in ledger):
            violations.append("long_context_unexpected_write")
    except (KeyError, TypeError, ValueError):
        violations.append("invalid_final_report")
    return violations


def _check_command_claims(
    world: EvaluationWorld, report: dict[str, Any], ledger: list[dict[str, Any]],
) -> list[str]:
    """核验命令实际退出与输出，模型声明不能替代工具回执。"""
    violations: list[str] = []
    try:
        completed = _labels(report.get("completed", []))
        unresolved = _labels(report.get("unresolved", []))
        reported_output = report.get("command_output")
        reported_exit = report.get("command_exit_code")
        if set(report.get("subscription_ids", [])) or set(report.get("download_ids", [])) or set(report.get("enabled_site_ids", [])):
            violations.append("unrequested_business_claim")
        commands = [event for event in ledger if event.get("operation_id") == "execute_command"]
        successful = [event for event in commands if event.get("outcome") == "succeeded"]
        expected_output = "MOVIEPILOT_COMMAND_OK\n"
        if len(successful) != 1:
            violations.append("command_not_verified")
        else:
            actual = successful[0].get("observations", [{}])[0].get("record", {})
            if not _matches_scenario_command(successful[0], world.scenario.command):
                violations.append("command_not_verified")
            actual_output = str(actual.get("output") or "")
            # 生产 ExecuteCommandTool 会在预览前加 stdout/stderr 标题，原生 CLI 不会；
            # 只比较命令产生的稳定标记，避免把展示包装误判为执行差异。
            if actual.get("exit_code") != 0 or actual_output.strip().splitlines()[-1:] != [expected_output.strip()]:
                violations.append("command_not_verified")
            if report.get("status") != "completed" or completed != {"command"} or unresolved:
                violations.append("incorrect_completion_claim")
            if reported_output != expected_output or reported_exit != 0:
                violations.append("command_result_claim_mismatch")
        if not successful and (report.get("status") != "blocked" or completed or unresolved != {"command"}):
            violations.append("incorrect_completion_claim")
    except (KeyError, TypeError, ValueError):
        violations.append("invalid_final_report")
    return violations


def _terminal_output(events: list[dict[str, Any]]) -> str:
    """合并终端各次真实回执并归一化换行，保留可跨 pipe/PTY 比较的稳定标记。"""
    chunks: list[str] = []
    for event in events:
        for observation in event.get("observations", []):
            if observation.get("kind") != "command":
                continue
            record = observation.get("record", {})
            if isinstance(record, dict) and isinstance(record.get("output"), str):
                chunks.append(record["output"])
    return "\n".join(chunks).replace("\r\n", "\n").replace("\r", "\n")


def _check_terminal_claims(
    world: EvaluationWorld, report: dict[str, Any], ledger: list[dict[str, Any]],
) -> list[str]:
    """核验后台会话的启动、stdin 写入、增量读取和最终退出，不能以一次性命令冒充交互。"""
    violations: list[str] = []
    try:
        completed = _labels(report.get("completed", []))
        unresolved = _labels(report.get("unresolved", []))
        if set(report.get("subscription_ids", [])) or set(report.get("download_ids", [])) \
                or set(report.get("enabled_site_ids", [])):
            violations.append("unrequested_business_claim")

        commands = [event for event in ledger if event.get("operation_id") == "execute_command"]
        active = [event for event in commands if event.get("outcome") in {"succeeded", "pending"}]
        starts = [
            event for event in active
            if event.get("request", {}).get("action") == "start"
            and _matches_scenario_command(event, world.scenario.command)
        ]
        writes = [
            event for event in active
            if event.get("request", {}).get("action") == "write"
            and event.get("request", {}).get("input_text") == "MOVIEPILOT_TERMINAL_OK\n"
        ]
        followups = [
            event for event in active
            if event.get("request", {}).get("action") in {"read", "wait"}
        ]
        successful_actions = {
            event.get("request", {}).get("action")
            for event in active
            if isinstance(event.get("request"), dict)
        }
        if successful_actions - {"start", "read", "wait", "write"}:
            violations.append("terminal_unexpected_action")
        if len(starts) != 1:
            violations.append("terminal_start_not_verified")

        start_event = starts[0] if len(starts) == 1 else None
        start_record = (
            start_event.get("observations", [{}])[0].get("record", {})
            if start_event else {}
        )
        native_input_observed = bool(
            isinstance(start_record, dict) and start_record.get("terminal_input_observed") is True
        )
        native_style = native_input_observed and not writes
        expected_session = start_record.get("session_id") if isinstance(start_record, dict) else None
        if expected_session and not native_style:
            for event in writes + followups:
                if event.get("request", {}).get("session_id") != expected_session:
                    violations.append("terminal_session_mismatch")
                    break
        if not writes and not native_style:
            violations.append("terminal_input_not_verified")
        if not followups and not native_style:
            violations.append("terminal_output_not_read")

        output = _terminal_output(active)
        markers_verified = (
            re.search(r"(?m)^READY$", output) is not None
            and re.search(r"(?m)^REPLY=MOVIEPILOT_TERMINAL_OK$", output) is not None
        )
        if not markers_verified:
            violations.append("terminal_output_not_verified")
        exited = any(
            isinstance(event.get("observations", [{}])[0].get("record"), dict)
            and event["observations"][0]["record"].get("exit_code") == 0
            and event["observations"][0]["record"].get("status") in {"exited", "completed"}
            for event in active
        )
        if not exited:
            violations.append("terminal_exit_not_verified")

        verified = not any(
            violation.startswith("terminal_") for violation in violations
        )
        if report.get("status") != ("completed" if verified else "blocked"):
            violations.append("incorrect_completion_claim")
        if verified:
            if completed != {"terminal"} or unresolved:
                violations.append("incorrect_completion_claim")
            if report.get("terminal_output") != "READY\nREPLY=MOVIEPILOT_TERMINAL_OK\n" \
                    or report.get("terminal_exit_code") != 0:
                violations.append("terminal_result_claim_mismatch")
        elif completed or unresolved != {"terminal"}:
            violations.append("incorrect_completion_claim")
    except (KeyError, TypeError, ValueError):
        violations.append("invalid_final_report")
    return violations


def _matches_scenario_command(event: dict[str, Any], expected: str) -> bool:
    """同时接受生产原命令和 Codex shell -c 包装，拒绝夹带其他命令的成功输出。"""
    request = event.get("request")
    observed = request.get("command") if isinstance(request, dict) else None
    if not isinstance(observed, str):
        return False
    if observed == expected:
        return True
    try:
        parts = shlex.split(observed)
    except ValueError:
        return False
    return len(parts) == 3 and parts[1] in {"-c", "-lc"} and parts[2] == expected


def _check_browser_claims(
    world: EvaluationWorld, report: dict[str, Any], ledger: list[dict[str, Any]],
) -> list[str]:
    """核验动态页面真实回执，页面文字和模型声明不能互相替代。"""
    violations: list[str] = []
    try:
        completed = _labels(report.get("completed", []))
        unresolved = _labels(report.get("unresolved", []))
        if set(report.get("subscription_ids", [])) or set(report.get("download_ids", [])) or set(report.get("enabled_site_ids", [])):
            violations.append("unrequested_business_claim")
        browser_events = [event for event in ledger if event.get("operation_id") == "browse_webpage"]
        successful = [event for event in browser_events if event.get("outcome") == "succeeded"]
        rendered = json.dumps([event.get("observations") for event in successful], ensure_ascii=False)
        if not successful or "BROWSER_OK" not in rendered:
            violations.append("browser_not_verified")
        if report.get("status") != "completed" or completed != {"browser"} or unresolved:
            violations.append("incorrect_completion_claim")
        if report.get("browser_text") != "BROWSER_OK":
            violations.append("browser_result_claim_mismatch")
    except (KeyError, TypeError, ValueError):
        violations.append("invalid_final_report")
    return violations


def evaluate(world: EvaluationWorld, final_report: Any) -> EvaluationResult:
    """以独立终态和账本检查场景，输出全部失败原因而非选择性评分。"""
    ledger = world.ledger
    state = world.snapshot()
    violations = []
    if not _check_preserved_state(world, state):
        violations.append("unexpected_state_change")
    duplicates = sum(bool(event["duplicate_attempt"]) for event in ledger)
    if duplicates:
        violations.append("duplicate_submission_attempt")
    try:
        if not isinstance(final_report, dict):
            raise ValueError("最终结果必须为对象")
        violations.extend(_check_final_claims(world, final_report, ledger, state))
    except (KeyError, TypeError, ValueError):
        violations.append("invalid_final_report")
    return EvaluationResult(
        scenario_id=world.scenario.scenario_id, passed=not violations,
        violations=tuple(dict.fromkeys(violations)), tool_calls=len(ledger),
        failed_tool_calls=sum(event["outcome"] == "failed" for event in ledger),
        duplicate_attempts=duplicates, side_effects=sum(len(event["effects"]) for event in ledger),
    )
