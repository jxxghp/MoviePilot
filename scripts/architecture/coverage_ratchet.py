"""检查 Application 与 Domain 是否达到固定 80% 行覆盖率基线。"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORT = PROJECT_ROOT / "coverage.json"
DEFAULT_BASELINE = PROJECT_ROOT / "tests/fixtures/architecture/coverage-baseline.json"
PACKAGE_PREFIXES = {
    "application": "app/application/",
    "domain": "app/domain/",
}
FIXED_COVERAGE_PERCENT = 80.0
FIXED_BASELINE = {
    name: {"statements": 100, "covered_lines": 80, "percent": FIXED_COVERAGE_PERCENT}
    for name in PACKAGE_PREFIXES
}
LEGACY_ZERO_BASELINE = {
    name: {"statements": 0, "covered_lines": 0, "percent": 0.0}
    for name in PACKAGE_PREFIXES
}


def _non_negative_int(value: object, *, field: str) -> int:
    """读取 coverage 整数计数，拒绝 bool、字符串和负值。"""
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} 必须是非负整数")
    return value


def collect_package_coverage(report: dict[str, Any]) -> dict[str, dict[str, int | float]]:
    """按治理包聚合 coverage.py JSON 中的语句和已覆盖行。"""
    if not isinstance(report, Mapping):
        raise ValueError("coverage JSON 顶层必须是对象")
    files = report.get("files")
    if not isinstance(files, Mapping):
        raise ValueError("coverage JSON 缺少 files 对象")
    result: dict[str, dict[str, int | float]] = {}
    for name, prefix in PACKAGE_PREFIXES.items():
        statements = 0
        covered = 0
        for path, details in files.items():
            if not isinstance(path, str):
                raise ValueError("coverage files 键必须是字符串路径")
            if not path.replace("\\", "/").startswith(prefix):
                continue
            if not isinstance(details, Mapping):
                raise ValueError(f"{path}: coverage 文件详情必须是对象")
            summary = details.get("summary")
            if not isinstance(summary, Mapping):
                raise ValueError(f"{path}: coverage 文件详情缺少 summary 对象")
            file_statements = _non_negative_int(
                summary.get("num_statements"),
                field=f"{path}.num_statements",
            )
            file_covered = _non_negative_int(
                summary.get("covered_lines"),
                field=f"{path}.covered_lines",
            )
            if file_covered > file_statements:
                raise ValueError(
                    f"{path}: 已覆盖行数越界 {file_covered}/{file_statements}"
                )
            statements += file_statements
            covered += file_covered
        percent = round(covered * 100 / statements, 2) if statements else 0.0
        result[name] = {
            "statements": statements,
            "covered_lines": covered,
            "percent": percent,
        }
    return result


def validate_coverage(
    current: object,
) -> list[str]:
    """拒绝缺包、额外包、零快照和不一致的派生百分比。"""
    problems: list[str] = []
    if not isinstance(current, Mapping):
        return ["覆盖率快照必须是对象"]
    expected_names = set(PACKAGE_PREFIXES)
    actual_names = set(current)
    for name in sorted(expected_names - actual_names):
        problems.append(f"{name}: 覆盖率报告缺少治理包")
    for name in sorted(actual_names - expected_names):
        problems.append(f"{name}: 覆盖率快照包含未知治理包")
    for name in PACKAGE_PREFIXES:
        values = current.get(name)
        if values is None:
            continue
        if not isinstance(values, Mapping):
            problems.append(f"{name}: 覆盖率数据必须是对象")
            continue
        if set(values) != {"statements", "covered_lines", "percent"}:
            problems.append(f"{name}: 覆盖率数据字段不完整或包含未知字段")
            continue
        try:
            statements = _non_negative_int(
                values.get("statements"),
                field=f"{name}.statements",
            )
            covered = _non_negative_int(
                values.get("covered_lines"),
                field=f"{name}.covered_lines",
            )
        except ValueError as error:
            problems.append(str(error))
            continue
        percent = values.get("percent")
        if (
            isinstance(percent, bool)
            or not isinstance(percent, (int, float))
            or not math.isfinite(float(percent))
        ):
            problems.append(f"{name}.percent 必须是有限数值")
            continue
        if statements <= 0:
            problems.append(f"{name}: 覆盖率报告语句数必须大于 0")
            continue
        if not 0 <= covered <= statements:
            problems.append(
                f"{name}: 已覆盖行数越界 {covered}/{statements}"
            )
            continue
        if covered == 0:
            problems.append(f"{name}: 已覆盖行数必须大于 0")
        expected_percent = round(covered * 100 / statements, 2)
        if float(percent) != expected_percent:
            problems.append(
                f"{name}: percent 与计数不一致 {float(percent):.2f}!={expected_percent:.2f}"
            )
    return problems


def is_legacy_zero_baseline(baseline: object) -> bool:
    """识别本批次之前唯一允许被初始化替换的全零 fixture。"""
    return baseline == LEGACY_ZERO_BASELINE


def classify_coverage(
    baseline: dict[str, dict[str, int | float]],
    current: dict[str, dict[str, int | float]],
) -> tuple[list[str], list[str]]:
    """按固定 80% 基线判断治理包是否回退。"""
    del baseline
    regressions: list[str] = []
    for name in PACKAGE_PREFIXES:
        actual_values = current[name]
        actual = float(actual_values["percent"])
        actual_statements = int(actual_values["statements"])
        actual_covered = int(actual_values["covered_lines"])
        if actual_covered * 100 < FIXED_COVERAGE_PERCENT * actual_statements:
            regressions.append(
                f"{name}: 行覆盖率低于固定基线 {FIXED_COVERAGE_PERCENT:.2f}%->{actual:.2f}%"
            )
    return regressions, []


def compare_coverage(
    baseline: dict[str, dict[str, int | float]],
    current: dict[str, dict[str, int | float]],
) -> list[str]:
    """返回低于固定 80% 基线的治理包。"""
    regressions, _ = classify_coverage(baseline, current)
    return regressions


def main() -> int:
    """检查 coverage JSON，或显式刷新当前阈值。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="写入当前覆盖率阈值")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    args = parser.parse_args()
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        current = collect_package_coverage(report)
    except (OSError, ValueError, TypeError) as error:
        print(f"Coverage 报告无效：{error}")
        return 1
    validation_problems = validate_coverage(current)
    if validation_problems:
        print("\n".join(validation_problems))
        return 1
    baseline_exists = args.baseline.exists()
    if baseline_exists:
        try:
            baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as error:
            print(f"Coverage 基线无效：{error}")
            return 1
        if not is_legacy_zero_baseline(baseline):
            baseline_problems = validate_coverage(baseline)
            if baseline_problems:
                print("Coverage 基线无效：")
                print("\n".join(baseline_problems))
                return 1
    else:
        baseline = {}
    regressions, _ = classify_coverage(baseline, current)
    if args.write:
        if regressions:
            print("\n".join(regressions))
            print("拒绝写入：当前结果低于固定 80% 基线。")
            return 1
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(
            json.dumps(FIXED_BASELINE, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        display_path = (
            args.baseline.relative_to(PROJECT_ROOT)
            if args.baseline.is_relative_to(PROJECT_ROOT)
            else args.baseline
        )
        print(f"已写入 {display_path}")
        return 0
    problems = regressions
    if problems:
        print("\n".join(problems))
        print("先补充测试或修复逻辑，使 Application 与 Domain 均达到固定 80% 基线。")
        return 1
    summary = ", ".join(
        f"{name}={values['percent']:.2f}%" for name, values in current.items()
    )
    print(f"覆盖率 ratchet 通过（固定 80% 基线：{summary}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
