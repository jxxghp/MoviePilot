"""为宿主源码维护 mypy 类型错误只降不增的基线。

与 ``complexity.py`` 同一模式：AST/subprocess 收集当前计数，与 JSON 基线对比，
删除和减少均合法，新增文件或错误码、既有计数增长都会被拒绝。
基线按 文件 -> 错误码 -> 数量 三级组织，修复单个文件后可用 ``--write`` 收紧基线，
不影响其他文件的存量豁免。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASELINE = PROJECT_ROOT / "tests/fixtures/architecture/mypy-baseline.json"

# 只扫描宿主源码；app/plugins 是运行时插件副本，质量由插件市场链路自行管理。
MYPY_TARGETS = ("app",)
MYPY_EXCLUDES = ("app/plugins",)

# 形如 app/foo.py:12: error: 消息说明 [error-code]；个别错误可能缺代码。
_ERROR_LINE = re.compile(r"^(?P<path>.+?):\d+(?::\d+)?: error: .+?(?:\s+\[(?P<code>[a-z0-9-]+)\])?$")


def run_mypy() -> str:
    """以当前解释器运行全量 mypy 并返回 stdout（非零退出码属于预期结果）。"""
    command = [sys.executable, "-m", "mypy", *MYPY_TARGETS]
    for pattern in MYPY_EXCLUDES:
        command += ["--exclude", pattern]
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout


def parse_errors(output: str) -> dict[str, dict[str, int]]:
    """把 mypy 输出聚合为 文件 -> 错误码 -> 数量 的精确计数。"""
    report: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for raw_line in output.splitlines():
        line = raw_line.strip()
        # 排除摘要行与源码上下文片段，只匹配真实错误行。
        if ": error:" not in line:
            continue
        match = _ERROR_LINE.match(line)
        if not match:
            continue
        path = match.group("path").replace("\\", "/")
        code = match.group("code") or "unknown"
        report[path][code] += 1
    return {path: dict(codes) for path, codes in sorted(report.items())}


def compare_counts(
    baseline: dict[str, dict[str, int]], current: dict[str, dict[str, int]]
) -> list[str]:
    """返回新增文件/错误码或既有计数增长问题；删除与减少均合法。"""
    problems = []
    for path, codes in current.items():
        previous = baseline.get(path, {})
        for code, count in codes.items():
            if code not in previous:
                problems.append(f"{path}: 新增类型错误 [{code}] x{count}")
            elif count > previous[code]:
                problems.append(f"{path}: 既有错误增长 [{code}] {previous[code]}->{count}")
    return problems


def main() -> int:
    """执行 mypy baseline check 或显式 write。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="写入当前类型错误基线")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    args = parser.parse_args()
    current = parse_errors(run_mypy())
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
    print(f"mypy ratchet 通过（存量 {total} 个类型错误，只降不增）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
