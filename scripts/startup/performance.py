#!/usr/bin/env python3
"""记录 MoviePilot 关键入口的冷导入耗时基线。"""

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "tests"
    / "fixtures"
    / "architecture"
    / "startup-performance-baseline.json"
)
IMPORT_TARGETS = (
    "app.startup.lifecycle",
    "app.factory",
    "app.main",
)
RESULT_PREFIX = "MOVIEPILOT_IMPORT_BASELINE="
LIFECYCLE_RESULT_PREFIX = "MOVIEPILOT_LIFECYCLE_BASELINE="


def measure_import(target: str) -> dict[str, Any]:
    """在独立解释器中测量单个模块的冷导入耗时与模块增量。"""
    code = f"""
import importlib
import json
import sys
import time

before = set(sys.modules)
started_at = time.perf_counter()
importlib.import_module({target!r})
elapsed_ms = (time.perf_counter() - started_at) * 1000
print({RESULT_PREFIX!r} + json.dumps({{
    'elapsed_ms': elapsed_ms,
    'loaded_module_count': len(set(sys.modules) - before),
}}))
"""
    environment = os.environ.copy()
    environment["PYTHONHASHSEED"] = "0"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"冷导入 {target} 失败：{result.stderr.strip() or result.stdout.strip()}"
        )
    payload_line = next(
        (
            line
            for line in reversed(result.stdout.splitlines())
            if line.startswith(RESULT_PREFIX)
        ),
        None,
    )
    if payload_line is None:
        raise RuntimeError(f"冷导入 {target} 未输出测量结果")
    return json.loads(payload_line.removeprefix(RESULT_PREFIX))


def measure_lifecycle(safe_mode: bool) -> dict[str, Any]:
    """在隔离的无 I/O 生命周期中测量正常/安全模式编排和资源增量。

    这里故意把每个组件回调替换为 no-op：基线用于比较生命周期编排、阶段计时、任务
    和线程是否泄漏，不应在生成基线时启动真实插件、调度器或连接用户数据库。
    """
    code = f"""
import asyncio
import dataclasses
import json
import threading
import time

from fastapi import FastAPI

from app.testing.bootstrap import ensure_sites_stub

ensure_sites_stub()
from app.startup import lifecycle


def _noop():
    return None


async def _async_noop():
    return None


async def _probe():
    lifecycle.settings.MOVIEPILOT_SAFE_MODE = {safe_mode!r}
    lifecycle.init_extra = _async_noop
    lifecycle.global_vars.set_loop = lambda loop: None
    lifecycle.global_vars.stop_system = lambda: None
    lifecycle.LoggerManager.shutdown = lambda: None
    original_components = lifecycle.build_lifecycle_components(FastAPI())
    isolated_components = tuple(
        dataclasses.replace(
            component,
            start=_noop if component.start is not None else None,
            stop=_noop if component.stop is not None else None,
        )
        for component in original_components
    )
    lifecycle.build_lifecycle_components = lambda _app: isolated_components
    stage_ms = {{}}
    original_step = lifecycle.run_startup_step

    async def timed_step(name, callback, timeout_seconds=None):
        started = time.perf_counter()
        result = await original_step(name, callback, timeout_seconds)
        stage_ms[name] = round((time.perf_counter() - started) * 1000, 3)
        return result

    lifecycle.run_startup_step = timed_step
    before_threads = threading.active_count()
    before_tasks = len(asyncio.all_tasks())
    started = time.perf_counter()
    async with lifecycle.lifespan(FastAPI()):
        startup_ms = (time.perf_counter() - started) * 1000
        started_threads = threading.active_count()
        started_tasks = len(asyncio.all_tasks())
    finished_ms = (time.perf_counter() - started) * 1000
    print({LIFECYCLE_RESULT_PREFIX!r} + json.dumps({{
        'mode': 'safe' if {safe_mode!r} else 'normal',
        'enabled_component_count': len([
            component for component in isolated_components
            if component.enabled({safe_mode!r})
        ]),
        'startup_ms': round(startup_ms, 3),
        'full_lifespan_ms': round(finished_ms, 3),
        'stage_ms': stage_ms,
        'threads_before': before_threads,
        'threads_started': started_threads,
        'threads_after': threading.active_count(),
        'tasks_before': before_tasks,
        'tasks_started': started_tasks,
        'tasks_after': len(asyncio.all_tasks()),
        # no-op 采样不建立数据库连接；字段显式记录采样范围，避免误读为生产连接数。
        'database_connections_started': 0,
    }}))


asyncio.run(_probe())
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        env={**os.environ, "PYTHONHASHSEED": "0"},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{('安全' if safe_mode else '正常')}模式生命周期采样失败："
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    payload_line = next(
        (
            line
            for line in reversed(result.stdout.splitlines())
            if line.startswith(LIFECYCLE_RESULT_PREFIX)
        ),
        None,
    )
    if payload_line is None:
        raise RuntimeError("生命周期采样未输出测量结果")
    return json.loads(payload_line.removeprefix(LIFECYCLE_RESULT_PREFIX))


def collect_baseline(repeat: int) -> dict[str, Any]:
    """按目标重复采样并生成便于后续对比的统计摘要。"""
    targets: dict[str, Any] = {}
    for target in IMPORT_TARGETS:
        samples = [measure_import(target) for _ in range(repeat)]
        elapsed = [sample["elapsed_ms"] for sample in samples]
        targets[target] = {
            "loaded_module_count": int(
                statistics.median(
                    sample["loaded_module_count"] for sample in samples
                )
            ),
            "max_ms": round(max(elapsed), 3),
            "median_ms": round(statistics.median(elapsed), 3),
            "min_ms": round(min(elapsed), 3),
            "samples_ms": [round(value, 3) for value in elapsed],
        }
    lifecycle_modes: dict[str, Any] = {}
    for safe_mode, mode_name in ((False, "normal"), (True, "safe")):
        samples = [measure_lifecycle(safe_mode) for _ in range(repeat)]
        lifecycle_modes[mode_name] = {
            "samples": samples,
            "median_startup_ms": round(
                statistics.median(sample["startup_ms"] for sample in samples),
                3,
            ),
            "median_full_lifespan_ms": round(
                statistics.median(sample["full_lifespan_ms"] for sample in samples),
                3,
            ),
            "enabled_component_count": samples[0]["enabled_component_count"],
        }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "repeat": repeat,
        "targets": targets,
        "lifecycle": {
            "scope": "isolated no-op component callbacks; no plugin/network/database I/O",
            "modes": lifecycle_modes,
        },
    }


def parse_args() -> argparse.Namespace:
    """解析输出路径和采样次数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    """执行冷导入采样并写入 JSON 基线。"""
    args = parse_args()
    if args.repeat < 1:
        raise SystemExit("--repeat 必须大于等于 1")
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(collect_baseline(args.repeat), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    try:
        display_path = output.relative_to(PROJECT_ROOT)
    except ValueError:
        display_path = output
    print(f"已写入 {display_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
