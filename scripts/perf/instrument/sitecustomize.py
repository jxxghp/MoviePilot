"""MoviePilot Docker 测量进程内使用的最小诊断探针。"""

from __future__ import annotations

import os
import signal
import sys

# 场景激活依赖只在收到信号后加载，避免改变 idle-default 的 import 基线。
# pylint: disable=import-outside-toplevel

_OUTPUT_DIR = os.environ.get("MP_PERF_OUTPUT_DIR")
_SCENARIO = os.environ.get("MP_PERF_SCENARIO", "idle-default")
_ACTIVATION_TIMEOUT = float(os.environ.get("MP_PERF_ACTIVATION_TIMEOUT", "120"))
_AGENT_SCENARIOS = {"agent-disabled-router", "agent-tool-catalog"}
_AGENT_HEAVY_MODULE_PREFIXES = tuple(
    prefix
    for prefix in os.environ.get(
        "MP_PERF_AGENT_MODULE_PREFIXES",
        (
            "app.agent.orchestrator,app.agent.callback,app.agent.llm.helper,"
            "app.agent.tools.base,app.agent.tools.catalog,"
            "app.agent.tools.factory,app.agent.tools.impl,langgraph,langchain,"
            "langchain_core,openai,anthropic,google.genai,boto3,botocore"
        ),
    ).split(",")
    if prefix
)
_snapshot_index = 0
_activation_started = False
_browser_resources: list[object] = []


def _utc_now() -> str:
    """返回稳定、可机器解析的 UTC 时间。"""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path, payload: dict[str, object]) -> None:
    """原子发布结果，避免采集端读取到半写 marker。"""
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    with temporary_path.open("w", encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary_path, path)


def _dump_modules(_signum, _frame) -> None:
    """收到 SIGUSR1 时原子写出当前解释器已经导入的模块名称。"""
    global _snapshot_index
    if not _OUTPUT_DIR:
        return

    _snapshot_index += 1
    os.makedirs(_OUTPUT_DIR, exist_ok=True)
    final_path = os.path.join(
        _OUTPUT_DIR,
        f"modules-{os.getpid()}-{_snapshot_index}.txt",
    )
    temporary_path = f"{final_path}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as output:
        for module_name in sorted(sys.modules):
            output.write(module_name)
            output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary_path, final_path)


def _launch_one_browser(
    *,
    index: int,
    headless: bool,
    launcher,
    start_gate,
) -> tuple[dict[str, object], list[object]]:
    """启动一个本地 data URL 浏览器上下文并返回可序列化结果。"""
    import time

    if start_gate is not None:
        start_gate.wait(timeout=min(_ACTIVATION_TIMEOUT, 10))
    started_at = time.perf_counter()
    retained: list[object] = []
    result: dict[str, object] = {
        "index": index,
        "headless": headless,
        "started_at_monotonic": started_at,
    }
    try:
        context = launcher(headless=headless)
        retained.append(context)
        page = context.new_page()
        retained.append(page)
        page.goto("data:text/html,<title>MoviePilot Browser Probe</title>")
        title = page.title()
        result.update(
            {
                "success": title == "MoviePilot Browser Probe",
                "page_title": title,
                "context_type": type(context).__name__,
            }
        )
        if not result["success"]:
            result["error"] = "本地 data URL 标题校验失败"
    except Exception as error:  # pragma: no cover - 真实浏览器错误由 marker 保存
        result.update(
            {
                "success": False,
                "error_type": type(error).__name__,
                "error": str(error),
            }
        )
    result["elapsed_seconds"] = time.perf_counter() - started_at
    return result, retained


def _enum_value(value):
    """把 runtime 枚举降为 JSON 标量。"""
    return getattr(value, "value", value)


