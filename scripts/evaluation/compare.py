"""校验 MoviePilot 与原生 Codex 的同条件评测报告并生成成对摘要。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MAX_REPORT_BYTES = 8 * 1024 * 1024
_MODEL_CONDITION_FIELDS = (
    "requested_model", "reasoning_effort", "max_model_calls", "max_output_tokens",
    "timeout_seconds", "harness_context_window", "auth_mode",
)
_EVIDENCE_KINDS = {
    "moviepilot_live_model": "moviepilot",
    "codex_native_controlled": "codex",
}


def _read_report(path: Path) -> dict[str, Any]:
    """读取有限大小的 JSON 报告，拒绝目录、超限或非对象正文。"""
    if not path.is_file():
        raise ValueError(f"评测报告不存在：{path}")
    if path.stat().st_size > MAX_REPORT_BYTES:
        raise ValueError("评测报告超过 8 MiB")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError(f"评测报告不是有效 JSON：{path}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"评测报告必须是 JSON 对象：{path}")
    return payload


def _conditions(report: dict[str, Any]) -> dict[str, Any]:
    """提取必须一致的模型条件，不把令牌或完整工具正文带入比较结果。"""
    model = report.get("model")
    if not isinstance(model, dict):
        raise ValueError("评测报告缺少模型条件")
    return {field: model.get(field) for field in _MODEL_CONDITION_FIELDS}


def _metric(report: dict[str, Any], key: str) -> Any:
    """读取报告中的公开轨迹指标，未知用量保持 None 而不是补零。"""
    if key == "total_tokens":
        tokens = report.get("tokens")
        return tokens.get("total_tokens") if isinstance(tokens, dict) else None
    return report.get(key)


def _side(report: dict[str, Any], role: str) -> dict[str, Any]:
    """生成不含最终正文、工具参数和凭据的单侧摘要。"""
    return {
        "role": role,
        "passed": report.get("passed"),
        "violations": report.get("violations", []),
        "intelligence_evaluated": report.get("intelligence_evaluated"),
        "usage_complete": report.get("usage_complete"),
        "model_calls": _metric(report, "model_calls"),
        "completed_model_calls": _metric(report, "completed_model_calls"),
        "failed_tool_calls": _metric(report, "failed_tool_calls"),
        "tool_calls": _metric(report, "tool_calls"),
        "duplicate_attempts": _metric(report, "duplicate_attempts"),
        "side_effects": _metric(report, "side_effects"),
        "elapsed_seconds": _metric(report, "elapsed_seconds"),
        "total_tokens": _metric(report, "total_tokens"),
    }


def _delta(moviepilot: dict[str, Any], codex: dict[str, Any], *, usage_comparable: bool = True) -> dict[str, Any]:
    """计算两个方向的数值差；任一侧未知时保留 unknown。"""
    result: dict[str, Any] = {}
    for key in ("model_calls", "completed_model_calls", "failed_tool_calls", "tool_calls",
                "duplicate_attempts", "side_effects", "elapsed_seconds", "total_tokens"):
        if key == "total_tokens" and not usage_comparable:
            result[key] = None
            continue
        left, right = _metric(moviepilot, key), _metric(codex, key)
        result[key] = None if not isinstance(left, (int, float)) or not isinstance(right, (int, float)) else left - right
    return result


def _has_cancel_action(report: dict[str, Any]) -> bool:
    """确认取消场景确实发出了子代理取消动作，而不是凭场景名放宽用量门禁。"""
    trace = report.get("agent_trace") if report.get("evidence_kind") == "moviepilot_live_model" else report.get("native_events")
    if not isinstance(trace, list):
        return False
    for entry in trace:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "ai":
            data = entry.get("data")
            calls = data.get("tool_calls", []) if isinstance(data, dict) else []
            if any(
                isinstance(call, dict)
                and str(call.get("name", "")).rsplit(".", 1)[-1] == "subagent_task"
                and isinstance(call.get("args"), dict)
                and str(call["args"].get("action", "")).strip().lower() == "cancel"
                for call in calls
            ):
                return True
        if entry.get("type") not in {"item.started", "item.completed"}:
            continue
        item = entry.get("item")
        if isinstance(item, dict) and str(item.get("tool", "")).rsplit(".", 1)[-1] in {
            "close_agent", "interrupt_agent", "cancel",
        }:
            return True
    return False


def _intentional_cancel_usage(report: dict[str, Any]) -> bool:
    """确认取消场景的执行质量，并约束未完整用量只能来自一个取消请求。"""
    if report.get("scenario_id") != "subagent_cancel_recovery":
        return False
    calls = report.get("model_calls")
    completed = report.get("completed_model_calls")
    execution_completed = (
        report.get("agent_execution_success") is True
        if report.get("evidence_kind") == "moviepilot_live_model"
        else report.get("native_turn_completed") is True and report.get("native_exit_code") == 0
    )
    return (
        (
            report.get("usage_complete") is True
            or (type(calls) is int and type(completed) is int and calls - completed == 1)
        )
        and report.get("intelligence_evaluated") is True
        and execution_completed
        and report.get("runner_error_type") is None
        and report.get("failed_tool_calls") == 0
        and _has_cancel_action(report)
    )


def compare_reports(moviepilot: dict[str, Any], codex: dict[str, Any]) -> dict[str, Any]:
    """仅在同场景、同源码和同模型预算成立时生成 Codex 成对比较摘要。"""
    roles = {}
    for report in (moviepilot, codex):
        evidence_kind = report.get("evidence_kind")
        role = _EVIDENCE_KINDS.get(evidence_kind)
        if role is None:
            raise ValueError("成对比较需要 MoviePilot live 与 Codex native 报告")
        if role in roles:
            raise ValueError("成对比较不能重复使用同一侧报告")
        roles[role] = report
    moviepilot, codex = roles["moviepilot"], roles["codex"]

    for field in ("scenario_id", "scenario_sha256", "production_agent_sha256", "skills_sha256", "harness_sha256"):
        if moviepilot.get(field) != codex.get(field):
            raise ValueError(f"成对报告的 {field} 不一致")
    moviepilot_conditions, codex_conditions = _conditions(moviepilot), _conditions(codex)
    if moviepilot_conditions != codex_conditions:
        raise ValueError("成对报告的模型、推理档位或预算不一致")
    complete_usage = all(report.get("usage_complete") for report in (moviepilot, codex))
    intentional_cancel_usage = (
        moviepilot["scenario_id"] == "subagent_cancel_recovery"
        and not complete_usage
        and all(_intentional_cancel_usage(report) for report in (moviepilot, codex))
    )
    if not all(report.get("intelligence_evaluated") for report in (moviepilot, codex)):
        raise ValueError("成对比较需要两侧都完成真实模型调用")
    if not complete_usage and not intentional_cancel_usage:
        raise ValueError("成对比较需要两侧都取得完整用量；主动取消场景只能按受限行为配对")

    moviepilot_side, codex_side = _side(moviepilot, "moviepilot"), _side(codex, "codex")
    return {
        "schema_version": 1,
        "codex_comparison": True,
        "pair_valid": True,
        "both_passed": bool(moviepilot.get("passed") and codex.get("passed")),
        "scenario_id": moviepilot["scenario_id"],
        "scenario_sha256": moviepilot["scenario_sha256"],
        "production_agent_sha256": moviepilot["production_agent_sha256"],
        "skills_sha256": moviepilot["skills_sha256"],
        "harness_sha256": moviepilot["harness_sha256"],
        "usage_comparable": complete_usage,
        "usage_note": (
            None if complete_usage else
            "主动取消使至少一侧的一个模型请求未取得完整供应商用量；保留行为配对，token 与成本差不作比较"
        ),
        "model": moviepilot_conditions,
        "moviepilot": moviepilot_side,
        "codex": codex_side,
        "delta_moviepilot_minus_codex": _delta(moviepilot, codex, usage_comparable=complete_usage),
    }


def compare_report_files(moviepilot_path: Path, codex_path: Path) -> dict[str, Any]:
    """从两个路径读取报告并执行严格成对校验。"""
    return compare_reports(_read_report(moviepilot_path), _read_report(codex_path))
