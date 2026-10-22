"""
检查除官方插件外全部 app Python 文件的覆盖率低水位。
"""

from __future__ import annotations

import argparse
import json
import math
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORT = PROJECT_ROOT / "coverage.json"
DEFAULT_BASELINE = PROJECT_ROOT / "tests/fixtures/architecture/coverage-baseline.json"
# 第 2 版覆盖率快照以逐文件计数为准，同时保留包级和全量摘要。
SCHEMA_VERSION = 2
SCOPE_ROOT = "app"
EXCLUDED_PREFIXES = ("app/plugins/",)
# 新受治理文件必须覆盖至少 80% 的可执行语句。
NEW_FILE_COVERAGE_PERCENT = 80.0
_METRIC_FIELDS = {"statements", "covered_lines", "percent"}


def _non_negative_int(value: object, *, field: str) -> int:
    """
    读取 coverage 整数计数，拒绝 bool、字符串和负值。
    """
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} 必须是非负整数")
    return value


def _normalise_path(path: str) -> str:
    """
    将 coverage.py 路径中的反斜杠统一为 POSIX 分隔符。
    """
    return path.replace("\\", "/")


def _is_governed_path(path: str) -> bool:
    """
    判断路径是否属于 app 且不在官方插件目录。
    """
    normalised = _normalise_path(path)
    return (
        normalised.startswith("app/")
        and not any(
            normalised == prefix.rstrip("/") or normalised.startswith(prefix)
            for prefix in EXCLUDED_PREFIXES
        )
        and normalised.endswith(".py")
    )


def _metric(statements: int, covered: int) -> dict[str, int | float]:
    """
    根据 coverage.py 计数构造稳定的逐文件覆盖率记录。
    """
    percent = round(covered * 100 / statements, 2) if statements else 0.0
    return {"statements": statements, "covered_lines": covered, "percent": percent}


def _validate_metric(
    values: object, *, name: str, allow_zero: bool = True
) -> list[str]:
    """
    校验一条覆盖率记录的字段、计数边界和派生百分比。
    """
    problems: list[str] = []
    if not isinstance(values, Mapping):
        return [f"{name}: 覆盖率数据必须是对象"]
    if set(values) != _METRIC_FIELDS:
        return [f"{name}: 覆盖率数据字段不完整或包含未知字段"]
    try:
        statements = _non_negative_int(
            values.get("statements"), field=f"{name}.statements"
        )
        covered = _non_negative_int(
            values.get("covered_lines"), field=f"{name}.covered_lines"
        )
    except ValueError as error:
        return [str(error)]
    percent = values.get("percent")
    if (
        isinstance(percent, bool)
        or not isinstance(percent, (int, float))
        or not math.isfinite(float(percent))
    ):
        problems.append(f"{name}.percent 必须是有限数值")
        return problems
    if not 0 <= covered <= statements:
        problems.append(f"{name}: 已覆盖行数越界 {covered}/{statements}")
    if not allow_zero and statements <= 0:
        problems.append(f"{name}: 覆盖率报告语句数必须大于 0")
    expected_percent = round(covered * 100 / statements, 2) if statements else 0.0
    if float(percent) != expected_percent:
        problems.append(
            f"{name}: percent 与计数不一致 {float(percent):.2f}!={expected_percent:.2f}"
        )
    return problems


def _aggregate(
    metrics: Mapping[str, Mapping[str, int | float]],
) -> dict[str, int | float]:
    """
    按真实语句数聚合逐文件覆盖率，避免平均百分比失真。
    """
    statements = sum(int(values["statements"]) for values in metrics.values())
    covered = sum(int(values["covered_lines"]) for values in metrics.values())
    return _metric(statements, covered)


def _package_name(path: str) -> str:
    """
    返回 app 文件对应的一级包名。
    """
    relative = path.removeprefix("app/")
    return relative.split("/", 1)[0] if "/" in relative else "__root__"


def _snapshot_from_files(
    files: dict[str, dict[str, int | float]],
) -> dict[str, Any]:
    """
    从逐文件记录构造带包级和全量摘要的快照。
    """
    package_files: dict[str, dict[str, dict[str, int | float]]] = {}
    for path, values in files.items():
        package_files.setdefault(_package_name(path), {})[path] = values
    packages = {
        name: _aggregate(files_for_package)
        for name, files_for_package in sorted(package_files.items())
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": {"root": SCOPE_ROOT, "excluded": ["app/plugins"]},
        "files": dict(sorted(files.items())),
        "packages": packages,
        "total": _aggregate(files),
    }


