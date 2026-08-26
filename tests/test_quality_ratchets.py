"""Ruff 与覆盖率增量门禁测试。"""

import json
import subprocess
import sys
from unittest.mock import patch

import pytest

from scripts.architecture.coverage_ratchet import (
    collect_package_coverage,
    compare_coverage,
)
from scripts.architecture.ruff_ratchet import (
    PROJECT_ROOT,
    aggregate_diagnostics,
    compare_counts,
    run_ruff,
)
from scripts.architecture.ruff_ratchet import (
    main as ruff_main,
)


def test_ruff_ratchet_rejects_new_rule_and_count_growth() -> None:
    """Ruff 基线拒绝新增、增长以及尚未固化的下降。"""
    baseline = {"app/example.py": {"F401": 2}}

    assert compare_counts(baseline, {"app/example.py": {"F401": 1}}) == [
        "app/example.py: Ruff 低水位未固化 [F401] 2->1"
    ]
    assert compare_counts(baseline, {"app/example.py": {"F401": 3}}) == [
        "app/example.py: Ruff 诊断增长 [F401] 2->3"
    ]
    assert compare_counts(
        baseline,
        {
            "app/example.py": {"F401": 2},
            "app/new.py": {"I001": 1},
        },
    ) == [
        "app/new.py: 新增 Ruff 诊断 [I001] x1"
    ]


def test_ruff_diagnostics_are_aggregated_by_relative_file_and_code() -> None:
    """Ruff JSON 的绝对路径必须归一化为稳定仓库路径。"""
    path = PROJECT_ROOT / "app" / "example.py"
    diagnostics = [
        {"filename": str(path), "code": "F401"},
        {"filename": str(path), "code": "F401"},
        {"filename": str(path), "code": "I001"},
    ]

    assert aggregate_diagnostics(diagnostics) == {
        "app/example.py": {"F401": 2, "I001": 1}
    }


def test_run_ruff_rejects_tool_failure() -> None:
    """Ruff 工具异常必须 fail-closed，不能把空输出当成零诊断。"""
    result = subprocess.CompletedProcess(
        args=[],
        returncode=2,
        stdout="",
        stderr="ruff failed",
    )
    with patch("scripts.architecture.ruff_ratchet.subprocess.run", return_value=result):
        with pytest.raises(RuntimeError, match="ruff failed"):
            run_ruff()


@pytest.mark.parametrize(
    ("returncode", "stdout", "message"),
    [
        (0, '[{"code": "F401"}]', "成功但仍输出诊断"),
        (1, "[]", "诊断状态但输出为空"),
    ],
)
def test_run_ruff_rejects_status_payload_mismatch(
    returncode: int,
    stdout: str,
    message: str,
) -> None:
    """Ruff 退出状态必须与结构化诊断是否存在一致。"""
    result = subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr="",
    )
    with patch("scripts.architecture.ruff_ratchet.subprocess.run", return_value=result):
        with pytest.raises(RuntimeError, match=message):
            run_ruff()


def test_ruff_write_refuses_to_legalize_regression(tmp_path, monkeypatch) -> None:
    """已有基线出现增长时，--write 不得覆盖原文件。"""
    baseline_path = tmp_path / "ruff-baseline.json"
    baseline_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["ruff_ratchet.py", "--write", "--baseline", str(baseline_path)],
    )
    diagnostic = {
        "filename": str(PROJECT_ROOT / "app" / "example.py"),
        "code": "F401",
    }

    with patch("scripts.architecture.ruff_ratchet.run_ruff", return_value=[diagnostic]):
        assert ruff_main() == 1

    assert json.loads(baseline_path.read_text(encoding="utf-8")) == {}


def test_ruff_write_persists_reduced_low_watermark(tmp_path, monkeypatch) -> None:
    """没有增长时，--write 应把下降后的 Ruff 快照固化。"""
    baseline_path = tmp_path / "ruff-baseline.json"
    baseline_path.write_text(
        json.dumps({"app/example.py": {"F401": 2}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["ruff_ratchet.py", "--write", "--baseline", str(baseline_path)],
    )
    diagnostic = {
        "filename": str(PROJECT_ROOT / "app" / "example.py"),
        "code": "F401",
    }

    with patch("scripts.architecture.ruff_ratchet.run_ruff", return_value=[diagnostic]):
        assert ruff_main() == 0

    assert json.loads(baseline_path.read_text(encoding="utf-8")) == {
        "app/example.py": {"F401": 1}
    }


def test_coverage_ratchet_aggregates_governed_packages() -> None:
    """覆盖率门禁只聚合 Application 与 Domain，并按真实语句数加权。"""
    report = {
        "files": {
            "app/application/a.py": {
                "summary": {"num_statements": 10, "covered_lines": 8}
            },
            "app/application/b.py": {
                "summary": {"num_statements": 30, "covered_lines": 12}
            },
            "app/domain/a.py": {
                "summary": {"num_statements": 20, "covered_lines": 15}
            },
            "app/chain/a.py": {
                "summary": {"num_statements": 100, "covered_lines": 0}
            },
        }
    }

    assert collect_package_coverage(report) == {
        "application": {"statements": 40, "covered_lines": 20, "percent": 50.0},
        "domain": {"statements": 20, "covered_lines": 15, "percent": 75.0},
    }


def test_coverage_ratchet_rejects_only_regressions() -> None:
    """包覆盖率达到或超过阈值时通过，任一包下降时失败。"""
    baseline = {
        "application": {"percent": 50.0},
        "domain": {"percent": 75.0},
    }

    assert compare_coverage(
        baseline,
        {"application": {"percent": 50.0}, "domain": {"percent": 76.0}},
    ) == []
    assert compare_coverage(
        baseline,
        {"application": {"percent": 49.99}, "domain": {"percent": 75.0}},
    ) == ["application: 行覆盖率下降 50.00%->49.99%"]
