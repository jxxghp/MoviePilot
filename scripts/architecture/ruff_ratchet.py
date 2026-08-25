"""维护 Ruff 诊断只降不增的全仓基线。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASELINE = PROJECT_ROOT / "tests/fixtures/architecture/ruff-baseline.json"
RUFF_TARGETS = ("app", "tests", "scripts")


def run_ruff() -> list[dict[str, Any]]:
    """运行 Ruff 并返回结构化诊断；发现存量问题时非零退出属于预期。"""
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", *RUFF_TARGETS, "--output-format", "json"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise RuntimeError(result.stderr or result.stdout or "Ruff 执行失败")
    return json.loads(result.stdout or "[]")


def aggregate_diagnostics(
    diagnostics: list[dict[str, Any]],
) -> dict[str, dict[str, int]]:
    """把 Ruff 诊断聚合为文件、规则和数量三级基线。"""
    report: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for diagnostic in diagnostics:
        path = Path(diagnostic["filename"]).resolve().relative_to(PROJECT_ROOT).as_posix()
        report[path][diagnostic["code"]] += 1
    return {path: dict(sorted(codes.items())) for path, codes in sorted(report.items())}


def compare_counts(
    baseline: dict[str, dict[str, int]],
    current: dict[str, dict[str, int]],
) -> list[str]:
    """返回新增规则或既有数量增长；修复和删除均合法。"""
    problems = []
    for path, codes in current.items():
        previous = baseline.get(path, {})
        for code, count in codes.items():
            if code not in previous:
                problems.append(f"{path}: 新增 Ruff 诊断 [{code}] x{count}")
            elif count > previous[code]:
                problems.append(
                    f"{path}: Ruff 诊断增长 [{code}] {previous[code]}->{count}"
                )
    return problems


def main() -> int:
    """执行 Ruff baseline check 或显式 write。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="写入当前 Ruff 基线")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    args = parser.parse_args()
    current = aggregate_diagnostics(run_ruff())
    if args.write:
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(
            json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"已写入 {args.baseline.relative_to(PROJECT_ROOT)}")
        return 0
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    problems = compare_counts(baseline, current)
    if problems:
        print("\n".join(problems))
        print("提示：修复后可用 --write 收紧基线；禁止为绕过门禁放宽基线。")
        return 1
    total = sum(count for codes in current.values() for count in codes.values())
    print(f"Ruff ratchet 通过（存量 {total} 个诊断，只降不增）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