def collect_package_coverage(report: dict[str, Any]) -> dict[str, Any]:
    """
    收集全部受治理文件，并附带一级包与全量聚合摘要。

    :param report: coverage.py 导出的 JSON 报告，必须含有效的 files 对象
    :return: 按文件、一级包和全部源码汇总的覆盖率快照
    :raises ValueError: 报告结构、文件路径或计数不符合要求
    """
    if not isinstance(report, Mapping):
        raise ValueError("coverage JSON 顶层必须是对象")
    files = report.get("files")
    if not isinstance(files, Mapping):
        raise ValueError("coverage JSON 缺少 files 对象")
    collected: dict[str, dict[str, int | float]] = {}
    for raw_path, details in files.items():
        if not isinstance(raw_path, str):
            raise ValueError("coverage files 键必须是字符串路径")
        path = _normalise_path(raw_path)
        if not _is_governed_path(path):
            continue
        if not isinstance(details, Mapping):
            raise ValueError(f"{path}: coverage 文件详情必须是对象")
        summary = details.get("summary")
        if not isinstance(summary, Mapping):
            raise ValueError(f"{path}: coverage 文件详情缺少 summary 对象")
        statements = _non_negative_int(
            summary.get("num_statements"), field=f"{path}.num_statements"
        )
        covered = _non_negative_int(
            summary.get("covered_lines"), field=f"{path}.covered_lines"
        )
        if covered > statements:
            raise ValueError(f"{path}: 已覆盖行数越界 {covered}/{statements}")
        collected[path] = _metric(statements, covered)

    return _snapshot_from_files(collected)


def validate_coverage(current: object, *, require_data: bool = False) -> list[str]:
    """
    拒绝超出治理范围、字段不完整或计数与百分比不一致的快照。

    :param current: 待检查的覆盖率快照
    :param require_data: 是否拒绝空快照或没有可执行语句的数据
    :return: 格式与计数错误列表；空列表表示有效
    """
    problems: list[str] = []
    if not isinstance(current, Mapping):
        return ["覆盖率快照必须是对象"]
    if current.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"覆盖率快照 schema_version 必须是 {SCHEMA_VERSION}")
    if current.get("scope") != {"root": SCOPE_ROOT, "excluded": ["app/plugins"]}:
        problems.append("覆盖率快照 scope 必须覆盖 app 并排除 app/plugins")
    files = current.get("files")
    if not isinstance(files, Mapping):
        problems.append("覆盖率快照缺少 files 对象")
        return problems
    if require_data and not files:
        problems.append("覆盖率报告缺少受治理文件")
    for path, values in files.items():
        if not isinstance(path, str):
            problems.append("覆盖率快照 files 键必须是字符串路径")
            continue
        if not _is_governed_path(path):
            problems.append(f"{path}: 路径不在覆盖率治理范围")
            continue
        problems.extend(_validate_metric(values, name=path))
    packages = current.get("packages")
    if not isinstance(packages, Mapping):
        problems.append("覆盖率快照缺少 packages 对象")
    else:
        for name, values in packages.items():
            if not isinstance(name, str):
                problems.append("packages 键必须是字符串")
                continue
            problems.extend(_validate_metric(values, name=f"packages.{name}"))
    total = current.get("total")
    problems.extend(_validate_metric(total, name="total"))
    if require_data and isinstance(total, Mapping) and total.get("statements") == 0:
        problems.append("覆盖率报告语句数必须大于 0")
    if isinstance(files, Mapping) and isinstance(packages, Mapping) and isinstance(total, Mapping):
        expected_packages: dict[str, dict[str, Mapping[str, int | float]]] = {}
        for path, values in files.items():
            if not isinstance(path, str) or not isinstance(values, Mapping):
                continue
            expected_packages.setdefault(_package_name(path), {})[path] = values
        for name in sorted(set(packages) - set(expected_packages)):
            problems.append(f"packages.{name}: 包级摘要没有对应文件")
        for name, values in expected_packages.items():
            if packages.get(name) != _aggregate(values):
                problems.append(f"packages.{name}: 聚合结果与 files 不一致")
        if dict(total) != _aggregate(files):
            problems.append("total: 聚合结果与 files 不一致")
    return problems


def _file_ratio_is_lower(
    previous: Mapping[str, int | float], current: Mapping[str, int | float]
) -> bool:
    """
    使用真实计数比较两个文件的覆盖率，避免四舍五入误判。
    """
    previous_statements = int(previous["statements"])
    current_statements = int(current["statements"])
    if previous_statements == 0:
        return False
    if current_statements == 0:
        return True
    return int(current["covered_lines"]) * previous_statements < int(
        previous["covered_lines"]
    ) * current_statements


def _fewer_statements_without_new_misses(
    previous: Mapping[str, int | float], current: Mapping[str, int | float]
) -> bool:
    """
    允许语句减少且未覆盖行数不增加造成的比例下降。
    """
    previous_statements = int(previous["statements"])
    current_statements = int(current["statements"])
    return (
        current_statements < previous_statements
        and current_statements - int(current["covered_lines"])
        <= previous_statements - int(previous["covered_lines"])
    )


