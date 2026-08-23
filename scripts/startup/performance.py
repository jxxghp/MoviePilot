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
from typing import Any, Optional


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
PERFORMANCE_FACTOR = 2.0
PERFORMANCE_SLACK_MS = 500.0
# 基线由维护者平台生成、CI 在 Linux runner 检查；给跨平台宽松预算保留小幅调度抖动带。
PERFORMANCE_JITTER_FACTOR = 1.05


def measure_import(target: str) -> dict[str, Any]:
    """在独立解释器中测量单个模块的冷导入耗时与模块增量。"""
    code = f"""
import importlib
import json
import sys
import time

from app.testing.bootstrap import install_sites_stub

install_sites_stub()
before = set(sys.modules)
started_at = time.perf_counter()
importlib.import_module({target!r})
elapsed_ms = (time.perf_counter() - started_at) * 1000
print({RESULT_PREFIX!r} + json.dumps({{
    'elapsed_ms': elapsed_ms,
    'loaded_app_module_count': len([
        name for name in set(sys.modules) - before
        if name == 'app' or name.startswith('app.')
    ]),
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

from app.testing.bootstrap import install_sites_stub

install_sites_stub()
from app.startup import lifecycle


def _noop():
    '''替代无需执行的真实同步组件回调。'''
    return None


async def _async_noop():
    '''替代无需执行的真实异步组件回调。'''
    return None


def _isolated_start(component, probe_app):
    '''保留探针所需基础状态，其余组件启动替换为空操作。'''
    # 保留 readiness 所需状态转换，其余真实启动回调替换为空操作。
    if component.start is None:
        return None
    if component.name == '后台任务登记器':
        return component.start
    if component.name == '数据库准备':
        return lambda: lifecycle.get_application_health(
            probe_app
        ).mark_database_ready()
    return _noop


def _isolated_stop(component):
    '''真实释放探针基础设施，其余组件关闭替换为空操作。'''
    # TaskRegistry 是后续生命周期代码的基础设施，探针必须验证其真实释放路径。
    if component.name == '后台任务登记器':
        return component.stop
    return _noop if component.stop is not None else None


async def _probe():
    '''执行一次隔离生命周期并输出资源与耗时样本。'''
    lifecycle.settings.MOVIEPILOT_SAFE_MODE = {safe_mode!r}
    lifecycle.init_extra = _async_noop
    lifecycle.global_vars.set_loop = lambda loop: None
    lifecycle.global_vars.stop_system = lambda: None
    lifecycle.LoggerManager.shutdown = lambda: None
    probe_app = FastAPI()
    original_components = lifecycle.build_lifecycle_components(probe_app)
    isolated_components = tuple(
        dataclasses.replace(
            component,
            start=_isolated_start(component, probe_app),
            stop=_isolated_stop(component),
        )
        for component in original_components
    )
    lifecycle.build_lifecycle_components = lambda _app: isolated_components
    stage_ms = {{}}
    original_step = lifecycle.run_startup_step

    async def timed_step(name, callback, timeout_seconds=None):
        '''执行隔离启动步骤并记录耗时。'''
        started = time.perf_counter()
        result = await original_step(name, callback, timeout_seconds)
        stage_ms[name] = round((time.perf_counter() - started) * 1000, 3)
        return result

    lifecycle.run_startup_step = timed_step
    before_threads = threading.active_count()
    before_tasks = len(asyncio.all_tasks())
    started = time.perf_counter()
    async with lifecycle.lifespan(probe_app):
        startup_ms = (time.perf_counter() - started) * 1000
        started_threads = threading.active_count()
        started_tasks = len(asyncio.all_tasks())
    finished_ms = (time.perf_counter() - started) * 1000
    enabled_components = [
        component.name for component in isolated_components
        if component.enabled({safe_mode!r})
    ]
    print({LIFECYCLE_RESULT_PREFIX!r} + json.dumps({{
        'mode': 'safe' if {safe_mode!r} else 'normal',
        'enabled_components': enabled_components,
        'enabled_component_count': len(enabled_components),
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
            "loaded_app_module_count": int(
                statistics.median(
                    sample["loaded_app_module_count"] for sample in samples
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
        enabled_components = samples[0]["enabled_components"]
        if any(
            sample["enabled_components"] != enabled_components
            for sample in samples[1:]
        ):
            raise RuntimeError(f"{mode_name} 模式生命周期组件在重复采样间发生变化")
        lifecycle_modes[mode_name] = {
            "samples": [
                {
                    key: value
                    for key, value in sample.items()
                    if key != "enabled_components"
                }
                for sample in samples
            ],
            "median_startup_ms": round(
                statistics.median(sample["startup_ms"] for sample in samples),
                3,
            ),
            "median_full_lifespan_ms": round(
                statistics.median(sample["full_lifespan_ms"] for sample in samples),
                3,
            ),
            "enabled_component_count": samples[0]["enabled_component_count"],
            "enabled_components": enabled_components,
        }
    return {
        "schema_version": 2,
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


def check_baseline(
    expected: dict[str, Any],
    actual: dict[str, Any],
) -> list[str]:
    """比较稳定资源契约与宽松耗时预算，返回所有不符合项。"""
    errors: list[str] = []
    if expected.get("schema_version") != actual.get("schema_version"):
        errors.append(
            "启动性能基线 schema 版本变化："
            f"{expected.get('schema_version')} -> {actual.get('schema_version')}"
        )
    expected_targets = expected.get("targets", {})
    actual_targets = actual.get("targets", {})
    if set(expected_targets) != set(actual_targets):
        errors.append("冷导入目标集合已变化")
    for target in sorted(set(expected_targets) & set(actual_targets)):
        expected_target = expected_targets[target]
        actual_target = actual_targets[target]
        if actual_target.get("loaded_app_module_count") != expected_target.get(
            "loaded_app_module_count"
        ):
            errors.append(
                f"{target} 加载宿主模块数变化："
                f"{expected_target.get('loaded_app_module_count')} -> "
                f"{actual_target.get('loaded_app_module_count')}"
            )
        budget_ms = max(
            expected_target["max_ms"] * PERFORMANCE_FACTOR,
            expected_target["max_ms"] + PERFORMANCE_SLACK_MS,
        ) * PERFORMANCE_JITTER_FACTOR
        if actual_target["median_ms"] > budget_ms:
            errors.append(
                f"{target} 冷导入中位数 {actual_target['median_ms']}ms "
                f"超过预算 {round(budget_ms, 3)}ms"
            )
    expected_modes = expected.get("lifecycle", {}).get("modes", {})
    actual_modes = actual.get("lifecycle", {}).get("modes", {})
    if set(expected_modes) != set(actual_modes):
        errors.append("生命周期模式集合已变化")
    for mode_name in sorted(set(expected_modes) & set(actual_modes)):
        expected_mode = expected_modes[mode_name]
        actual_mode = actual_modes[mode_name]
        if actual_mode.get("enabled_components") != expected_mode.get(
            "enabled_components"
        ):
            errors.append(f"{mode_name} 模式生命周期组件集合或顺序已变化")
        if (
            actual_mode["enabled_component_count"]
            != expected_mode["enabled_component_count"]
        ):
            errors.append(
                f"{mode_name} 模式组件数变化："
                f"{expected_mode['enabled_component_count']} -> "
                f"{actual_mode['enabled_component_count']}"
            )
        for sample in actual_mode.get("samples", []):
            if sample["threads_after"] != sample["threads_before"]:
                errors.append(f"{mode_name} 模式存在未释放线程")
            if sample["tasks_after"] != sample["tasks_before"]:
                errors.append(f"{mode_name} 模式存在未释放异步任务")
            if sample["database_connections_started"] != 0:
                errors.append(f"{mode_name} 模式隔离采样建立了数据库连接")
    return errors


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """解析只读打印、检查或显式写入操作。"""
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--print",
        dest="operation",
        action="store_const",
        const="print",
        help="打印本次采样且不写文件（默认）",
    )
    action.add_argument(
        "--check",
        dest="operation",
        action="store_const",
        const="check",
        help="按已提交基线检查资源契约和宽松性能预算",
    )
    action.add_argument(
        "--write",
        dest="operation",
        action="store_const",
        const="write",
        help="显式写入采样基线",
    )
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.set_defaults(operation="print")
    return parser.parse_args(argv)


def _display_path(path: Path) -> Path:
    """优先返回仓库相对路径，便于 CLI 输出稳定可读。"""
    try:
        return path.relative_to(PROJECT_ROOT)
    except ValueError:
        return path


def main(argv: Optional[list[str]] = None) -> int:
    """采样关键入口，并按显式操作打印、检查或写入结果。"""
    args = parse_args(argv)
    if args.repeat < 1:
        raise SystemExit("--repeat 必须大于等于 1")
    baseline = collect_baseline(args.repeat)
    output = args.output.resolve()
    if args.operation == "print":
        print(json.dumps(baseline, ensure_ascii=False, indent=2))
        return 0
    if args.operation == "check":
        if not output.is_file():
            raise SystemExit(f"性能基线不存在：{output}")
        expected = json.loads(output.read_text(encoding="utf-8"))
        errors = check_baseline(expected, baseline)
        if errors:
            for error in errors:
                print(f"性能基线检查失败：{error}", file=sys.stderr)
            return 1
        print(f"性能基线检查通过：{_display_path(output)}")
        return 0
    print(f"即将写入：{_display_path(output)}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"已写入：{_display_path(output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
