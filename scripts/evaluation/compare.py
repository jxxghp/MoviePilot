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


def _delta(moviepilot: dict[str, Any], codex: dict[str, Any]) -> dict[str, Any]:
    """计算两个方向的数值差；任一侧未知时保留 unknown。"""
    result: dict[str, Any] = {}
    for key in ("model_calls", "completed_model_calls", "failed_tool_calls", "tool_calls",
                "duplicate_attempts", "side_effects", "elapsed_seconds", "total_tokens"):
        left, right = _metric(moviepilot, key), _metric(codex, key)
        result[key] = None if not isinstance(left, (int, float)) or not isinstance(right, (int, float)) else left - right
    return result


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
    if not all(report.get("intelligence_evaluated") and report.get("usage_complete") for report in (moviepilot, codex)):
        raise ValueError("成对比较需要两侧都完成真实模型调用并取得完整用量")

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
        "model": moviepilot_conditions,
        "moviepilot": moviepilot_side,
        "codex": codex_side,
        "delta_moviepilot_minus_codex": _delta(moviepilot, codex),
    }


def compare_report_files(moviepilot_path: Path, codex_path: Path) -> dict[str, Any]:
    """从两个路径读取报告并执行严格成对校验。"""
    return compare_reports(_read_report(moviepilot_path), _read_report(codex_path))