def _stable_digest(value: object) -> str:
    """计算不依赖对象地址的 JSON 摘要。"""
    import hashlib
    import json

    content = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _agent_module_observation() -> dict[str, object]:
    """记录 Agent 重模块在目标解释器中的精确加载状态。"""
    prefix_counts = {
        prefix: sum(
            1
            for module_name in sys.modules
            if module_name == prefix or module_name.startswith(f"{prefix}.")
        )
        for prefix in _AGENT_HEAVY_MODULE_PREFIXES
    }
    matching_modules = sorted(
        module_name
        for module_name in sys.modules
        if any(
            module_name == prefix or module_name.startswith(f"{prefix}.")
            for prefix in _AGENT_HEAVY_MODULE_PREFIXES
        )
    )
    return {
        "total_modules": len(sys.modules),
        "prefix_counts": prefix_counts,
        "matching_modules": matching_modules,
        "matching_sha256": _stable_digest(matching_modules),
    }


def _read_agent_runtime() -> dict[str, object]:
    """读取轻量 Agent loader 的公开只读状态，不触发 capability 首用。"""
    try:
        from app.agent.runtime_loader import is_tool_factory_materialized

        return {
            "available": True,
            "tool_factory_materialized": is_tool_factory_materialized(),
        }
    except Exception as error:  # pragma: no cover - 候选未就绪或真实 runtime 错误
        return {
            "available": False,
            "error_type": type(error).__name__,
            "error": str(error),
        }


def _probe_router_openapi(app_instance=None, settings_object=None) -> dict[str, object]:
    """在主进程中生成 OpenAPI，并验证禁用态 Agent 路由仍完整存在。"""
    if app_instance is None:
        from app.factory import app as app_instance
    if settings_object is None:
        from app.runtime.config import settings as settings_object

    required_paths = (
        "/api/v1/message/agent/stream",
        "/api/v1/message/agent/sessions",
        "/api/v1/openai/v1/chat/completions",
        "/api/v1/openai/v1/responses",
        "/api/v1/anthropic/v1/messages",
        "/api/v1/llm/manage",
        "/api/v1/mcp",
        "/api/v1/mcp/tools",
    )
    schema = app_instance.openapi()
    route_paths = sorted(
        {
            str(route.path)
            for route in app_instance.routes
            if getattr(route, "path", None)
        }
    )
    openapi_paths = sorted((schema.get("paths") or {}).keys())
    missing_routes = [path for path in required_paths if path not in route_paths]
    missing_openapi_paths = [
        path for path in required_paths if path not in openapi_paths
    ]
    agent_enabled = bool(settings_object.AI_AGENT_ENABLE)
    return {
        "success": not agent_enabled
        and not missing_routes
        and not missing_openapi_paths,
        "ai_agent_enable": agent_enabled,
        "required_paths": list(required_paths),
        "missing_routes": missing_routes,
        "missing_openapi_paths": missing_openapi_paths,
        "route_count": len(route_paths),
        "openapi_path_count": len(openapi_paths),
        "openapi_sha256": _stable_digest(schema),
        "openapi_title": (schema.get("info") or {}).get("title"),
        "openapi_version": (schema.get("info") or {}).get("version"),
    }


