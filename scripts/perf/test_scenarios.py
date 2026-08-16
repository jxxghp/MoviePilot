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


def test_browser_scenario_rejects_before_without_touching_docker() -> None:
    """旧基线不具备 SDK/display 冷启动不变量，非默认场景只接受 After。"""
    harness = load_module(
        "moviepilot_perf_after_only", PERF_DIR / "moviepilot_docker_ab.py"
    )
    args = argparse.Namespace(scenario="browser-headless", variant="before")

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

    facade = ModuleType("app.runtime.managed_resources")

    def managed_resource_snapshot(capability_id: str):
        assert capability_id == "host.display"
        return runtime_snapshot

    def managed_resource_observations(capability_id=None):
        assert capability_id == "host.display"
        return (observation,)

    facade.managed_resource_snapshot = managed_resource_snapshot
    facade.managed_resource_observations = managed_resource_observations
    monkeypatch.setitem(sys.modules, "app.runtime.managed_resources", facade)
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
