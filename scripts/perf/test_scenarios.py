"""PERF Docker harness 场景协议的无 Docker fake 测试。"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

PERF_DIR = Path(__file__).resolve().parent


def load_module(name: str, path: Path):
    """从脚本路径加载模块，避免要求 scripts 变成运行时 Python package。"""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def snapshot(xvfb_count: int, xvfb_pss_kib: int = 0) -> dict:
    """构造只包含验收字段的进程快照。"""
    return {
        "processes": {
            "xvfb": {"count": xvfb_count, "pss_kib": xvfb_pss_kib},
        }
    }


def agent_snapshot(prefix_counts: dict[str, int], xvfb_count: int = 0) -> dict:
    """构造包含 Agent 模块哨兵的场景边界快照。"""
    return {
        "engine": {
            "working_set_bytes": 500 * 1024 * 1024,
            "network_rx_bytes": 1024,
            "network_tx_bytes": 512,
        },
        "processes": {
            "main_python": {
                "pss_kib": 400 * 1024,
                "uss_kib": 390 * 1024,
                "threads": 8,
            },
            "xvfb": {"count": xvfb_count, "pss_kib": 0},
        },
        "modules": {
            "count": 3000,
            "prefix_counts": prefix_counts,
        },
    }


def managed_resource(before_generation: int, after_generation: int) -> dict:
    """构造 host.display single-flight 观测。"""
    observations = []
    if after_generation > before_generation:
        observations = [
            {
                "operation": "activate",
                "outcome": "started",
                "generation": after_generation,
                "reason": "headed_browser_launch",
            },
            {
                "operation": "activate",
                "outcome": "succeeded",
                "generation": after_generation,
            },
        ]
    return {
        "before": {
            "available": True,
            "snapshot": {"generation": before_generation},
            "observations": [],
        },
        "after": {
            "available": True,
            "snapshot": {"generation": after_generation},
            "observations": observations,
        },
    }


class FakePage:
    """验证本地 data URL 的同步页面替身。"""

    def __init__(self) -> None:
        self.url = ""

    def goto(self, url: str) -> None:
        self.url = url

    def title(self) -> str:
        assert self.url.startswith("data:text/html,")
        return "MoviePilot Browser Probe"

    def close(self) -> None:
        """模拟 Playwright page 对称关闭。"""


class FakeContext:
    """浏览器上下文替身。"""

    def new_page(self) -> FakePage:
        return FakePage()

    def close(self) -> None:
        """模拟 CloakBrowser context 对称关闭。"""


def test_default_cli_and_paths_keep_idle_contract(tmp_path: Path) -> None:
    """未指定 scenario 时保持既有 idle 命令和资源路径。"""
    harness = load_module("moviepilot_perf_cli", PERF_DIR / "moviepilot_docker_ab.py")
    args = harness.build_parser().parse_args(
        [
            "--campaign",
            "fake",
            "--output-dir",
            str(tmp_path),
            "sample",
            "--variant",
            "after",
            "--index",
            "1",
        ]
    )

    assert args.scenario == "idle-default"
    assert harness.sample_volume_names(args, "after", 1) == (
        "mpperf-fake-after-1-config",
        "mpperf-fake-after-1-browser",
    )
    assert harness.sample_result_directory(args, "after", 1) == (
        tmp_path / "fake" / "samples" / "after-1"
    )


def test_browser_scenario_uses_isolated_resource_and_result_names(
    tmp_path: Path,
) -> None:
    """不同激活场景不会覆盖 idle 样本或彼此复用可写卷。"""
    harness = load_module("moviepilot_perf_paths", PERF_DIR / "moviepilot_docker_ab.py")
    args = argparse.Namespace(
        campaign="fake",
        output_dir=tmp_path,
        scenario="browser-headed",
    )

    assert harness.sample_volume_names(args, "after", 2) == (
        "mpperf-fake-browser-headed-after-2-config",
        "mpperf-fake-browser-headed-after-2-browser",
    )
    assert harness.sample_result_directory(args, "after", 2) == (
        tmp_path / "fake" / "samples" / "browser-headed" / "after-2"
    )


def test_agent_scenarios_are_explicit_and_keep_idle_prefix_contract() -> None:
    """PERF-003 暴露完整哨兵，并把 Schema 基线排除在归零门禁外。"""
    harness = load_module(
        "moviepilot_perf_agent_cli",
        PERF_DIR / "moviepilot_docker_ab.py",
    )
    expected_prefixes = {
        "app.agent.orchestrator",
        "app.agent.callback",
        "app.agent.llm.helper",
        "app.agent.tools.base",
        "app.agent.tools.catalog",
        "app.agent.tools.factory",
        "app.agent.tools.impl",
        "langgraph",
        "langchain",
        "langchain_core",
        "openai",
        "anthropic",
        "google.genai",
        "boto3",
        "botocore",
    }

    assert set(harness.AGENT_SCENARIOS) == {
        "agent-disabled-router",
        "agent-tool-catalog",
    }
    assert set(harness.AGENT_HEAVY_MODULE_PREFIXES) == expected_prefixes
    assert expected_prefixes.issubset(harness.MODULE_PREFIXES)
    assert set(harness.AGENT_SCHEMA_BASELINE_PREFIXES) == {
        "langchain",
        "langchain_core",
    }
    assert not set(harness.AGENT_SCHEMA_BASELINE_PREFIXES).intersection(
        harness.AGENT_NONMATERIALIZATION_PREFIXES
    )


@pytest.mark.parametrize("scenario", ["browser-headless", "agent-disabled-router"])
def test_non_default_scenario_rejects_before_without_touching_docker(
    scenario: str,
) -> None:
    """旧基线不具备候选公共合同，所有非默认场景只接受 After。"""
    harness = load_module(
        "moviepilot_perf_after_only", PERF_DIR / "moviepilot_docker_ab.py"
    )
    args = argparse.Namespace(scenario=scenario, variant="before")

    with pytest.raises(harness.HarnessError, match="After"):
        harness.command_sample(args)


def test_activation_validation_enforces_headless_and_headed_invariants() -> None:
    """headless 保持无 Xvfb，headed 两调用只能产生一个 Xvfb。"""
    harness = load_module(
        "moviepilot_perf_validation",
        PERF_DIR / "moviepilot_docker_ab.py",
    )
    headless_marker = {
        "scenario": "browser-headless",
        "pid": 42,
        "success": True,
        "browser": {
            "success": True,
            "concurrency": 1,
            "successes": 1,
            "retained_contexts": 1,
            "managed_resource": managed_resource(0, 0),
            "single_flight_probe": {
                "concurrent_callers": 0,
                "successful_callers": 0,
            },
        },
    }
    headed_marker = {
        "scenario": "browser-headed",
        "pid": 42,
        "success": True,
        "browser": {
            "success": True,
            "concurrency": 2,
            "successes": 2,
            "retained_contexts": 1,
            "managed_resource": managed_resource(0, 1),
            "single_flight_probe": {
                "concurrent_callers": 2,
                "successful_callers": 2,
            },
        },
    }

    headless = harness.evaluate_browser_activation(
        "browser-headless",
        snapshot(0),
        snapshot(0),
        headless_marker,
        expected_pid=42,
    )
    headed = harness.evaluate_browser_activation(
        "browser-headed",
        snapshot(0),
        snapshot(1, 72 * 1024),
        headed_marker,
        expected_pid=42,
    )
    invalid = harness.evaluate_browser_activation(
        "browser-headed",
        snapshot(0),
        snapshot(2, 144 * 1024),
        headed_marker,
        expected_pid=42,
    )

    assert headless["passed"] is True
    assert headed["passed"] is True
    assert headed["single_flight"]["passed"] is True
    assert headed["single_flight"]["generation_after"] == 1
    assert headed["single_flight"]["activation_start_count"] == 1
    assert invalid["passed"] is False
    assert invalid["single_flight"]["passed"] is False


def test_agent_activation_validation_enforces_lazy_boundaries() -> None:
    """禁用态延迟重 Agent 域，首次目录只允许工具域物化。"""
    harness = load_module(
        "moviepilot_perf_agent_validation",
        PERF_DIR / "moviepilot_docker_ab.py",
    )
    zero = {prefix: 0 for prefix in harness.AGENT_HEAVY_MODULE_PREFIXES}
    catalog_loaded = dict(zero)
    catalog_loaded["app.agent.tools.base"] = 1
    catalog_loaded["app.agent.tools.catalog"] = 1
    catalog_loaded["app.agent.tools.factory"] = 1
    catalog_loaded["app.agent.tools.impl"] = 82
    schema_baseline = dict(zero)
    schema_baseline["langchain_core"] = 5
    router_marker = {
        "scenario": "agent-disabled-router",
        "pid": 42,
        "success": True,
        "agent": {
            "success": True,
            "observations": {
                "before": {
                    "available": True,
                    "tool_factory_materialized": False,
                },
                "after": {
                    "available": True,
                    "tool_factory_materialized": False,
                },
            },
            "router_openapi": {
                "success": True,
                "ai_agent_enable": False,
                "missing_routes": [],
                "missing_openapi_paths": [],
                "route_count": 200,
                "openapi_path_count": 180,
                "openapi_sha256": "schema",
            },
        },
    }
    catalog_marker = {
        "scenario": "agent-tool-catalog",
        "pid": 42,
        "success": True,
        "agent": {
            "success": True,
            "observations": {
                "before": {
                    "available": True,
                    "tool_factory_materialized": False,
                },
                "after": {
                    "available": True,
                    "tool_factory_materialized": True,
                },
            },
            "tool_catalog": {
                "success": True,
                "tool_count": 82,
                "schema_count": 82,
                "catalog_entry_count": 82,
                "collision_names": [],
                "plugin_revision": 0,
                "factory_revision": "factory-revision",
                "schemas_sha256": "schemas",
                "schema_digests_complete": True,
                "repeat_tool_count": 82,
                "repeat_stable": True,
            },
        },
    }

    router = harness.evaluate_agent_activation(
        "agent-disabled-router",
        agent_snapshot(schema_baseline),
        agent_snapshot(schema_baseline),
        router_marker,
        expected_pid=42,
    )
    catalog = harness.evaluate_agent_activation(
        "agent-tool-catalog",
        agent_snapshot(zero),
        agent_snapshot(catalog_loaded),
        catalog_marker,
        expected_pid=42,
    )
    invalid_loaded = dict(catalog_loaded)
    invalid_loaded["app.agent.orchestrator"] = 1
    invalid = harness.evaluate_agent_activation(
        "agent-tool-catalog",
        agent_snapshot(zero),
        agent_snapshot(invalid_loaded),
        catalog_marker,
        expected_pid=42,
    )
    callback_loaded = dict(catalog_loaded)
    callback_loaded["app.agent.callback"] = 1
    invalid_callback = harness.evaluate_agent_activation(
        "agent-tool-catalog",
        agent_snapshot(zero),
        agent_snapshot(callback_loaded),
        catalog_marker,
        expected_pid=42,
    )
    network_post = agent_snapshot(catalog_loaded)
    network_post["engine"]["network_tx_bytes"] += 1
    invalid_network = harness.evaluate_agent_activation(
        "agent-tool-catalog",
        agent_snapshot(zero),
        network_post,
        catalog_marker,
        expected_pid=42,
    )

    assert router["passed"] is True
    assert router["action"]["openapi_path_count"] == 180
    assert catalog["passed"] is True
    assert catalog["revision"]["factory"] == "factory-revision"
    assert invalid["passed"] is False
    assert any("非目录" in error for error in invalid["errors"])
    assert invalid_callback["passed"] is False
    assert invalid_network["passed"] is False
    assert any("网络" in error for error in invalid_network["errors"])


def test_sitecustomize_acquires_headed_display_concurrently_in_same_process() -> None:
    """headed probe 并发走公开 SDK 冷启动，并只保留一个上下文。"""
    probe = load_module(
        "moviepilot_perf_sitecustomize",
        PERF_DIR / "instrument" / "sitecustomize.py",
    )
    browser_calls: list[tuple[int, bool]] = []
    closed_contexts: list[tuple[int, int]] = []
    lock = threading.Lock()

    class TrackedContext(FakeContext):
        """记录并发探针关闭的额外浏览器上下文。"""

        def __init__(self, index: int) -> None:
            self.index = index

        def close(self) -> None:
            closed_contexts.append((self.index, threading.get_ident()))

    def launcher(*, headless: bool) -> FakeContext:
        with lock:
            browser_calls.append((threading.get_ident(), headless))
            index = len(browser_calls) - 1
        return TrackedContext(index)

    result = probe._activate_browser_scenario(
        "browser-headed",
        launcher=launcher,
    )

    assert result["success"] is True
    assert result["successes"] == 2
    assert result["retained_contexts"] == 1
    assert len(browser_calls) == 2
    assert len({thread_id for thread_id, _headless in browser_calls}) == 2
    assert all(headless is False for _thread_id, headless in browser_calls)
    assert len(closed_contexts) == 1
    closed_index, closed_thread_id = closed_contexts[0]
    assert closed_thread_id == browser_calls[closed_index][0]
    assert result["single_flight_probe"]["barrier_used"] is True


def test_sitecustomize_headless_uses_one_headless_context() -> None:
    """headless probe 只启动一个无显示上下文。"""
    probe = load_module(
        "moviepilot_perf_sitecustomize_headless",
        PERF_DIR / "instrument" / "sitecustomize.py",
    )
    calls: list[bool] = []

    def launcher(*, headless: bool) -> FakeContext:
        calls.append(headless)
        return FakeContext()

    result = probe._activate_browser_scenario("browser-headless", launcher=launcher)

    assert result["success"] is True
    assert calls == [True]
    assert result["single_flight_probe"]["requested"] is False


def test_sitecustomize_router_probe_generates_complete_openapi_without_http() -> None:
    """禁用态探针直接读取主进程 app，不发起 HTTP 或外部请求。"""
    probe = load_module(
        "moviepilot_perf_sitecustomize_router",
        PERF_DIR / "instrument" / "sitecustomize.py",
    )
    required_paths = [
        "/api/v1/message/agent/stream",
        "/api/v1/message/agent/sessions",
        "/api/v1/openai/v1/chat/completions",
        "/api/v1/openai/v1/responses",
        "/api/v1/anthropic/v1/messages",
        "/api/v1/llm/manage",
        "/api/v1/mcp",
        "/api/v1/mcp/tools",
    ]

    class FakeApp:
        """只实现 Router/OpenAPI 探针使用的 FastAPI 合同。"""

        routes = [SimpleNamespace(path=path) for path in required_paths]

        @staticmethod
        def openapi() -> dict:
            return {
                "info": {"title": "MoviePilot", "version": "v3"},
                "paths": {path: {"get": {}} for path in required_paths},
            }

    result = probe._probe_router_openapi(
        app_instance=FakeApp(),
        settings_object=SimpleNamespace(AI_AGENT_ENABLE=False),
    )

    assert result["success"] is True
    assert result["route_count"] == len(required_paths)
    assert result["openapi_path_count"] == len(required_paths)
    assert result["missing_routes"] == []
    assert result["missing_openapi_paths"] == []


def test_sitecustomize_tool_catalog_probe_records_schema_and_revisions() -> None:
    """首次目录探针保留工具数、Schema 摘要和双 revision。"""
    probe = load_module(
        "moviepilot_perf_sitecustomize_catalog",
        PERF_DIR / "instrument" / "sitecustomize.py",
    )
    definitions = [
        SimpleNamespace(
            name="query_media",
            input_schema={"type": "object", "properties": {}},
        ),
        SimpleNamespace(
            name="add_subscribe",
            input_schema={"type": "object", "properties": {"title": {}}},
        ),
    ]
    catalog = SimpleNamespace(
        entries=(
            SimpleNamespace(
                name="query_media",
                source="builtin",
                schema_digest="a" * 64,
            ),
            SimpleNamespace(
                name="add_subscribe",
                source="builtin",
                schema_digest="b" * 64,
            ),
        ),
        collisions={},
        plugin_revision=7,
        factory_revision="factory-revision",
    )

    class FakeManager:
        """按真实管理器合同在 list_tools 后发布 catalog。"""

        def __init__(self) -> None:
            self.catalog = None

        def list_tools(self):
            self.catalog = catalog
            return definitions

    result = probe._probe_tool_catalog(manager=FakeManager())

    assert result["success"] is True
    assert result["tool_count"] == 2
    assert result["schema_count"] == 2
    assert result["plugin_revision"] == 7
    assert result["factory_revision"] == "factory-revision"
    assert len(result["schemas_sha256"]) == 64
    assert len(result["catalog_sha256"]) == 64
    assert result["source_counts"] == {"builtin": 2}
    assert result["schema_digests_complete"] is True
    assert result["repeat_catalog_same_object"] is True
    assert result["repeat_revision_unchanged"] is True
    assert result["repeat_stable"] is True


def test_sitecustomize_agent_scenario_records_before_and_after_observations(
    monkeypatch,
) -> None:
    """Agent 场景在同一目标解释器内记录模块与 materialization 边界。"""
    probe = load_module(
        "moviepilot_perf_sitecustomize_agent",
        PERF_DIR / "instrument" / "sitecustomize.py",
    )
    module_observations = iter(
        [
            {"total_modules": 100, "prefix_counts": {}, "matching_modules": []},
            {
                "total_modules": 190,
                "prefix_counts": {
                    "app.agent.tools.factory": 1,
                    "app.agent.tools.impl": 82,
                },
                "matching_modules": ["app.agent.tools.factory"],
            },
        ]
    )
    runtime_observations = iter(
        [
            {"available": True, "tool_factory_materialized": False},
            {"available": True, "tool_factory_materialized": True},
        ]
    )
    monkeypatch.setattr(
        probe,
        "_agent_module_observation",
        lambda: next(module_observations),
    )
    monkeypatch.setattr(
        probe,
        "_probe_tool_catalog",
        lambda manager=None: {
            "success": True,
            "tool_count": 82,
            "schema_count": 82,
        },
    )

    result = probe._activate_agent_scenario(
        "agent-tool-catalog",
        runtime_reader=lambda: next(runtime_observations),
    )

    assert result["success"] is True
    assert result["observations"]["before"]["tool_factory_materialized"] is False
    assert result["observations"]["after"]["tool_factory_materialized"] is True
    assert result["modules"]["before"]["total_modules"] == 100
    assert result["modules"]["after"]["total_modules"] == 190


def test_sitecustomize_serializes_managed_resource_facade(monkeypatch) -> None:
    """进程探针按公开只读 facade 记录 generation 与 activate observation。"""
    observation = SimpleNamespace(
        capability_id="host.display",
        generation=1,
        operation="activate",
        outcome="started",
        reason="fake",
        materialization="materialized",
        lifecycle="starting",
        duration_ms=0.5,
        error=None,
    )
    runtime_snapshot = SimpleNamespace(
        capability_id="host.display",
        materialization="materialized",
        lifecycle="running",
        generation=1,
        visible=True,
        error=None,
    )

    facade = ModuleType("app.runtime.resources")

    def managed_resource_snapshot(capability_id: str):
        assert capability_id == "host.display"
        return runtime_snapshot

    def managed_resource_observations(capability_id=None):
        assert capability_id == "host.display"
        return (observation,)

    facade.managed_resource_snapshot = managed_resource_snapshot
    facade.managed_resource_observations = managed_resource_observations
    monkeypatch.setitem(sys.modules, "app.runtime.resources", facade)
    probe = load_module(
        "moviepilot_perf_sitecustomize_observation",
        PERF_DIR / "instrument" / "sitecustomize.py",
    )

    result = probe._read_display_runtime()

    assert result["available"] is True
    assert result["snapshot"]["generation"] == 1
    assert result["observations"][0]["operation"] == "activate"
    assert result["observations"][0]["outcome"] == "started"


def test_sitecustomize_signal_worker_publishes_atomic_marker(tmp_path: Path) -> None:
    """信号回调只调度目标进程工作线程，并发布带 PID 的完成 marker。"""
    probe = load_module(
        "moviepilot_perf_sitecustomize_marker",
        PERF_DIR / "instrument" / "sitecustomize.py",
    )
    probe._OUTPUT_DIR = str(tmp_path)
    probe._SCENARIO = "browser-headless"
    probe._activation_started = False
    probe._activate_browser_scenario = lambda scenario: {
        "success": scenario == "browser-headless"
    }

    probe._request_activation(None, None)
    marker_path = tmp_path / f"activation-{os.getpid()}.json"
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not marker_path.exists():
        time.sleep(0.01)

    payload = json.loads(marker_path.read_text(encoding="utf-8"))
    assert payload["pid"] == os.getpid()
    assert payload["scenario"] == "browser-headless"
    assert payload["success"] is True
    assert not list(tmp_path.glob("*.tmp"))


def test_sitecustomize_signal_worker_dispatches_agent_scenario(tmp_path: Path) -> None:
    """SIGUSR2 worker 对 Agent 场景也在当前 PID 发布完整 marker。"""
    probe = load_module(
        "moviepilot_perf_sitecustomize_agent_marker",
        PERF_DIR / "instrument" / "sitecustomize.py",
    )
    probe._OUTPUT_DIR = str(tmp_path)
    probe._SCENARIO = "agent-disabled-router"
    probe._activate_agent_scenario = lambda scenario: {
        "success": scenario == "agent-disabled-router"
    }

    probe._run_activation()

    marker_path = tmp_path / f"activation-{os.getpid()}.json"
    payload = json.loads(marker_path.read_text(encoding="utf-8"))
    assert payload["pid"] == os.getpid()
    assert payload["scenario"] == "agent-disabled-router"
    assert payload["agent"]["success"] is True
    assert "browser" not in payload


def test_markdown_reports_activation_and_keeps_scenario_medians_separate() -> None:
    """非默认场景报告包含激活证据，并按场景隔离中位数。"""
    harness = load_module(
        "moviepilot_perf_report", PERF_DIR / "moviepilot_docker_ab.py"
    )
    process_data = {
        "main_python": {"pss_kib": 400 * 1024, "uss_kib": 390 * 1024, "threads": 8},
        "xvfb": {"count": 0, "pss_kib": 0},
    }
    post_process_data = {
        "main_python": {"pss_kib": 410 * 1024, "uss_kib": 400 * 1024, "threads": 10},
        "xvfb": {"count": 1, "pss_kib": 72 * 1024},
    }
    activation = {
        "worker_elapsed_seconds": 1.25,
        "pre": {
            "engine": {
                "working_set_bytes": 500 * 1024 * 1024,
                "network_rx_bytes": 1024,
                "network_tx_bytes": 512,
            },
            "processes": process_data,
        },
        "post": {
            "engine": {
                "working_set_bytes": 600 * 1024 * 1024,
                "network_rx_bytes": 3072,
                "network_tx_bytes": 1536,
            },
            "processes": post_process_data,
        },
        "marker": {"success": True},
        "validation": {
            "passed": True,
            "single_flight": {"passed": True},
        },
    }
    sample = {
        "scenario": "browser-headed",
        "variant": "after",
        "sample_index": 1,
        "http_ready_seconds": 7.0,
        "activation": activation,
        "measurements": [
            {
                "target_minute": 1.0,
                "engine": {
                    "working_set_bytes": 610 * 1024 * 1024,
                    "network_rx_bytes": 1024,
                    "network_tx_bytes": 512,
                },
                "processes": post_process_data,
                "modules": {"count": 3000},
            }
        ],
    }
    build = {
        "campaign": "fake",
        "platform": "linux/arm64",
        "before_commit": "before",
        "after_commit": "after",
        "substrate": {"reference": "frozen"},
    }

    report = harness.build_markdown_report(build, None, [sample])

    assert "## 场景激活" in report
    assert "browser-headed" in report
    assert "Single-flight" in report
    assert "### `browser-headed`" in report
    assert "1.25" in report


def test_markdown_reports_agent_observation_revision_and_sentinel() -> None:
    """Agent 场景报告展示物化边界、revision 与定时哨兵峰值。"""
    harness = load_module(
        "moviepilot_perf_agent_report",
        PERF_DIR / "moviepilot_docker_ab.py",
    )
    zero = {prefix: 0 for prefix in harness.AGENT_HEAVY_MODULE_PREFIXES}
    loaded = dict(zero)
    loaded["app.agent.tools.factory"] = 1
    loaded["app.agent.tools.impl"] = 82
    pre = agent_snapshot(zero)
    post = agent_snapshot(loaded)
    activation = {
        "worker_elapsed_seconds": 2.5,
        "pre": pre,
        "post": post,
        "marker": {"success": True},
        "validation": {
            "passed": True,
            "observed": {
                "prefix_before": zero,
                "prefix_after": loaded,
                "tool_factory_materialized_before": False,
                "tool_factory_materialized_after": True,
            },
            "action": {
                "tool_count": 82,
                "schema_count": 82,
                "repeat_stable": True,
                "collision_names": [],
            },
            "revision": {"plugin": 7, "factory": "1234567890abcdef"},
        },
    }
    sample = {
        "scenario": "agent-tool-catalog",
        "variant": "after",
        "sample_index": 1,
        "http_ready_seconds": 7.0,
        "activation": activation,
        "measurements": [
            {
                "target_minute": 1.0,
                "engine": post["engine"],
                "processes": post["processes"],
                "modules": {
                    "count": 3082,
                    "prefix_counts": loaded,
                },
            }
        ],
    }
    build = {
        "campaign": "fake",
        "platform": "linux/arm64",
        "before_commit": "before",
        "after_commit": "after",
        "substrate": {"reference": "frozen"},
    }

    report = harness.build_markdown_report(build, None, [sample])

    assert "## Agent 场景动作" in report
    assert "False→True" in report
    assert "82/82; repeat=Y; collision=0" in report
    assert "7/1234567890ab" in report
    assert "## Agent 模块哨兵" in report
    assert "app.agent.tools.impl=82" in report
