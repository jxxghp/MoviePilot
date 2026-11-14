"""
后端单测入口：默认并行执行文件分片，也支持单分片和显式串行调试。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Sequence

import pytest

TESTS_DIR = Path(__file__).resolve().parent
RUNNER_PATH = Path(__file__).resolve()
# 默认启动 4 个独立测试进程，每个进程使用自己的隔离配置目录。
DEFAULT_SHARD_COUNT = 4
DURATIONS_PATH = TESTS_DIR / "fixtures" / "durations.json"
# 缺少历史耗时的文件按 1 秒参与分片负载估算。
DEFAULT_TEST_DURATION = 1.0
# CI 架构门禁任务单独运行的测试文件；分片使用 --exclude-architecture-gate 时不再重复执行
ARCHITECTURE_GATE_TESTS = (
    "test_architecture_dependencies.py",
    "test_architecture_adapter_imports.py",
    "test_architecture_egress.py",
    "test_architecture_event_facts.py",
    "test_architecture_event_policy.py",
    "test_architecture_contract_baseline.py",
    "test_architecture_baseline_cli.py",
)


def collect_test_files(exclude_architecture_gate: bool = False) -> list[Path]:
    """
    按稳定路径顺序收集测试根目录中的测试文件。

    :param exclude_architecture_gate: 是否排除已由 CI 架构门禁单独运行的文件
    :return: 排序后的 test_*.py 路径；不递归子目录
    """
    excluded = set(ARCHITECTURE_GATE_TESTS) if exclude_architecture_gate else set()
    return sorted(path for path in TESTS_DIR.glob("test_*.py") if path.name not in excluded)


def load_test_durations() -> dict[str, float]:
    """
    读取版本控制中的测试文件耗时估算。

    不使用本机运行缓存，确保本地与 CI 对相同版本作出相同分片决策。

    :return: 测试文件名到预计秒数的映射
    """
    return json.loads(DURATIONS_PATH.read_text(encoding="utf-8"))["files"]


def split_test_files(
    test_files: Sequence[Path], shard_count: int,
    durations: Mapping[str, float] | None = None,
) -> list[list[Path]]:
    """
    按耗时从长到短分配文件，优先填入预计最短的分片。

    未记录耗时的文件按 1 秒估算；耗时相同时按路径和较小分片编号稳定选择。

    :raises ValueError: 分片数小于等于 0
    :param test_files: 待分片的测试文件路径
    :param shard_count: 目标分片数，必须大于 0
    :param durations: 文件名到预计秒数的映射；None 时读取仓库中的耗时数据
    :return: 按分片编号排列的文件列表，每个分片内部再按路径排序
    """
    if shard_count <= 0:
        raise ValueError("shard_count 必须大于 0")
    durations = load_test_durations() if durations is None else durations
    shards: list[list[Path]] = [[] for _ in range(shard_count)]
    shard_durations = [0.0] * shard_count
    weighted_files = sorted(
        test_files,
        key=lambda path: (-durations.get(path.name, DEFAULT_TEST_DURATION), path),
    )
    for test_file in weighted_files:
        shard_index = min(range(shard_count), key=shard_durations.__getitem__)
        shards[shard_index].append(test_file)
        shard_durations[shard_index] += durations.get(test_file.name, DEFAULT_TEST_DURATION)
    return [sorted(shard) for shard in shards]


def parse_shard(value: str) -> tuple[int, int]:
    """
    解析从 1 开始编号的 N/TOTAL 分片标识。

    :raises argparse.ArgumentTypeError: 格式无效或不满足 1 <= N <= TOTAL
    :param value: 分片编号及总数文本，例如 2/4
    :return: 分片编号与总数
    """
    try:
        index_text, count_text = value.split("/", maxsplit=1)
        index = int(index_text)
        count = int(count_text)
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError("--shard 必须使用 N/TOTAL 格式") from error
    if count <= 0 or not 1 <= index <= count:
        raise argparse.ArgumentTypeError("--shard 必须满足 1 <= N <= TOTAL")
    return index, count


def parse_args(argv: Sequence[str]) -> tuple[argparse.Namespace, list[str]]:
    """
    解析测试入口选项，将未识别选项留给 pytest。

    串行模式不能同时指定分片或排除架构门禁；参数错误由 argparse 终止并报告。

    :param argv: 不含脚本名称的命令行参数
    :return: 入口选项对象和待透传的 pytest 参数列表
    """
    parser = argparse.ArgumentParser(
        description="默认以 4 个独立 pytest 文件分片并行运行后端全量测试。"
    )
    parser.add_argument(
        "--serial",
        action="store_true",
        help="在当前进程串行运行 tests 目录，适合断点和顺序污染调试。",
    )
    parser.add_argument(
        "--shard",
        type=parse_shard,
        metavar="N/TOTAL",
        help="只运行指定文件分片；CI 使用同一参数启动独立 job。",
    )
    parser.add_argument(
        "--exclude-architecture-gate",
        action="store_true",
        help="跳过 CI 架构门禁已运行的测试文件，供覆盖率分片使用。",
    )
    args, pytest_args = parser.parse_known_args(argv)
    if args.serial and args.shard is not None:
        parser.error("--serial 不能与 --shard 同时使用")
    if args.serial and args.exclude_architecture_gate:
        parser.error("--serial 不能与 --exclude-architecture-gate 同时使用")
    return args, pytest_args


def run_pytest(paths: Sequence[Path], pytest_args: Sequence[str]) -> int:
    """
    在当前进程调用 pytest 执行指定路径。

    :param paths: 完整测试目录或选定分片的测试文件
    :param pytest_args: 原样附加的 pytest 参数
    :return: pytest 返回的退出状态码
    """
    return pytest.main([*(str(path) for path in paths), *pytest_args])


def _worker_command(
    shard_index: int, shard_count: int, pytest_args: Sequence[str],
    exclude_architecture_gate: bool = False,
) -> list[str]:
    """
    生成与 CI 使用相同分片算法的子进程命令。

    子进程按编号重新计算文件分片，排除选项必须与父进程一致。

    :param shard_index: 从 1 开始的分片编号
    :param shard_count: 总分片数
    :param pytest_args: 透传给子进程 pytest 的参数
    :param exclude_architecture_gate: 是否在子进程中同样排除架构门禁文件
    :return: 使用当前 Python 解释器的命令参数列表
    """
    return [
        sys.executable,
        str(RUNNER_PATH),
        "--shard",
        f"{shard_index}/{shard_count}",
        *(["--exclude-architecture-gate"] if exclude_architecture_gate else []),
        *pytest_args,
    ]


def run_parallel_shards(
    shards: Sequence[Sequence[Path]], pytest_args: Sequence[str],
    exclude_architecture_gate: bool = False,
) -> int:
    """
    为非空分片启动独立 pytest 进程并等待全部结束。

    收到键盘中断后终止仍在运行的子进程，并等待它们退出。

    :param shards: 用于启动和展示的分片列表；子进程依同一算法重新计算文件清单
    :param pytest_args: 传递给每个子进程的 pytest 参数
    :param exclude_architecture_gate: 父子进程共同使用的架构测试排除选项
    :return: 全部成功时为 0，否则返回按分片顺序遇到的首个非零退出码；中断时为 130
    """
    shard_count = len(shards)
    processes: list[tuple[int, subprocess.Popen]] = []
    for shard_index, shard in enumerate(shards, start=1):
        if not shard:
            continue
        print(
            f"启动测试分片 {shard_index}/{shard_count}：{len(shard)} 个文件",
            flush=True,
        )
        processes.append((
            shard_index,
            subprocess.Popen(_worker_command(
                shard_index, shard_count, pytest_args, exclude_architecture_gate,
            )),
        ))

    exit_code = 0
    try:
        for shard_index, process in processes:
            return_code = process.wait()
            if return_code != 0:
                print(
                    f"测试分片 {shard_index}/{shard_count} 失败，退出码 {return_code}",
                    file=sys.stderr,
                    flush=True,
                )
                exit_code = exit_code or return_code
    except KeyboardInterrupt:
        for _, process in processes:
            if process.poll() is None:
                process.terminate()
        for _, process in processes:
            process.wait()
        return 130
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    """
    选择串行全量、指定分片或默认四分片并行执行。

    :param argv: 入口参数；None 时读取 sys.argv，空列表表示不传额外参数
    :return: 执行状态码；没有测试文件或所选分片为空时为 2
    """
    args, pytest_args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.serial:
        return run_pytest([TESTS_DIR], pytest_args)

    test_files = collect_test_files(args.exclude_architecture_gate)
    if not test_files:
        print(f"未在 {TESTS_DIR} 找到 test_*.py", file=sys.stderr)
        return 2

    if args.shard is not None:
        shard_index, shard_count = args.shard
        selected = split_test_files(test_files, shard_count)[shard_index - 1]
        if not selected:
            print(f"测试分片 {shard_index}/{shard_count} 为空", file=sys.stderr)
            return 2
        print(
            f"运行测试分片 {shard_index}/{shard_count}：{len(selected)} 个文件",
            flush=True,
        )
        return run_pytest(selected, pytest_args)

    shards = split_test_files(test_files, DEFAULT_SHARD_COUNT)
    return run_parallel_shards(shards, pytest_args, args.exclude_architecture_gate)


if __name__ == "__main__":
    sys.exit(main())
