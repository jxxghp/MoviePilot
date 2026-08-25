"""Ruff 与覆盖率增量门禁测试。"""

from scripts.architecture.coverage_ratchet import (
    collect_package_coverage,
    compare_coverage,
)
from scripts.architecture.ruff_ratchet import aggregate_diagnostics, compare_counts


def test_ruff_ratchet_rejects_new_rule_and_count_growth() -> None:
    """Ruff 基线允许修复，但拒绝新增规则和既有数量增长。"""
    baseline = {"app/example.py": {"F401": 2}}

    assert compare_counts(baseline, {"app/example.py": {"F401": 1}}) == []
    assert compare_counts(baseline, {"app/example.py": {"F401": 3}}) == [
        "app/example.py: Ruff 诊断增长 [F401] 2->3"
    ]
    assert compare_counts(baseline, {"app/new.py": {"I001": 1}}) == [
        "app/new.py: 新增 Ruff 诊断 [I001] x1"
    ]


def test_ruff_diagnostics_are_aggregated_by_relative_file_and_code() -> None:
    """Ruff JSON 的绝对路径必须归一化为稳定仓库路径。"""
    from scripts.architecture.ruff_ratchet import PROJECT_ROOT

    path = PROJECT_ROOT / "app" / "example.py"
    diagnostics = [
        {"filename": str(path), "code": "F401"},
        {"filename": str(path), "code": "F401"},
        {"filename": str(path), "code": "I001"},
    ]

    assert aggregate_diagnostics(diagnostics) == {
        "app/example.py": {"F401": 2, "I001": 1}
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
