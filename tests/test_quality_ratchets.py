"""Ruff 与覆盖率增量门禁测试。"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.architecture.coverage_ratchet import (
    collect_package_coverage,
    compare_coverage,
    validate_coverage,
)
from scripts.architecture.coverage_ratchet import (
    main as coverage_main,
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


def test_ruff_write_refuses_to_legalize_regression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


def test_ruff_write_persists_reduced_low_watermark(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


def test_coverage_ratchet_enforces_fixed_eighty_percent_floor() -> None:
    """覆盖率门禁只检查 Application 与 Domain 的固定 80% 基线。"""
    baseline = {
        "application": {"statements": 10, "covered_lines": 5, "percent": 50.0},
        "domain": {"statements": 20, "covered_lines": 15, "percent": 75.0},
    }

    assert compare_coverage(
        baseline,
        {
            "application": {"statements": 100, "covered_lines": 79, "percent": 79.0},
            "domain": {"statements": 25, "covered_lines": 20, "percent": 80.0},
        },
    ) == ["application: 行覆盖率低于固定基线 80.00%->79.00%"]
    assert compare_coverage(
        baseline,
        {
            "application": {
                "statements": 10000,
                "covered_lines": 7999,
                "percent": 79.99,
            },
            "domain": {"statements": 20, "covered_lines": 16, "percent": 80.0},
        },
    ) == ["application: 行覆盖率低于固定基线 80.00%->79.99%"]


def test_coverage_ratchet_uses_exact_ratio_at_fixed_floor() -> None:
    """固定基线比较使用真实计数，不受显示百分比四舍五入影响。"""
    baseline = {
        "application": {"statements": 10, "covered_lines": 5, "percent": 50.0},
        "domain": {"statements": 20, "covered_lines": 15, "percent": 75.0},
    }
    current = {
        "application": {
            "statements": 10000,
            "covered_lines": 7999,
            "percent": 79.99,
        },
        "domain": {"statements": 20, "covered_lines": 16, "percent": 80.0},
    }

    assert compare_coverage(baseline, current) == [
        "application: 行覆盖率低于固定基线 80.00%->79.99%"
    ]


def test_coverage_ratchet_ignores_snapshot_shape_changes_above_floor() -> None:
    """达到固定基线后，语句计数变化不会制造快照同步噪音。"""
    baseline = {
        "application": {"statements": 10, "covered_lines": 5, "percent": 50.0},
        "domain": {"statements": 20, "covered_lines": 15, "percent": 75.0},
    }
    current = {
        "application": {"statements": 20, "covered_lines": 16, "percent": 80.0},
        "domain": {"statements": 20, "covered_lines": 16, "percent": 80.0},
    }

    assert compare_coverage(baseline, current) == []


def test_coverage_ratchet_rejects_zero_statement_report() -> None:
    """没有采集到治理包时不得把零语句误判为满覆盖。"""
    current = collect_package_coverage({"files": {}})

    assert current == {
        "application": {"statements": 0, "covered_lines": 0, "percent": 0.0},
        "domain": {"statements": 0, "covered_lines": 0, "percent": 0.0},
    }
    assert validate_coverage(current) == [
        "application: 覆盖率报告语句数必须大于 0",
        "domain: 覆盖率报告语句数必须大于 0",
    ]


@pytest.mark.parametrize(
    ("snapshot", "message"),
    [
        (
            {
                "application": {
                    "statements": 10,
                    "covered_lines": 11,
                    "percent": 110.0,
                },
                "domain": {"statements": 20, "covered_lines": 15, "percent": 75.0},
            },
            "已覆盖行数越界",
        ),
        (
            {
                "application": {
                    "statements": 10,
                    "covered_lines": 5,
                    "percent": 49.0,
                },
                "domain": {"statements": 20, "covered_lines": 15, "percent": 75.0},
            },
            "percent 与计数不一致",
        ),
        (
            {
                "application": {
                    "statements": True,
                    "covered_lines": 1,
                    "percent": 100.0,
                },
                "domain": {"statements": 20, "covered_lines": 15, "percent": 75.0},
            },
            "必须是非负整数",
        ),
    ],
)
def test_coverage_snapshot_validation_is_fail_closed(
    snapshot: object,
    message: str,
) -> None:
    """非法计数、布尔计数和人工百分比必须被受控拒绝。"""
    assert any(message in problem for problem in validate_coverage(snapshot))


def test_coverage_main_rejects_malformed_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺少 files 对象的 JSON 报告必须返回失败而不是抛出栈。"""
    report_path = tmp_path / "coverage.json"
    report_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["coverage_ratchet.py", "--report", str(report_path)],
    )

    assert coverage_main() == 1