def classify_coverage(
    baseline: Mapping[str, Any], current: Mapping[str, Any]
) -> tuple[list[str], list[str]]:
    """
    拒绝存量文件新增未覆盖行造成的回退，并要求新文件达到 80%。

    :param baseline: 已确认的逐文件覆盖率低水位
    :param current: 当前逐文件覆盖率快照
    :return: 回退问题与可提升低水位的条目两个列表
    """
    regressions: list[str] = []
    improvements: list[str] = []
    baseline_files = baseline.get("files", {})
    current_files = current.get("files", {})
    if not isinstance(baseline_files, Mapping) or not isinstance(current_files, Mapping):
        return ["Coverage 基线或当前报告缺少 files 对象"], []
    for path, values in current_files.items():
        previous = baseline_files.get(path)
        if previous is None:
            statements = int(values["statements"])
            covered = int(values["covered_lines"])
            if covered * 100 < NEW_FILE_COVERAGE_PERCENT * statements:
                regressions.append(
                    f"{path}: 新文件覆盖率低于 {NEW_FILE_COVERAGE_PERCENT:.0f}%"
                    f"->{float(values['percent']):.2f}%"
                )
            else:
                improvements.append(f"{path}: 新文件达到覆盖率门槛")
        elif _file_ratio_is_lower(previous, values) and not _fewer_statements_without_new_misses(
            previous, values
        ):
            regressions.append(
                f"{path}: 存量覆盖率下降 "
                f"{float(previous['percent']):.2f}%->{float(values['percent']):.2f}%"
            )
        elif _file_ratio_is_lower(values, previous):
            improvements.append(f"{path}: 覆盖率上升")
    return regressions, improvements


def compare_coverage(
    baseline: Mapping[str, Any], current: Mapping[str, Any]
) -> list[str]:
    """
    返回存量覆盖率回退或新文件低于 80% 的问题。
    """
    regressions, _ = classify_coverage(baseline, current)
    return regressions


def _merge_baseline(
    baseline: Mapping[str, Any], current: Mapping[str, Any]
) -> dict[str, Any]:
    """
    使用同一份当前计数生成逐文件和汇总低水位；调用方已拒绝覆盖率回退。
    """
    regressions = compare_coverage(baseline, current)
    if regressions:
        raise ValueError("拒绝写入覆盖率回退：" + "; ".join(regressions))
    return _snapshot_from_files(current["files"])


def main() -> int:
    """
    检查 coverage JSON，或在无回退时显式刷新逐文件低水位。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="写入当前覆盖率低水位")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    args = parser.parse_args()
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        current = collect_package_coverage(report)
    except (OSError, ValueError, TypeError) as error:
        print(f"Coverage 报告无效：{error}")
        return 1
    if args.report.resolve() == DEFAULT_REPORT.resolve():
        source_paths = {
            path.relative_to(PROJECT_ROOT).as_posix()
            for path in (PROJECT_ROOT / SCOPE_ROOT).rglob("*.py")
            if path.relative_to(PROJECT_ROOT).parts[:2] != ("app", "plugins")
        }
        missing_source_paths = source_paths - set(current["files"])
        stale_report_paths = set(current["files"]) - source_paths
        if os.environ.get("CI", "").lower() == "true" and (
            missing_source_paths or stale_report_paths
        ):
            print(
                "Coverage 报告未完整覆盖当前 app 源码，"
                "CI 只接受 canonical 全量报告。"
            )
            return 1
        if args.write and (missing_source_paths or stale_report_paths):
            print(
                "拒绝写入：默认 coverage 报告未完整覆盖当前 app 源码，"
                "请使用 canonical 全量报告。"
            )
            return 1
        current_files = {
            path: values
            for path, values in current["files"].items()
            if path in source_paths
        }
        current = _snapshot_from_files(current_files)
    validation_problems = validate_coverage(current, require_data=True)
    if validation_problems:
        print("\n".join(validation_problems))
        return 1

    if args.baseline.exists():
        try:
            baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as error:
            print(f"Coverage 基线无效：{error}")
            return 1
        baseline_problems = validate_coverage(baseline)
        if baseline_problems:
            print("Coverage 基线无效：")
            print("\n".join(baseline_problems))
            return 1
    else:
        baseline = {
            "schema_version": SCHEMA_VERSION,
            "scope": {"root": SCOPE_ROOT, "excluded": ["app/plugins"]},
            "files": {},
        }

    regressions, _ = classify_coverage(baseline, current)
    if regressions:
        print("\n".join(regressions))
        if args.write:
            print("拒绝写入：覆盖率低于既有低水位或新文件 80% 门槛。")
        return 1
    if args.write:
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(
            json.dumps(
                _merge_baseline(baseline, current), ensure_ascii=False, indent=2, sort_keys=True
            )
            + "\n",
            encoding="utf-8",
        )
        display_path = (
            args.baseline.relative_to(PROJECT_ROOT)
            if args.baseline.is_relative_to(PROJECT_ROOT)
            else args.baseline
        )
        print(f"已写入 {display_path}")
        return 0
    total = current["total"]
    print(
        "覆盖率 ratchet 通过（全 app 排除 plugins："
        f"{total['percent']:.2f}%，文件 {len(current['files'])} 个）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
