"""对 Application 与 Domain 维护不可退化的行覆盖率阈值。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORT = PROJECT_ROOT / "coverage.json"
DEFAULT_BASELINE = PROJECT_ROOT / "tests/fixtures/architecture/coverage-baseline.json"
PACKAGE_PREFIXES = {
    "application": "app/application/",
    "domain": "app/domain/",
}


def collect_package_coverage(report: dict[str, Any]) -> dict[str, dict[str, int | float]]:
    """按治理包聚合 coverage.py JSON 中的语句和已覆盖行。"""
    result: dict[str, dict[str, int | float]] = {}
    files = report.get("files", {})
    for name, prefix in PACKAGE_PREFIXES.items():
        statements = 0
        covered = 0
        for path, details in files.items():
            if not path.replace("\\", "/").startswith(prefix):
                continue
            summary = details["summary"]
            statements += int(summary["num_statements"])
            covered += int(summary["covered_lines"])
        percent = round(covered * 100 / statements, 2) if statements else 100.0
        result[name] = {
            "statements": statements,
            "covered_lines": covered,
            "percent": percent,
        }
    return result


def compare_coverage(
    baseline: dict[str, dict[str, int | float]],
    current: dict[str, dict[str, int | float]],
) -> list[str]:
    """返回包覆盖率低于已提交阈值的问题。"""
    problems = []
    for name in PACKAGE_PREFIXES:
        expected = float(baseline[name]["percent"])
        actual = float(current[name]["percent"])
        if actual < expected:
            problems.append(f"{name}: 行覆盖率下降 {expected:.2f}%->{actual:.2f}%")
    return problems


def main() -> int:
    """检查 coverage JSON，或显式刷新当前阈值。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="写入当前覆盖率阈值")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    current = collect_package_coverage(report)
    if args.write:
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(
            json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"已写入 {args.baseline.relative_to(PROJECT_ROOT)}")
        return 0
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    problems = compare_coverage(baseline, current)
    if problems:
        print("\n".join(problems))
        return 1
    summary = ", ".join(
        f"{name}={values['percent']:.2f}%" for name, values in current.items()
    )
    print(f"覆盖率 ratchet 通过（{summary}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
