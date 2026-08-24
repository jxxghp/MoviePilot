"""测量 TaskRegistry 跨线程 pending submission 的关停终态与提交吞吐。"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import statistics
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.runtime.tasks import TaskRegistry


async def _value(value: int) -> int:
    """返回输入值，提供最小可完成协程。"""
    return value


def pending_shutdown_probe() -> dict[str, object]:
    """在目标 loop 尚未分发 callback 时关闭 Registry，并报告提交终态。"""
    registry = TaskRegistry()
    target_loop = asyncio.new_event_loop()
    coroutine = _value(1)
    completion = registry.submit_threadsafe(
        coroutine,
        loop=target_loop,
        owner="probe.pending",
    )
    shutdown_result = asyncio.run(registry.shutdown(timeout_seconds=0.001))
    result = {
        "shutdown_result": shutdown_result,
        "completion_done": completion.done(),
        "completion_cancelled": completion.cancelled(),
        "coroutine_state": inspect.getcoroutinestate(coroutine),
    }
    completion.cancel()
    coroutine.close()
    target_loop.close()
    return result


async def throughput_sample(iterations: int) -> dict[str, float]:
    """从工作线程提交一组最小协程，并测量提交和完整完成时间。"""
    registry = TaskRegistry()
    loop = asyncio.get_running_loop()
    started = time.perf_counter()

    def submit_all():
        """在同一宿主线程连续提交，保持各轮工作负载一致。"""
        return [
            registry.submit_threadsafe(
                _value(index),
                loop=loop,
                owner="probe.throughput",
            )
            for index in range(iterations)
        ]

    completions = await asyncio.to_thread(submit_all)
    submitted = time.perf_counter()
    values = await asyncio.gather(
        *(asyncio.wrap_future(completion) for completion in completions)
    )
    finished = time.perf_counter()
    assert sum(values) == iterations * (iterations - 1) // 2
    assert await registry.shutdown(timeout_seconds=1.0) is True
    return {
        "submit_ms": (submitted - started) * 1000,
        "total_ms": (finished - started) * 1000,
    }


async def run_samples(iterations: int, samples: int) -> list[dict[str, float]]:
    """顺序执行多轮样本，避免并行样本互相争抢事件循环。"""
    return [await throughput_sample(iterations) for _ in range(samples)]


def parse_args() -> argparse.Namespace:
    """解析探针负载规模。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=2000)
    parser.add_argument("--samples", type=int, default=7)
    return parser.parse_args()


def main() -> None:
    """运行终态探针和吞吐样本并输出 JSON。"""
    args = parse_args()
    if args.iterations < 1 or args.samples < 1:
        raise SystemExit("iterations 和 samples 必须大于 0")
    samples = asyncio.run(run_samples(args.iterations, args.samples))
    print(
        json.dumps(
            {
                "pending": pending_shutdown_probe(),
                "throughput": {
                    "iterations": args.iterations,
                    "samples": args.samples,
                    "submit_ms": [sample["submit_ms"] for sample in samples],
                    "total_ms": [sample["total_ms"] for sample in samples],
                    "submit_median_ms": statistics.median(
                        sample["submit_ms"] for sample in samples
                    ),
                    "total_median_ms": statistics.median(
                        sample["total_ms"] for sample in samples
                    ),
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
