"""
为宿主源码维护 mypy 存量债务例外清单，并保证新增文件、新增内容清零。

门禁口径（本脚本即为口径来源）：

1. 例外：基线中登记的文件是“当前未完成”的存量债务，按文件 + 错误码记录上限。
2. 新增文件：基线未登记的文件必须零诊断，出现任何错误即失败。
3. 新增内容：例外文件内任一错误码数量不得超过登记上限。
4. 只降不增：债务下降后必须用 --write 固化低水位，避免已修复的额度被重新占用。

基线因此不是“允许失败”的白名单，而是逐文件、逐错误码的债务上限快照。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import NamedTuple

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASELINE = PROJECT_ROOT / "tests/fixtures/architecture/mypy-baseline.json"

# 只扫描宿主源码；app/plugins 是运行时插件副本，质量由插件市场链路自行管理。
MYPY_TARGETS = ("app",)
MYPY_EXCLUDES = ("app/plugins",)
# 与 Linux CI 保持相同平台分支，避免本机平台改变诊断基线。
MYPY_PLATFORM = "linux"


class Migration(NamedTuple):
    """
    一次已登记的单体文件拆分。

    额度只允许转移给 targets 中逐一登记的接收文件；同包内后来新增的文件不在
    名单内，仍按“新增文件必须清零”独立判定，避免新文件吸收旧文件的存量额度。
    """

    sources: tuple[str, ...]
    targets: tuple[str, ...]


# 只在旧路径仍留在基线、当前诊断已全部落到新路径时生效，属于一次性迁移登记。
# 额度转移按错误码汇总比较，不据此推断旧源码文件已经删除。
MYPY_MIGRATIONS: dict[str, Migration] = {
    "subscribe-package": Migration(
        sources=("app/chain/subscribe.py",),
        targets=(
            "app/chain/subscribe/completion.py",
            "app/chain/subscribe/create.py",
            "app/chain/subscribe/facade.py",
            "app/chain/subscribe/match.py",
            "app/chain/subscribe/query.py",
            "app/chain/subscribe/refresh.py",
        ),
    ),
    "transfer-package": Migration(
        sources=("app/chain/transfer.py", "app/chain/_transfer.py"),
        targets=(
            "app/chain/transfer/execution.py",
            "app/chain/transfer/queue.py",
            "app/chain/transfer/records.py",
            "app/chain/transfer/request.py",
            "app/chain/transfer/retry.py",
            "app/chain/transfer/settlement.py",
            "app/chain/transfer/workflow.py",
        ),
    ),
    "plugin-management": Migration(
        sources=("app/agent/tools/impl/_plugin_tool_utils.py",),
        targets=("app/application/plugin/management.py",),
    ),
}

# 形如 app/foo.py:12: error: 消息说明 [error-code]；个别错误可能缺代码。
_ERROR_LINE = re.compile(r"^(?P<path>.+?):\d+(?::\d+)?: error: .+?(?:\s+\[(?P<code>[a-z0-9-]+)\])?$")
_ERROR_SUMMARY = re.compile(r"^Found (?P<count>\d+) errors? in ")
# 类型桩缺失属于环境问题，不是代码债务；必须报错而不是计成新增错误。
_MISSING_STUB = re.compile(r'Library stubs not installed for "(?P<module>[^"]+)"')


class RatchetDiff(NamedTuple):
    """
    三类差异分别对应不同的处置方式，不能混在一条输出里。

    new_files：基线未登记的文件出现诊断，必须清零。
    growth：例外文件超出登记上限，必须消除新增的类型错误。
    stale：债务下降但未固化，必须用 --write 写入新低水位。
    """

    new_files: list[str]
    growth: list[str]
    stale: list[str]

    @property
    def blocking(self) -> list[str]:
        """返回需要消除而不是写入基线的差异。"""
        return [*self.new_files, *self.growth]


def run_mypy() -> str:
    """
    运行全量 mypy；只接受可完整解析的正常成功或诊断结果。

    :return: 已通过退出状态、类型桩和摘要计数核对的 mypy 标准输出
    :raises RuntimeError: 工具异常退出、写入标准错误、缺少类型桩或输出摘要不完整
    """
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
    _reject_missing_stubs(result.stdout)

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


def _reject_missing_stubs(output: str) -> None:
    """
    拒绝把缺失的类型桩计成类型债务。

    缺少 types-* 时，mypy 会把带类型的第三方库当作 Any，既产生 import-untyped，
    也会连带出现 no-any-return 等派生诊断；这类结果写入基线等于凭空放宽门禁。
    """
    modules = sorted(
        {
            match.group("module")
            for line in output.splitlines()
            if (match := _MISSING_STUB.search(line))
        }
    )
    if not modules:
        return
    raise RuntimeError(
        "检测到缺失的类型桩包："
        + ", ".join(modules)
        + "；请先安装 pyproject.toml 声明的 types-* 依赖（uv sync --locked），"
        "门禁不把环境缺失计为类型债务。"
    )


def parse_errors(output: str) -> dict[str, dict[str, int]]:
    """
    把 mypy 输出聚合为文件 -> 错误码 -> 数量的精确计数。
    """
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
    baseline: dict[str, dict[str, int]],
    current: dict[str, dict[str, int]],
    migrations: dict[str, Migration] | None = None,
) -> RatchetDiff:
    """
    把计数差异分为新增文件、例外增长和待固化低水位三类。
    """
    baseline, current = _fold_migrations(
        baseline, current, MYPY_MIGRATIONS if migrations is None else migrations
    )
    new_files: list[str] = []
    growth: list[str] = []
    stale: list[str] = []
    for path in sorted(baseline.keys() | current.keys()):
        previous = baseline.get(path, {})
        latest = current.get(path, {})
        if not previous and latest:
            # 基线未登记即新增文件：不允许以任何存量额度抵扣。
            new_files.append(
                f"{path}: 新增文件必须清零类型错误（"
                + "，".join(f"[{code}] x{count}" for code, count in sorted(latest.items()))
                + "）"
            )
            continue
        for code in sorted(previous.keys() | latest.keys()):
            old_count = previous.get(code, 0)
            new_count = latest.get(code, 0)
            if new_count > old_count:
                if old_count == 0:
                    growth.append(f"{path}: 新增类型错误 [{code}] x{new_count}")
                else:
                    growth.append(
                        f"{path}: 既有错误增长 [{code}] {old_count}->{new_count}"
                    )
            elif new_count < old_count:
                stale.append(
                    f"{path}: 类型错误低水位未固化 [{code}] {old_count}->{new_count}"
                )
    return RatchetDiff(new_files=new_files, growth=growth, stale=stale)


def _fold_migrations(
    baseline: dict[str, dict[str, int]],
    current: dict[str, dict[str, int]],
    migrations: dict[str, Migration],
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    """
    把退役文件的诊断与已登记的接收文件折叠为一次性迁移归属。

    只有旧路径仍在基线、且当前诊断已不再包含旧路径时才折叠；接收文件必须逐一
    登记在 Migration.targets 中，同包内其他文件继续按新增文件独立判定。
    """
    folded_baseline = {path: dict(codes) for path, codes in baseline.items()}
    folded_current = {path: dict(codes) for path, codes in current.items()}
    for name, migration in migrations.items():
        if not any(path in folded_baseline for path in migration.sources):
            continue
        if any(path in current for path in migration.sources):
            continue
        targets = tuple(
            path
            for path in migration.targets
            if path in folded_current or path in folded_baseline
        )
        if not targets:
            continue
        previous: Counter[str] = Counter()
        latest: Counter[str] = Counter()
        for path in migration.sources:
            previous.update(folded_baseline.pop(path, {}))
        for path in targets:
            latest.update(folded_current.pop(path, {}))
        migration_key = f"<path-migration:{name}>"
        folded_baseline[migration_key] = dict(previous)
        folded_current[migration_key] = dict(latest)
    return folded_baseline, folded_current


def compare_counts(
    baseline: dict[str, dict[str, int]],
    current: dict[str, dict[str, int]],
    migrations: dict[str, Migration] | None = None,
) -> list[str]:
    """
    返回阻断性问题与尚未固化的新低水位，供调用方逐条输出。
    """
    diff = classify_counts(baseline, current, migrations)
    return [*diff.blocking, *diff.stale]


def main() -> int:
    """
    执行 mypy 例外清单检查或显式写入。
    """
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
    diff = classify_counts(baseline, current)
    if args.write:
        if baseline_exists and diff.blocking:
            _print_diff(diff)
            print("拒绝写入：当前结果包含新增文件或类型错误回退，--write 只能固化下降后的低水位。")
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
    if diff.blocking:
        _print_diff(diff)
        print("新增文件必须清零，例外文件不得增长；存在增长时禁止用 --write 覆盖基线。")
        return 1
    if diff.stale:
        _print_diff(diff)
        print("提示：当前只有债务下降，可用 --write 固化新的低水位。")
        return 1
    total = sum(count for codes in current.values() for count in codes.values())
    print(f"mypy ratchet 通过（存量例外已同步：{total} 个类型错误）")
    return 0


def _print_diff(diff: RatchetDiff) -> None:
    """
    按处置方式分组输出，避免把"新增文件"和"存量增长"混成同一条诊断。
    """
    sections = (
        ("新增文件必须清零：", diff.new_files),
        ("例外文件出现新增类型错误：", diff.growth),
        ("类型错误低水位未固化：", diff.stale),
    )
    for title, items in sections:
        if not items:
            continue
        print(title)
        print("\n".join(items))


if __name__ == "__main__":
    raise SystemExit(main())
