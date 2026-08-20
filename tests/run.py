"""后端单测入口：默认并行执行文件分片，也支持单分片和显式串行调试。"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Sequence

import pytest

TESTS_DIR = Path(__file__).resolve().parent
RUNNER_PATH = Path(__file__).resolve()
DEFAULT_SHARD_COUNT = 4


def collect_test_files() -> list[Path]:
    """按稳定路径顺序返回根测试目录中的全部测试文件。"""
    return sorted(TESTS_DIR.glob("test_*.py"))


def split_test_files(
    test_files: Sequence[Path], shard_count: int
) -> list[list[Path]]:
    """把排序后的文件连续均分，保持 CI 分片归属稳定且易于复现。"""
    if shard_count <= 0:
        raise ValueError("shard_count 必须大于 0")
    shard_size = (len(test_files) + shard_count - 1) // shard_count
    if shard_size == 0:
        return [[] for _ in range(shard_count)]
    shards = [
        list(test_files[start:start + shard_size])
        for start in range(0, len(test_files), shard_size)
    ]
    return shards + [[] for _ in range(shard_count - len(shards))]


def parse_shard(value: str) -> tuple[int, int]:
    """解析一基的 ``N/TOTAL`` 分片标识，供本地与 CI 共享稳定参数。"""
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
    """解析 runner 参数，其余参数原样传给 pytest。"""
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
    args, pytest_args = parser.parse_known_args(argv)
    if args.serial and args.shard is not None:
        parser.error("--serial 不能与 --shard 同时使用")
    return args, pytest_args


def run_pytest(paths: Sequence[Path], pytest_args: Sequence[str]) -> int:
    """在当前进程运行完整目录或一个文件分片。"""
    return pytest.main([*(str(path) for path in paths), *pytest_args])


def _worker_command(
    shard_index: int, shard_count: int, pytest_args: Sequence[str]
) -> list[str]:
    """构造与 CI 完全相同的单分片 worker 命令。"""
    return [
        sys.executable,
        str(RUNNER_PATH),
        "--shard",
        f"{shard_index}/{shard_count}",
        *pytest_args,
    ]


def run_parallel_shards(
    shards: Sequence[Sequence[Path]], pytest_args: Sequence[str]
) -> int:
    """启动独立 pytest 进程并等待全部文件分片结束。"""
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
            subprocess.Popen(_worker_command(shard_index, shard_count, pytest_args)),
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
    """执行串行全量、指定单分片或默认四分片并行全量。"""
    args, pytest_args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.serial:
        return run_pytest([TESTS_DIR], pytest_args)

    test_files = collect_test_files()
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
    return run_parallel_shards(shards, pytest_args)


if __name__ == "__main__":
    sys.exit(main())
