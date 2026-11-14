"""
Ruff 与覆盖率增量门禁测试。
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from coverage import Coverage

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
    """
    Ruff 基线拒绝新增、增长以及尚未固化的下降。
    """
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
    """
    Ruff JSON 的绝对路径必须归一化为稳定仓库路径。
    """
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
    """
    Ruff 工具异常必须 fail-closed，不能把空输出当成零诊断。
    """
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
    """
    Ruff 退出状态必须与结构化诊断是否存在一致。
    """
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
    """
    已有基线出现增长时，--write 不得覆盖原文件。
    """
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
    """
    没有增长时，--write 应把下降后的 Ruff 快照固化。
    """
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


def _coverage_report(files):
    """
    构造最小 coverage.py JSON 报告。
    """
    return {"files": {path: {"summary": {"num_statements": s, "covered_lines": c}} for path, (s, c) in files.items()}}


def _coverage_snapshot(files):
    """
    将最小报告转换为 coverage ratchet 快照。
    """
    return collect_package_coverage(_coverage_report(files))


def test_coverage_ratchet_collects_all_app_files_except_plugins() -> None:
    """
    覆盖率快照覆盖 app 全部文件，并排除 plugins。
    """
    snapshot = _coverage_snapshot(
        {
            "app/application/a.py": (10, 8),
            "app/chain/a.py": (30, 12),
            "app/__init__.py": (0, 0),
            "app/plugins/official.py": (10, 0),
            "tests/outside.py": (10, 0),
        }
    )
    assert snapshot["files"]["app/chain/a.py"]["percent"] == 40.0
    assert "app/plugins/official.py" not in snapshot["files"]
    assert snapshot["total"] == {
        "statements": 40,
        "covered_lines": 20,
        "percent": 50.0,
    }


def test_coverage_ratchet_only_allows_existing_low_watermark_to_increase() -> None:
    """
    存量文件覆盖率下降失败，计数变化但比例不降可以通过。
    """
    baseline = _coverage_snapshot(
        {"app/application/a.py": (10, 8), "app/chain/a.py": (10, 5)}
    )
    current = _coverage_snapshot(
        {"app/application/a.py": (10, 7), "app/chain/a.py": (20, 10)}
    )
    assert compare_coverage(baseline, current) == [
        "app/application/a.py: 存量覆盖率下降 80.00%->70.00%"
    ]


def test_coverage_ratchet_allows_fewer_statements_without_new_misses() -> None:
    """
    文档表达式退出统计后，未覆盖行数不变不能误报为回退。
    """
    baseline = _coverage_snapshot({"app/agent/middleware/jobs.py": (118, 92)})
    current = _coverage_snapshot({"app/agent/middleware/jobs.py": (109, 83)})

    assert compare_coverage(baseline, current) == []


def test_coverage_ratchet_rejects_new_misses_despite_fewer_statements() -> None:
    """
    即使语句总数减少，未覆盖行数增加仍须判为回退。
    """
    baseline = _coverage_snapshot({"app/agent/middleware/jobs.py": (118, 92)})
    current = _coverage_snapshot({"app/agent/middleware/jobs.py": (109, 82)})

    assert compare_coverage(baseline, current) == [
        "app/agent/middleware/jobs.py: 存量覆盖率下降 77.97%->75.23%"
    ]


def test_coverage_config_excludes_only_standalone_ellipsis(tmp_path: Path) -> None:
    """
    协议占位语句不计覆盖率，含省略号的真实语句仍须执行。
    """
    source = tmp_path / "ellipsis_contract.py"
    lines = [
        "def placeholder():",
        "    ...",
        "def real_value():",
        "    return ...",
        "def actual_text():",
        '    return "..."',
    ]
    source.write_text("\n".join(lines) + "\n", encoding="utf-8")
    coverage = Coverage(
        config_file=str(PROJECT_ROOT / ".coveragerc"),
        data_file=str(tmp_path / ".coverage"),
        source=[str(tmp_path)],
    )

    _, statements, excluded, missing, _ = coverage.analysis2(str(source))

    assert 2 in excluded
    assert 4 in statements and 4 in missing
    assert 6 in statements and 6 in missing


def test_coverage_ratchet_requires_new_files_to_reach_eighty_percent() -> None:
    """
    不在低水位快照中的新文件必须达到 80% 覆盖率。
    """
    baseline = _coverage_snapshot({"app/application/existing.py": (10, 5)})
    below_floor = _coverage_snapshot(
        {"app/application/existing.py": (10, 5), "app/new.py": (10, 7)}
    )
    at_floor = _coverage_snapshot(
        {"app/application/existing.py": (10, 5), "app/new.py": (10, 8)}
    )
    assert compare_coverage(baseline, below_floor) == [
        "app/new.py: 新文件覆盖率低于 80%->70.00%"
    ]
    assert compare_coverage(baseline, at_floor) == []


def test_coverage_snapshot_validation_is_fail_closed() -> None:
    """
    非法路径和派生百分比必须被受控拒绝。
    """
    snapshot = _coverage_snapshot({"app/application/a.py": (10, 8)})
    snapshot["files"]["app/plugins/x.py"] = {
        "statements": 1,
        "covered_lines": 0,
        "percent": 0.0,
    }
    snapshot["files"]["app/application/a.py"]["percent"] = 79.0
    snapshot["packages"]["ghost"] = {
        "statements": 1,
        "covered_lines": 1,
        "percent": 100.0,
    }
    problems = validate_coverage(snapshot)
    assert any("路径不在覆盖率治理范围" in problem for problem in problems)
    assert any("percent 与计数不一致" in problem for problem in problems)
    assert any("包级摘要没有对应文件" in problem for problem in problems)


def test_coverage_main_rejects_empty_governed_report() -> None:
    """
    没有受治理文件的报告不能被误判为通过。
    """
    snapshot = _coverage_snapshot({})

    assert any("缺少受治理文件" in problem for problem in validate_coverage(snapshot, require_data=True))


def test_coverage_main_rejects_malformed_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    缺少 files 对象的 JSON 报告必须返回失败而不是抛出栈。
    """
    report_path = tmp_path / "coverage.json"
    report_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["coverage_ratchet.py", "--report", str(report_path)])
    assert coverage_main() == 1


