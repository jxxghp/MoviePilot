"""成对评测必须锁定同场景、同源码和同模型条件。"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.evaluation.compare import compare_reports


def _report(evidence_kind: str, *, passed: bool = True) -> dict:
    """构造不含正文和凭据的最小真实报告。"""
    return {
        "scenario_id": "dedup_existing",
        "scenario_sha256": "scenario",
        "production_agent_sha256": "agent",
        "skills_sha256": "skills",
        "harness_sha256": "harness",
        "evidence_kind": evidence_kind,
        "passed": passed,
        "violations": [] if passed else ["download_not_verified"],
        "intelligence_evaluated": True,
        "usage_complete": True,
        "model_calls": 7,
        "completed_model_calls": 7,
        "failed_tool_calls": 0,
        "tool_calls": 2,
        "duplicate_attempts": 0,
        "side_effects": 0,
        "elapsed_seconds": 12.5,
        "tokens": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        "model": {
            "requested_model": "gpt-5.6-luna", "reasoning_effort": "max", "max_model_calls": 16,
            "max_output_tokens": 8192, "timeout_seconds": 180, "harness_context_window": 128000,
            "auth_mode": "codex_oauth",
        },
    }


def test_same_conditions_produce_a_directional_pair_summary() -> None:
    """一侧失败也应保留有效的成对事实，不能把比较结果伪装成两侧都通过。"""
    moviepilot = _report("moviepilot_live_model", passed=False)
    codex = _report("codex_native_controlled")
    codex["model_calls"] = 5
    codex["tokens"] = {"input_tokens": 80, "output_tokens": 10, "total_tokens": 90}
    result = compare_reports(moviepilot, codex)
    assert result["codex_comparison"] is True and result["pair_valid"] is True
    assert result["both_passed"] is False
    assert result["delta_moviepilot_minus_codex"]["model_calls"] == 2
    assert result["moviepilot"]["violations"] == ["download_not_verified"]


@pytest.mark.parametrize("field", ["scenario_id", "scenario_sha256", "production_agent_sha256", "skills_sha256", "harness_sha256"])
def test_different_provenance_is_rejected(field: str) -> None:
    """不同场景或代码指纹不能被合并成一次比较。"""
    moviepilot, codex = _report("moviepilot_live_model"), _report("codex_native_controlled")
    codex[field] = "different"
    with pytest.raises(ValueError, match=field):
        compare_reports(moviepilot, codex)


def test_different_model_condition_is_rejected() -> None:
    """模型、推理档位或预算不同会破坏对照，必须先停止。"""
    moviepilot, codex = _report("moviepilot_live_model"), _report("codex_native_controlled")
    codex["model"]["reasoning_effort"] = "high"
    with pytest.raises(ValueError, match="模型"):
        compare_reports(moviepilot, codex)


def test_incomplete_usage_is_rejected() -> None:
    """缺少供应商用量时保留单侧报告，但不能宣称完成成对比较。"""
    moviepilot, codex = _report("moviepilot_live_model"), _report("codex_native_controlled")
    moviepilot["usage_complete"] = False
    with pytest.raises(ValueError, match="完整用量"):
        compare_reports(moviepilot, codex)


def test_cli_compare_reads_reports_and_writes_output(tmp_path: Path) -> None:
    """命令行比较只读取已有报告，不启动模型或业务服务。"""
    live, native, output = (tmp_path / name for name in ("live.json", "native.json", "pair.json"))
    live.write_text(json.dumps(_report("moviepilot_live_model")), encoding="utf-8")
    native.write_text(json.dumps(_report("codex_native_controlled")), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "scripts.evaluation", "--compare", str(live), str(native), "--output", str(output)],
        capture_output=True, text=True, check=False,
    )
    payload = json.loads(result.stdout)
    assert result.returncode == 0 and payload["pair_valid"] is True
    assert json.loads(output.read_text(encoding="utf-8")) == payload