def _probe_tool_catalog(manager=None) -> dict[str, object]:
    """通过稳定工具管理入口首次生成目录与 JSON Schema。"""
    if manager is None:
        from app.agent.tools.manager import moviepilot_tool_manager

        manager = moviepilot_tool_manager
    definitions = manager.list_tools()
    catalog = manager.catalog
    serialized_definitions = [
        {
            "name": definition.name,
            "input_schema": definition.input_schema,
        }
        for definition in definitions
    ]
    schema_count = sum(
        isinstance(definition.input_schema, dict) for definition in definitions
    )
    entries = catalog.entries if catalog is not None else ()
    collisions = catalog.collisions if catalog is not None else {}
    source_counts: dict[str, int] = {}
    serialized_entries = []
    for entry in entries:
        source_counts[entry.source] = source_counts.get(entry.source, 0) + 1
        serialized_entries.append(
            {
                "name": entry.name,
                "source": entry.source,
                "schema_digest": entry.schema_digest,
            }
        )
    first_catalog_sha256 = _stable_digest(serialized_entries)
    first_schemas_sha256 = _stable_digest(serialized_definitions)
    repeated_definitions = manager.list_tools()
    repeated_catalog = manager.catalog
    repeated_serialized_definitions = [
        {
            "name": definition.name,
            "input_schema": definition.input_schema,
        }
        for definition in repeated_definitions
    ]
    repeated_entries = repeated_catalog.entries if repeated_catalog is not None else ()
    repeated_serialized_entries = [
        {
            "name": entry.name,
            "source": entry.source,
            "schema_digest": entry.schema_digest,
        }
        for entry in repeated_entries
    ]
    repeated_catalog_sha256 = _stable_digest(repeated_serialized_entries)
    repeated_schemas_sha256 = _stable_digest(repeated_serialized_definitions)
    schema_digests_complete = all(
        isinstance(entry.schema_digest, str) and len(entry.schema_digest) == 64
        for entry in entries
    )
    repeat_revision_unchanged = bool(
        catalog is not None
        and repeated_catalog is not None
        and repeated_catalog.plugin_revision == catalog.plugin_revision
        and repeated_catalog.factory_revision == catalog.factory_revision
    )
    repeat_stable = bool(
        repeated_catalog is catalog
        and len(repeated_definitions) == len(definitions)
        and repeated_catalog_sha256 == first_catalog_sha256
        and repeated_schemas_sha256 == first_schemas_sha256
        and repeat_revision_unchanged
    )
    return {
        "success": bool(definitions)
        and catalog is not None
        and len(entries) == len(definitions)
        and schema_count == len(definitions)
        and not collisions
        and schema_digests_complete
        and repeat_stable,
        "tool_count": len(definitions),
        "schema_count": schema_count,
        "catalog_entry_count": len(entries),
        "collision_names": sorted(collisions),
        "plugin_revision": catalog.plugin_revision if catalog is not None else None,
        "factory_revision": catalog.factory_revision if catalog is not None else None,
        "schemas_sha256": first_schemas_sha256,
        "catalog_sha256": first_catalog_sha256,
        "source_counts": source_counts,
        "schema_digests_complete": schema_digests_complete,
        "repeat_tool_count": len(repeated_definitions),
        "repeat_catalog_same_object": repeated_catalog is catalog,
        "repeat_catalog_sha256": repeated_catalog_sha256,
        "repeat_schemas_sha256": repeated_schemas_sha256,
        "repeat_revision_unchanged": repeat_revision_unchanged,
        "repeat_stable": repeat_stable,
    }


def _activate_agent_scenario(
    scenario: str,
    *,
    app_instance=None,
    settings_object=None,
    tool_manager=None,
    runtime_reader=None,
) -> dict[str, object]:
    """执行 Agent 禁用态路由或首次工具目录的进程内场景。"""
    if scenario not in _AGENT_SCENARIOS:
        raise ValueError(f"场景不支持 Agent 激活：{scenario}")
    runtime_reader = runtime_reader or _read_agent_runtime
    modules_before = _agent_module_observation()
    runtime_before = runtime_reader()

    if scenario == "agent-disabled-router":
        action = _probe_router_openapi(
            app_instance=app_instance,
            settings_object=settings_object,
        )
    else:
        action = _probe_tool_catalog(manager=tool_manager)

    modules_after = _agent_module_observation()
    runtime_after = runtime_reader()
    return {
        "requested": True,
        "action": scenario.removeprefix("agent-"),
        "success": bool(action.get("success")),
        "modules": {"before": modules_before, "after": modules_after},
        "observations": {"before": runtime_before, "after": runtime_after},
        "router_openapi": action if scenario == "agent-disabled-router" else None,
        "tool_catalog": action if scenario == "agent-tool-catalog" else None,
    }


