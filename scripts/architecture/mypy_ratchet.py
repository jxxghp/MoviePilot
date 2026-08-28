"""为宿主源码维护 mypy 类型错误只降不增的基线。

与 ``complexity.py`` 同一模式：AST/subprocess 收集当前计数，与 JSON 基线对比，
基线按 文件 -> 错误码 -> 数量 三级组织。任何增长都会被拒绝；债务下降后也必须用
``--write`` 固化新的低水位，避免已经修复的错误重新获得回退额度。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASELINE = PROJECT_ROOT / "tests/fixtures/architecture/mypy-baseline.json"

# 只扫描宿主源码；app/plugins 是运行时插件副本，质量由插件市场链路自行管理。
MYPY_TARGETS = ("app",)
MYPY_EXCLUDES = ("app/plugins",)
MYPY_PLATFORM = "linux"

# 单体文件退役为同名 package 时，错误 owner 会从一个路径迁移到多个文件。迁移只在旧路径
# 已从当前源码消失时生效，并且仍按错误码聚合执行只降不增；其他文件继续使用原有逐路径门禁。
MYPY_PATH_MIGRATIONS = {
    "subscribe-package": (
        ("app/chain/subscribe.py",),
        ("app/chain/subscribe/",),
    ),
    "transfer-package": (
        ("app/chain/transfer.py", "app/chain/_transfer.py"),
        ("app/chain/transfer/",),
    ),
}

# 形如 app/foo.py:12: error: 消息说明 [error-code]；个别错误可能缺代码。
_ERROR_LINE = re.compile(r"^(?P<path>.+?):\d+(?::\d+)?: error: .+?(?:\s+\[(?P<code>[a-z0-9-]+)\])?$")
_ERROR_SUMMARY = re.compile(r"^Found (?P<count>\d+) errors? in ")


def run_mypy() -> str:
    """运行全量 mypy；只接受可完整解析的正常成功或诊断结果。"""
    command = [
        sys.executable,
        "-m",
        "mypy",
        "--no-incremental",
        "--no-pretty",
        "--platform",
        MYPY_PLATFORM,
        *MYPY_TARGETS,
    ]
    for pattern in MYPY_EXCLUDES:
        command += ["--exclude", pattern]
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in {0, 1}:
        details = result.stderr.strip() or result.stdout.strip() or "mypy 执行失败"
        raise RuntimeError(f"mypy 异常退出（{result.returncode}）：{details}")
    if result.stderr.strip():
        raise RuntimeError(f"mypy 在 stderr 输出异常：{result.stderr.strip()}")

    parsed_total = sum(
        count
        for codes in parse_errors(result.stdout).values()
        for count in codes.values()
    )
    summaries = [
        match
        for line in result.stdout.splitlines()
        if (match := _ERROR_SUMMARY.match(line.strip()))
    ]
    if result.returncode == 0:
        if parsed_total or not any(
            line.startswith("Success: no issues found")
            for line in result.stdout.splitlines()
        ):
            raise RuntimeError("mypy 成功输出缺少可验证摘要")
        return result.stdout
    if len(summaries) != 1:
        raise RuntimeError("mypy 错误输出缺少唯一的完整摘要")
    summary_total = int(summaries[0].group("count"))
    if summary_total != parsed_total:
        raise RuntimeError(
            f"mypy 摘要与已解析错误数不一致：摘要 {summary_total}，解析 {parsed_total}"
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


def classify_counts(
    baseline: dict[str, dict[str, int]], current: dict[str, dict[str, int]]
) -> tuple[list[str], list[str]]:
    """把计数差异分为不可写入的回退和可固化的低水位下降。"""
    baseline, current = _fold_path_migrations(baseline, current)
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
                    regressions.append(f"{path}: 新增类型错误 [{code}] x{new_count}")
                else:
                    regressions.append(
                        f"{path}: 既有错误增长 [{code}] {old_count}->{new_count}"
                    )
            elif new_count < old_count:
                stale.append(
                    f"{path}: 类型错误低水位未固化 [{code}] {old_count}->{new_count}"
                )
    return regressions, stale


def _fold_path_migrations(
    baseline: dict[str, dict[str, int]],
    current: dict[str, dict[str, int]],
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    """把已退役单文件与同名 package 折叠为按错误码比较的一次性迁移 owner。"""
    folded_baseline = {path: dict(codes) for path, codes in baseline.items()}
    folded_current = {path: dict(codes) for path, codes in current.items()}
    for name, (source_paths, target_prefixes) in MYPY_PATH_MIGRATIONS.items():
        if not any(path in folded_baseline for path in source_paths):
            continue
        if any(path in current for path in source_paths):
            continue
        target_paths = tuple(
            path
            for path in current
            if any(path.startswith(prefix) for prefix in target_prefixes)
        )
        if not target_paths:
            continue
        previous: Counter[str] = Counter()
        latest: Counter[str] = Counter()
        for path in source_paths:
            previous.update(folded_baseline.pop(path, {}))
        for path in target_paths:
            latest.update(folded_current.pop(path, {}))
        migration_key = f"<path-migration:{name}>"
        folded_baseline[migration_key] = dict(previous)
        folded_current[migration_key] = dict(latest)
    return folded_baseline, folded_current


def compare_counts(
    baseline: dict[str, dict[str, int]], current: dict[str, dict[str, int]]
) -> list[str]:
    """返回类型错误增长和尚未固化的新低水位。"""
    regressions, stale = classify_counts(baseline, current)
    return [*regressions, *stale]


def main() -> int:
    """执行 mypy baseline check 或显式 write。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="写入当前类型错误基线")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    args = parser.parse_args()
    current = parse_errors(run_mypy())
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
            print("拒绝写入：当前结果包含类型错误回退，--write 只能固化下降后的低水位。")
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
            print("先消除类型错误回退；存在增长时禁止用 --write 覆盖基线。")
        else:
            print("提示：当前只有债务下降，可用 --write 固化新的低水位。")
        return 1
    total = sum(count for codes in current.values() for count in codes.values())
    print(f"mypy ratchet 通过（低水位已同步：{total} 个类型错误）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
