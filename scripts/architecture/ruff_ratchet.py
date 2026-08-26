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
    diagnostics = json.loads(result.stdout or "[]")
    if not isinstance(diagnostics, list):
        raise RuntimeError("Ruff JSON 输出不是诊断列表")
    if result.returncode == 0 and diagnostics:
        raise RuntimeError("Ruff 返回成功但仍输出诊断")
    if result.returncode == 1 and not diagnostics:
        raise RuntimeError("Ruff 返回诊断状态但输出为空")
    return diagnostics


def aggregate_diagnostics(
    diagnostics: list[dict[str, Any]],
) -> dict[str, dict[str, int]]:
    """把 Ruff 诊断聚合为文件、规则和数量三级基线。"""
    report: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for diagnostic in diagnostics:
        path = Path(diagnostic["filename"]).resolve().relative_to(PROJECT_ROOT).as_posix()
        report[path][diagnostic["code"]] += 1
    return {path: dict(sorted(codes.items())) for path, codes in sorted(report.items())}


def classify_counts(
    baseline: dict[str, dict[str, int]],
    current: dict[str, dict[str, int]],
) -> tuple[list[str], list[str]]:
    """把计数差异分为不可写入的回退和可固化的低水位下降。"""
    regressions: list[str] = []
    stale: list[str] = []
    for path in sorted(baseline.keys() | current.keys()):
        previous = baseline.get(path, {})
        latest = current.get(path, {})
        for code in sorted(previous.keys() | latest.keys()):
            old_count = previous.get(code, 0)
            new_count = latest.get(code, 0)
            if new_count > old_count:
                if old_count == 0:
                    regressions.append(f"{path}: 新增 Ruff 诊断 [{code}] x{new_count}")
                else:
                    regressions.append(
                        f"{path}: Ruff 诊断增长 [{code}] {old_count}->{new_count}"
                    )
            elif new_count < old_count:
                stale.append(
                    f"{path}: Ruff 低水位未固化 [{code}] {old_count}->{new_count}"
                )
    return regressions, stale


def compare_counts(
    baseline: dict[str, dict[str, int]],
    current: dict[str, dict[str, int]],
) -> list[str]:
    """返回 Ruff 诊断增长和尚未固化的新低水位。"""
    regressions, stale = classify_counts(baseline, current)
    return [*regressions, *stale]


def main() -> int:
    """执行 Ruff baseline check 或显式 write。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="写入当前 Ruff 基线")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    args = parser.parse_args()
    current = aggregate_diagnostics(run_ruff())
    baseline_exists = args.baseline.exists()
    baseline = (
        json.loads(args.baseline.read_text(encoding="utf-8"))
        if baseline_exists
        else {}
    )
    regressions, stale = classify_counts(baseline, current)
    if args.write:
        if baseline_exists and regressions:
            print("\n".join(regressions))
            print("拒绝写入：当前结果包含 Ruff 回退，--write 只能固化下降后的低水位。")
            return 1
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(
            json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        display_path = (
            args.baseline.relative_to(PROJECT_ROOT)
            if args.baseline.is_relative_to(PROJECT_ROOT)
            else args.baseline
        )
        print(f"已写入 {display_path}")
        return 0
    problems = [*regressions, *stale]
    if problems:
        print("\n".join(problems))
        if regressions:
            print("先消除 Ruff 回退；存在增长时禁止用 --write 覆盖基线。")
        else:
            print("提示：当前只有债务下降，可用 --write 固化新的低水位。")
        return 1
    total = sum(count for codes in current.values() for count in codes.values())
    print(f"Ruff ratchet 通过（低水位已同步：{total} 个诊断）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