def _read_display_runtime() -> dict[str, object]:
    """读取 host.display 的只读状态和观测，不触发资源激活。"""
    try:
        from app.runtime.resources import (
            managed_resource_observations,
            managed_resource_snapshot,
        )

        snapshot = managed_resource_snapshot("host.display")
        observations = managed_resource_observations("host.display")
        return {
            "available": True,
            "snapshot": {
                "capability_id": snapshot.capability_id,
                "materialization": _enum_value(snapshot.materialization),
                "lifecycle": _enum_value(snapshot.lifecycle),
                "generation": snapshot.generation,
                "visible": snapshot.visible,
                "error": snapshot.error,
            },
            "observations": [
                {
                    "capability_id": item.capability_id,
                    "generation": item.generation,
                    "operation": item.operation,
                    "outcome": item.outcome,
                    "reason": item.reason,
                    "materialization": _enum_value(item.materialization),
                    "lifecycle": _enum_value(item.lifecycle),
                    "duration_ms": item.duration_ms,
                    "error": item.error,
                }
                for item in observations
            ],
        }
    except Exception as error:  # pragma: no cover - 核心未就绪或真实 runtime 错误
        return {
            "available": False,
            "error_type": type(error).__name__,
            "error": str(error),
        }


def _close_browser_resources(resources: list[object]) -> list[dict[str, str]]:
    """逆序关闭一次探针创建的页面与上下文，并返回可序列化错误。"""
    errors: list[dict[str, str]] = []
    for resource in reversed(resources):
        close = getattr(resource, "close", None)
        if not callable(close):
            continue
        try:
            close()
        except Exception as error:  # pragma: no cover - 真实浏览器错误由 marker 保存
            errors.append(
                {
                    "resource_type": type(resource).__name__,
                    "error_type": type(error).__name__,
                    "error": str(error),
                }
            )
    return errors


def _activate_browser_scenario(
    scenario: str,
    launcher=None,
) -> dict[str, object]:
    """通过公开 SDK 执行真实浏览器激活，headed 使用并发冷启动探针。"""
    import threading
    import time

    if scenario not in {"browser-headless", "browser-headed"}:
        raise ValueError(f"场景不支持浏览器激活：{scenario}")
    if launcher is None:
        from app.sdk.browser import launch_browser_context

        launcher = launch_browser_context

    display_before = _read_display_runtime()
    headless = scenario == "browser-headless"
    concurrency = 1 if headless else 2
    launch_results: list[dict[str, object] | None] = [None] * concurrency
    cleanup_error_slots: list[list[dict[str, str]]] = [
        [] for _index in range(concurrency)
    ]
    retained_slots = [False] * concurrency
    start_gate = None if headless else threading.Barrier(concurrency)
    completion_gate = None if headless else threading.Barrier(concurrency)

    def launch(index: int) -> None:
        result, resources = _launch_one_browser(
            index=index,
            headless=headless,
            launcher=launcher,
            start_gate=start_gate,
        )
        launch_results[index] = result
        if headless:
            if result.get("success"):
                _browser_resources.extend(resources)
                result["retained"] = True
                retained_slots[index] = True
            else:
                cleanup_error_slots[index] = _close_browser_resources(resources)
            return

        try:
            completion_gate.wait(timeout=min(_ACTIVATION_TIMEOUT, 30))
        except threading.BrokenBarrierError:
            cleanup_error_slots[index] = _close_browser_resources(resources)
            result.update(
                {
                    "success": False,
                    "error_type": "BrokenBarrierError",
                    "error": "并发浏览器启动未能完成同线程清理协调",
                }
            )
            return

        successful_indices = [
            candidate_index
            for candidate_index, item in enumerate(launch_results)
            if item is not None and bool(item.get("success"))
        ]
        retained_index = min(successful_indices) if successful_indices else None
        if index == retained_index:
            # 保留对象不再跨线程使用；容器退出会回收浏览器及其 worker 进程。
            _browser_resources.extend(resources)
            result["retained"] = True
            retained_slots[index] = True
        else:
            # Playwright sync/greenlet 对象必须在创建它的线程内关闭。
            cleanup_error_slots[index] = _close_browser_resources(resources)

    if headless:
        launch(0)
    else:
        threads = [
            threading.Thread(
                target=launch,
                args=(index,),
                name=f"mp-perf-browser-launch-{index}",
                daemon=True,
            )
            for index in range(concurrency)
        ]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + _ACTIVATION_TIMEOUT
        for thread in threads:
            thread.join(timeout=max(deadline - time.monotonic(), 0))

    serialized_launches = [
        item
        if item is not None
        else {
            "index": index,
            "success": False,
            "error_type": "TimeoutError",
            "error": "浏览器启动未在进程内超时前完成",
        }
        for index, item in enumerate(launch_results)
    ]
    launch_starts = [
        float(item["started_at_monotonic"])
        for item in serialized_launches
        if "started_at_monotonic" in item
    ]
    successful_indices = [
        index
        for index, item in enumerate(serialized_launches)
        if bool(item.get("success"))
    ]
    cleanup_errors = [
        error for slot_errors in cleanup_error_slots for error in slot_errors
    ]
    retained_count = sum(retained_slots)

    expected_successes = concurrency
    browser_success = (
        len(successful_indices) == expected_successes and not cleanup_errors
    )
    return {
        "requested": True,
        "headless": headless,
        "concurrency": concurrency,
        "successes": len(successful_indices),
        "retained_contexts": retained_count,
        "launches": serialized_launches,
        "cleanup_errors": cleanup_errors,
        "success": browser_success,
        "managed_resource": {
            "before": display_before,
            "after": _read_display_runtime(),
        },
        "single_flight_probe": {
            "requested": not headless,
            "concurrent_callers": concurrency if not headless else 0,
            "successful_callers": len(successful_indices) if not headless else 0,
            "barrier_used": not headless,
            "launch_start_spread_ms": (
                (max(launch_starts) - min(launch_starts)) * 1000
                if launch_starts
                else None
            ),
            "all_callers_succeeded": len(successful_indices) == expected_successes,
            "calls": serialized_launches if not headless else [],
        },
    }


