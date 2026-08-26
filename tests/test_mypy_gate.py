"""mypy 错误低水位 ratchet 的执行、解析与对比逻辑测试。"""

import json
import subprocess
import sys
from unittest.mock import patch

import pytest

from scripts.architecture.mypy_ratchet import compare_counts, main, parse_errors, run_mypy

MYPY_SAMPLE = """
app/application/a.py:12: error: Function is missing a type annotation [no-untyped-def]
app/application/a.py:15: error: Function is missing a type annotation [no-untyped-def]
     def run(self):
     ^
app/application/a.py:20: error: Missing type parameters for generic type "dict"  [type-arg]
Found 3 errors in 1 file (checked 500 source files)
"""


def test_run_mypy_uses_stable_full_analysis() -> None:
    """全量门禁不得复用前序检查缓存，也不得输出换行错误码。"""
    with patch("scripts.architecture.mypy_ratchet.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="Success: no issues found in 500 source files\n",
            stderr="",
        )

        run_mypy()

    command = run.call_args.args[0]
    assert "--no-incremental" in command
    assert "--no-pretty" in command
    assert command[command.index("--platform") + 1] == "linux"


def test_run_mypy_rejects_tool_failure() -> None:
    """内部错误等退出码 2 必须让门禁失败，不能拿截断输出生成基线。"""
    result = subprocess.CompletedProcess(
        args=[],
        returncode=2,
        stdout=MYPY_SAMPLE,
        stderr="mypy: INTERNAL ERROR",
    )
    with patch("scripts.architecture.mypy_ratchet.subprocess.run", return_value=result):
        with pytest.raises(RuntimeError, match="异常退出.*INTERNAL ERROR"):
            run_mypy()


def test_run_mypy_rejects_unexpected_stderr() -> None:
    """正常退出码携带 stderr 时仍须 fail-closed。"""
    result = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout=MYPY_SAMPLE,
        stderr="analysis incomplete",
    )
    with patch("scripts.architecture.mypy_ratchet.subprocess.run", return_value=result):
        with pytest.raises(RuntimeError, match="stderr"):
            run_mypy()


def test_run_mypy_rejects_truncated_error_report() -> None:
    """摘要错误数与解析数量不同时必须拒绝不完整报告。"""
    result = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout=MYPY_SAMPLE.replace("Found 3 errors", "Found 4 errors"),
        stderr="",
    )
    with patch("scripts.architecture.mypy_ratchet.subprocess.run", return_value=result):
        with pytest.raises(RuntimeError, match="摘要与已解析错误数不一致"):
            run_mypy()


def test_run_mypy_accepts_complete_error_report() -> None:
    """退出码 1 的完整诊断应交给 ratchet 比较，而不是被当作工具失败。"""
    result = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout=MYPY_SAMPLE,
        stderr="",
    )
    with patch("scripts.architecture.mypy_ratchet.subprocess.run", return_value=result):
        assert run_mypy() == MYPY_SAMPLE


def test_run_mypy_rejects_missing_summary() -> None:
    """没有完成摘要的错误输出可能被截断，必须拒绝。"""
    result = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout=MYPY_SAMPLE.replace(
            "Found 3 errors in 1 file (checked 500 source files)\n",
            "",
        ),
        stderr="",
    )
    with patch("scripts.architecture.mypy_ratchet.subprocess.run", return_value=result):
        with pytest.raises(RuntimeError, match="缺少唯一的完整摘要"):
            run_mypy()


def test_parse_errors_aggregates_per_file_and_code() -> None:
    """错误行按文件与错误码聚合，源码上下文与摘要行不计入。"""
    report = parse_errors(MYPY_SAMPLE)

    assert report == {
        "app/application/a.py": {
            "no-untyped-def": 2,
            "type-arg": 1,
        }
    }


def test_parse_errors_buckets_missing_code_as_unknown() -> None:
    """缺少错误码的错误行归入 unknown 桶，不丢失计数。"""
    report = parse_errors("app/b.py:3: error: Something went wrong\n")

    assert report == {"app/b.py": {"unknown": 1}}


def test_ratchet_requires_reduced_errors_to_be_persisted() -> None:
    """错误减少后必须刷新 fixture，避免旧额度允许回退。"""
    baseline = {"app/a.py": {"arg-type": 5, "misc": 2}}
    current = {"app/a.py": {"arg-type": 3}}

    assert compare_counts(baseline, current) == [
        "app/a.py: 类型错误低水位未固化 [arg-type] 5->3",
        "app/a.py: 类型错误低水位未固化 [misc] 2->0",
    ]


def test_ratchet_requires_deleted_file_to_be_persisted() -> None:
    """整文件债务消失也必须写回 fixture。"""
    baseline = {"app/a.py": {"arg-type": 1}}

    assert compare_counts(baseline, {}) == [
        "app/a.py: 类型错误低水位未固化 [arg-type] 1->0"
    ]


def test_ratchet_rejects_growth_and_new_errors() -> None:
    """既有错误码增长和新增文件/错误码必须同时给出精确诊断。"""
    baseline = {
        "app/a.py": {"arg-type": 5},
        "app/c.py": {"misc": 1},
    }
    current = {
        "app/a.py": {"arg-type": 6, "type-arg": 2},
        "app/b.py": {"no-untyped-def": 10},
        "app/c.py": {},
    }

    problems = compare_counts(baseline, current)

    assert any("app/a.py" in p and "arg-type" in p and "既有错误增长" in p for p in problems)
    assert any("app/a.py" in p and "type-arg" in p and "新增类型错误" in p for p in problems)
    assert any("app/b.py" in p and "新增类型错误" in p for p in problems)


def test_write_refuses_to_legalize_type_regression(tmp_path, monkeypatch) -> None:
    """已有基线出现增长时，--write 不得覆盖原文件。"""
    baseline_path = tmp_path / "mypy-baseline.json"
    baseline_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["mypy_ratchet.py", "--write", "--baseline", str(baseline_path)],
    )

    with patch("scripts.architecture.mypy_ratchet.run_mypy", return_value=MYPY_SAMPLE):
        assert main() == 1

    assert json.loads(baseline_path.read_text(encoding="utf-8")) == {}


def test_write_persists_reduced_type_low_watermark(tmp_path, monkeypatch) -> None:
    """没有增长时，--write 应把下降后的完整快照固化。"""
    baseline_path = tmp_path / "mypy-baseline.json"
    baseline_path.write_text(
        json.dumps(
            {
                "app/application/a.py": {
                    "no-untyped-def": 3,
                    "type-arg": 1,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["mypy_ratchet.py", "--write", "--baseline", str(baseline_path)],
    )

    with patch("scripts.architecture.mypy_ratchet.run_mypy", return_value=MYPY_SAMPLE):
        assert main() == 0

    assert json.loads(baseline_path.read_text(encoding="utf-8")) == {
        "app/application/a.py": {"no-untyped-def": 2, "type-arg": 1}
    }