def test_coverage_write_refuses_to_legalize_regression(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    已有低水位出现下降时，--write 不得覆盖原文件。
    """
    report_path = tmp_path / "coverage.json"
    baseline_path = tmp_path / "coverage-baseline.json"
    report_path.write_text(
        json.dumps(_coverage_report({"app/application/a.py": (10, 7)})),
        encoding="utf-8",
    )
    original = _coverage_snapshot({"app/application/a.py": (10, 8)})
    original_bytes = json.dumps(original, indent=1) + "\n"
    baseline_path.write_text(original_bytes, encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["coverage_ratchet.py", "--write", "--report", str(report_path), "--baseline", str(baseline_path)],
    )
    assert coverage_main() == 1
    assert baseline_path.read_text(encoding="utf-8") == original_bytes


def test_coverage_write_persists_improved_low_watermarks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    --write 固化提升后的文件，并保留未提升文件的旧比例。
    """
    report_path = tmp_path / "coverage.json"
    baseline_path = tmp_path / "coverage-baseline.json"
    current_files = {
        "app/application/improved.py": (20, 18),
        "app/application/unchanged.py": (10, 7),
        "app/new.py": (10, 8),
    }
    baseline = _coverage_snapshot(
        {
            "app/application/improved.py": (10, 8),
            "app/application/unchanged.py": (10, 8),
        }
    )
    report_path.write_text(json.dumps(_coverage_report(current_files)), encoding="utf-8")
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["coverage_ratchet.py", "--write", "--report", str(report_path), "--baseline", str(baseline_path)],
    )
    assert coverage_main() == 1
    assert json.loads(baseline_path.read_text(encoding="utf-8")) == baseline

    current_files["app/application/unchanged.py"] = (10, 8)
    report_path.write_text(json.dumps(_coverage_report(current_files)), encoding="utf-8")
    assert coverage_main() == 0
    written = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert written["files"]["app/application/improved.py"] == {
        "statements": 20,
        "covered_lines": 18,
        "percent": 90.0,
    }
    assert written["files"]["app/application/unchanged.py"] == {
        "statements": 10,
        "covered_lines": 8,
        "percent": 80.0,
    }
    assert written["files"]["app/new.py"]["percent"] == 80.0


def test_coverage_write_rejects_malformed_existing_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    已有 malformed 基线不能借 --write 被静默洗掉。
    """
    report_path = tmp_path / "coverage.json"
    baseline_path = tmp_path / "coverage-baseline.json"
    report_path.write_text(
        json.dumps(_coverage_report({"app/application/a.py": (10, 8)})),
        encoding="utf-8",
    )
    original_bytes = b'{"schema_version": 1}\n'
    baseline_path.write_bytes(original_bytes)
    monkeypatch.setattr(
        sys,
        "argv",
        ["coverage_ratchet.py", "--write", "--report", str(report_path), "--baseline", str(baseline_path)],
    )
    assert coverage_main() == 1
    assert baseline_path.read_bytes() == original_bytes