def test_coverage_write_refuses_to_legalize_regression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已有阈值出现下降时，--write 不得覆盖原文件。"""
    report_path = tmp_path / "coverage.json"
    baseline_path = tmp_path / "coverage-baseline.json"
    report_path.write_text(
        json.dumps({
            "files": {
                "app/application/a.py": {
                    "summary": {"num_statements": 10, "covered_lines": 5}
                },
                "app/domain/a.py": {
                    "summary": {"num_statements": 20, "covered_lines": 14}
                },
            }
        }),
        encoding="utf-8",
    )
    original = {
        "application": {"statements": 10, "covered_lines": 4, "percent": 40.0},
        "domain": {"statements": 20, "covered_lines": 15, "percent": 75.0},
    }
    original_bytes = json.dumps(original, indent=1) + "\n"
    baseline_path.write_text(original_bytes, encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "coverage_ratchet.py",
            "--write",
            "--report",
            str(report_path),
            "--baseline",
            str(baseline_path),
        ],
    )

    assert coverage_main() == 1
    assert baseline_path.read_text(encoding="utf-8") == original_bytes


def test_coverage_write_persists_improved_low_watermark(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """达到固定基线时，--write 只固化标准 80% fixture。"""
    report_path = tmp_path / "coverage.json"
    baseline_path = tmp_path / "coverage-baseline.json"
    report_path.write_text(
        json.dumps({
            "files": {
                "app/application/a.py": {
                    "summary": {"num_statements": 10, "covered_lines": 8}
                },
                "app/domain/a.py": {
                    "summary": {"num_statements": 20, "covered_lines": 16}
                },
            }
        }),
        encoding="utf-8",
    )
    baseline_path.write_text(
        json.dumps({
            "application": {"statements": 10, "covered_lines": 4, "percent": 40.0},
            "domain": {"statements": 20, "covered_lines": 14, "percent": 70.0},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "coverage_ratchet.py",
            "--write",
            "--report",
            str(report_path),
            "--baseline",
            str(baseline_path),
        ],
    )

    assert coverage_main() == 0
    assert json.loads(baseline_path.read_text(encoding="utf-8")) == {
        "application": {"statements": 100, "covered_lines": 80, "percent": 80.0},
        "domain": {"statements": 100, "covered_lines": 80, "percent": 80.0},
    }


def test_coverage_write_persists_equal_ratio_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """固定基线不要求同步运行时的真实语句计数。"""
    report_path = tmp_path / "coverage.json"
    baseline_path = tmp_path / "coverage-baseline.json"
    report_path.write_text(
        json.dumps({
            "files": {
                "app/application/a.py": {
                    "summary": {"num_statements": 20, "covered_lines": 16}
                },
                "app/domain/a.py": {
                    "summary": {"num_statements": 20, "covered_lines": 16}
                },
            }
        }),
        encoding="utf-8",
    )
    baseline_path.write_text(
        json.dumps({
            "application": {"statements": 10, "covered_lines": 5, "percent": 50.0},
            "domain": {"statements": 20, "covered_lines": 15, "percent": 75.0},
        }),
        encoding="utf-8",
    )
    command = [
        "coverage_ratchet.py",
        "--report",
        str(report_path),
        "--baseline",
        str(baseline_path),
    ]

    monkeypatch.setattr(sys, "argv", command)
    assert coverage_main() == 0
    monkeypatch.setattr(sys, "argv", [*command, "--write"])
    assert coverage_main() == 0
    monkeypatch.setattr(sys, "argv", command)
    assert coverage_main() == 0
    assert json.loads(baseline_path.read_text(encoding="utf-8")) == {
        "application": {"statements": 100, "covered_lines": 80, "percent": 80.0},
        "domain": {"statements": 100, "covered_lines": 80, "percent": 80.0},
    }


def test_coverage_fixed_baseline_replaces_legacy_zero_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """历史全零 fixture 不改变固定 80% 门禁，写入后统一为标准 fixture。"""
    report_path = tmp_path / "coverage.json"
    baseline_path = tmp_path / "coverage-baseline.json"
    baseline_path.write_text(
        json.dumps({
            "application": {"statements": 0, "covered_lines": 0, "percent": 0.0},
            "domain": {"statements": 0, "covered_lines": 0, "percent": 0.0},
        }),
        encoding="utf-8",
    )
    report = {
        "files": {
            "app/application/a.py": {
                "summary": {"num_statements": 10, "covered_lines": 8}
            },
            "app/domain/a.py": {
                "summary": {"num_statements": 20, "covered_lines": 16}
            },
        }
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    command = [
        "coverage_ratchet.py",
        "--report",
        str(report_path),
        "--baseline",
        str(baseline_path),
    ]

    monkeypatch.setattr(sys, "argv", command)
    assert coverage_main() == 0
    monkeypatch.setattr(sys, "argv", [*command, "--write"])
    assert coverage_main() == 0
    assert json.loads(baseline_path.read_text(encoding="utf-8")) == {
        "application": {"statements": 100, "covered_lines": 80, "percent": 80.0},
        "domain": {"statements": 100, "covered_lines": 80, "percent": 80.0},
    }
    initialized_bytes = baseline_path.read_bytes()

    report["files"]["app/domain/a.py"]["summary"]["covered_lines"] = 15
    report_path.write_text(json.dumps(report), encoding="utf-8")
    assert coverage_main() == 1
    assert baseline_path.read_bytes() == initialized_bytes


def test_coverage_write_rejects_malformed_existing_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已有 malformed 基线不能借 --write 被静默洗掉。"""
    report_path = tmp_path / "coverage.json"
    baseline_path = tmp_path / "coverage-baseline.json"
    report_path.write_text(
        json.dumps({
            "files": {
                "app/application/a.py": {
                    "summary": {"num_statements": 10, "covered_lines": 5}
                },
                "app/domain/a.py": {
                    "summary": {"num_statements": 20, "covered_lines": 15}
                },
            }
        }),
        encoding="utf-8",
    )
    original_bytes = json.dumps({
        "application": {"statements": 0, "covered_lines": 0, "percent": 0.0},
    }).encode()
    baseline_path.write_bytes(original_bytes)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "coverage_ratchet.py",
            "--write",
            "--report",
            str(report_path),
            "--baseline",
            str(baseline_path),
        ],
    )

    assert coverage_main() == 1
    assert baseline_path.read_bytes() == original_bytes