def _run_activation() -> None:
    """在目标解释器的工作线程中运行激活并发布完成 marker。"""
    if not _OUTPUT_DIR:
        return
    import time
    from pathlib import Path

    started_at = time.perf_counter()
    result: dict[str, object] = {
        "schema_version": 1,
        "scenario": _SCENARIO,
        "pid": os.getpid(),
        "started_at": _utc_now(),
    }
    try:
        if _SCENARIO in _AGENT_SCENARIOS:
            result["agent"] = _activate_agent_scenario(_SCENARIO)
            result["success"] = bool(result["agent"]["success"])
        else:
            result["browser"] = _activate_browser_scenario(_SCENARIO)
            result["success"] = bool(result["browser"]["success"])
    except Exception as error:  # pragma: no cover - 真实集成错误由 marker 保存
        result.update(
            {
                "success": False,
                "error_type": type(error).__name__,
                "error": str(error),
            }
        )
    result["elapsed_seconds"] = time.perf_counter() - started_at
    result["completed_at"] = _utc_now()
    _atomic_write_json(
        Path(_OUTPUT_DIR) / f"activation-{os.getpid()}.json",
        result,
    )


def _request_activation(_signum, _frame) -> None:
    """SIGUSR2 只调度一次工作线程，真实 import 与启动仍在目标进程内完成。"""
    global _activation_started
    if not _OUTPUT_DIR or _activation_started:
        return
    import threading

    _activation_started = True
    threading.Thread(
        target=_run_activation,
        name="mp-perf-scenario-activation",
        daemon=True,
    ).start()


if _OUTPUT_DIR and hasattr(signal, "SIGUSR1"):
    signal.signal(signal.SIGUSR1, _dump_modules)
if _OUTPUT_DIR and hasattr(signal, "SIGUSR2"):
    signal.signal(signal.SIGUSR2, _request_activation)
