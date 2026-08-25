"""测量同步资源关闭期间的事件循环响应与关闭总耗时。"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Awaitable, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.testing.bootstrap import ensure_sites_stub, isolate_config_dir

isolate_config_dir()
ensure_sites_stub()

from app.startup import lifecycle
from app.startup.initializers import modules as modules_initializer


async def _noop_async() -> None:
    """提供不产生外部副作用的异步关闭替身。"""


def _noop_sync() -> None:
    """提供不产生外部副作用的同步关闭替身。"""


def configure_module_probe(block_seconds: float) -> None:
    """把模块关闭依赖替换为隔离 owner，仅保留一个固定同步等待。"""

    def blocking_module_shutdown() -> bool:
        time.sleep(block_seconds)
        return True

    modules_initializer.ModuleManager = lambda: SimpleNamespace(
        shutdown=blocking_module_shutdown
    )
    modules_initializer.EventManager = lambda: SimpleNamespace(
        stop_async=_noop_async
    )
    modules_initializer.DohHelper = lambda: SimpleNamespace(shutdown=_noop_sync)
    modules_initializer.ThreadHelper = lambda: SimpleNamespace(shutdown=_noop_sync)
    modules_initializer.RedisHelper = lambda: SimpleNamespace(close=_noop_sync)
    modules_initializer.AsyncRedisHelper = lambda: SimpleNamespace(close=_noop_async)
    modules_initializer.close_image_proxy_block_log_coalescer = _noop_async
    modules_initializer.close_browser_sessions = _noop_sync
    modules_initializer.stop_managed_resources = _noop_async
    modules_initializer.stop_message = _noop_sync
    modules_initializer.shutdown_web_agent_background_tasks = _noop_async
    modules_initializer.wait_web_agent_background_tasks = _noop_async
    modules_initializer.get_configured_agent_chat_persistence = lambda: SimpleNamespace(
        begin_shutdown=_noop_sync,
        shutdown=_noop_async,
    )

    async def stop_database_worker() -> None:
        modules_initializer._database_worker = None

    modules_initializer.stop_database_worker = stop_database_worker
    modules_initializer.close_database = _noop_async
    modules_initializer.stop_frontend = _noop_sync
    modules_initializer.clear_temp = _noop_sync
    modules_initializer._database_worker = None


async def sample(
    shutdown: Callable[[], Awaitable[object]],
    *,
    heartbeat_seconds: float,
) -> dict[str, float | bool]:
    """执行一次关闭并测量同期心跳的实际唤醒延迟。"""
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    heartbeat_at: float | None = None

    async def heartbeat() -> None:
        nonlocal heartbeat_at
        await asyncio.sleep(heartbeat_seconds)
        heartbeat_at = loop.time()

    heartbeat_task = asyncio.create_task(heartbeat())
    await shutdown()
    shutdown_finished_at = loop.time()
    completed_before_shutdown = heartbeat_task.done()
    await heartbeat_task
    assert heartbeat_at is not None
    return {
        "heartbeat_delay_ms": (heartbeat_at - started_at) * 1000,
        "heartbeat_completed_before_shutdown": completed_before_shutdown,
        "shutdown_ms": (shutdown_finished_at - started_at) * 1000,
    }


async def run_samples(
    *,
    block_seconds: float,
    heartbeat_seconds: float,
    samples: int,
) -> dict[str, object]:
    """顺序采集模块内部和生命周期总入口两类同步关闭样本。"""
    configure_module_probe(block_seconds)
    module_samples = [
        await sample(
            modules_initializer.stop_modules,
            heartbeat_seconds=heartbeat_seconds,
        )
        for _ in range(samples)
    ]
    lifecycle_samples = [
        await sample(
            lambda: lifecycle.run_shutdown_step(
                "probe.sync_owner",
                lifecycle.offload_shutdown_callback(
                    lambda: time.sleep(block_seconds)
                ),
                timeout_seconds=max(1.0, block_seconds * 4),
            ),
            heartbeat_seconds=heartbeat_seconds,
        )
        for _ in range(samples)
    ]

    def summarize(values: list[dict[str, float | bool]]) -> dict[str, object]:
        return {
            "samples": values,
            "heartbeat_delay_median_ms": statistics.median(
                float(value["heartbeat_delay_ms"]) for value in values
            ),
            "shutdown_median_ms": statistics.median(
                float(value["shutdown_ms"]) for value in values
            ),
            "heartbeat_completed_before_shutdown": all(
                bool(value["heartbeat_completed_before_shutdown"])
                for value in values
            ),
        }

    return {
        "block_ms": block_seconds * 1000,
        "heartbeat_target_ms": heartbeat_seconds * 1000,
        "module_step": summarize(module_samples),
        "lifecycle_step": summarize(lifecycle_samples),
    }


def parse_args() -> argparse.Namespace:
    """解析固定阻塞、心跳间隔和样本数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--block-ms", type=float, default=50.0)
    parser.add_argument("--heartbeat-ms", type=float, default=10.0)
    parser.add_argument("--samples", type=int, default=7)
    return parser.parse_args()


def main() -> None:
    """运行隔离样本并输出 JSON。"""
    args = parse_args()
    if args.block_ms <= 0 or args.heartbeat_ms <= 0 or args.samples < 1:
        raise SystemExit("block-ms、heartbeat-ms 和 samples 必须大于 0")
    result = asyncio.run(
        run_samples(
            block_seconds=args.block_ms / 1000,
            heartbeat_seconds=args.heartbeat_ms / 1000,
            samples=args.samples,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
