from __future__ import annotations

import asyncio
import sys
import threading
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

import pytest

from app.runtime.capabilities.errors import (
    CapabilityAdapterModeError,
    CapabilityOperationError,
    CapabilityRuntimeClosedError,
)
from app.runtime.capabilities.model import (
    AdapterExecutionMode,
    CapabilityLifecycleState,
    CapabilityMaterializationState,
)
from app.runtime.capabilities.registry import CapabilityRegistry
from app.runtime.capabilities.runtime import CapabilityRuntime


_MANIFEST = """
schema_version = 1
id = "sample.capability"
kind = "sample"
entrypoint = "sample_implementation:SampleCapability"
depends_on = []

[metadata]
name = "Sample capability"

[activation]
policy = "on_first_use"
watch = []
"""


def _registry(tmp_path: Path) -> CapabilityRegistry:
    manifest_dir = tmp_path / "sample"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "capability.toml").write_text(
        _MANIFEST.strip() + "\n",
        encoding="utf-8",
    )
    return CapabilityRegistry.discover(
        roots=[tmp_path],
        kinds={"sample"},
        selector_schemas={},
    )


@dataclass
class _Candidate:
    generation: int
    started: bool = False
    stopped: bool = False


class _SyncAdapter:
    execution_mode = AdapterExecutionMode.SYNC

    def __init__(self) -> None:
        self.materialize_calls = 0
        self.create_calls = 0
        self.start_calls = 0
        self.stop_calls = 0
        self.stop_instances = []
        self.cleanup_calls = 0
        self.fail_materialize = False
        self.fail_start = False
        self.fail_stop = False
        self.start_entered: Optional[threading.Event] = None
        self.start_release: Optional[threading.Event] = None
        self.stop_entered: Optional[threading.Event] = None
        self.stop_release: Optional[threading.Event] = None

    def materialize(self, spec) -> object:
        self.materialize_calls += 1
        if self.fail_materialize:
            raise RuntimeError("materialize failed")
        return object()

    def create(self, spec, implementation: object, generation: int, previous: Any = None) -> _Candidate:
        self.create_calls += 1
        return _Candidate(generation=generation)

    def start(self, spec, candidate: _Candidate, generation: int) -> None:
        self.start_calls += 1
        if self.start_entered:
            self.start_entered.set()
        if self.start_release:
            assert self.start_release.wait(timeout=5)
        candidate.started = True
        if self.fail_start:
            raise RuntimeError("start failed")

    def stop(self, spec, instance: _Candidate, generation: int) -> None:
        self.stop_calls += 1
        self.stop_instances.append(instance)
        if self.stop_entered:
            self.stop_entered.set()
        if self.stop_release:
            assert self.stop_release.wait(timeout=5)
        instance.stopped = True
        if self.fail_stop:
            raise RuntimeError("stop failed")

    def cleanup(self, spec, candidate: _Candidate, generation: int, error: BaseException) -> None:
        self.cleanup_calls += 1
        candidate.stopped = True


class _AsyncAdapter:
    execution_mode = AdapterExecutionMode.ASYNC

    def __init__(self) -> None:
        self.materialize_calls = 0
        self.create_calls = 0
        self.start_calls = 0
        self.stop_calls = 0
        self.stop_instances = []
        self.cleanup_calls = 0
        self.fail_materialize = False
        self.fail_start = False
        self.fail_stop = False
        self.start_entered = asyncio.Event()
        self.start_release = asyncio.Event()
        self.stop_entered: Optional[asyncio.Event] = None
        self.stop_release: Optional[asyncio.Event] = None

    async def materialize(self, spec) -> object:
        self.materialize_calls += 1
        await asyncio.sleep(0)
        if self.fail_materialize:
            raise RuntimeError("async materialize failed")
        return object()

    async def create(self, spec, implementation: object, generation: int, previous: Any = None) -> _Candidate:
        self.create_calls += 1
        await asyncio.sleep(0)
        return _Candidate(generation=generation)

    async def start(self, spec, candidate: _Candidate, generation: int) -> None:
        self.start_calls += 1
        self.start_entered.set()
        await self.start_release.wait()
        candidate.started = True
        if self.fail_start:
            raise RuntimeError("async start failed")

    async def stop(self, spec, instance: _Candidate, generation: int) -> None:
        self.stop_calls += 1
        self.stop_instances.append(instance)
        if self.stop_entered:
            self.stop_entered.set()
        if self.stop_release:
            await self.stop_release.wait()
        await asyncio.sleep(0)
        instance.stopped = True
        if self.fail_stop:
            raise RuntimeError("async stop failed")

    async def cleanup(self, spec, candidate: _Candidate, generation: int, error: BaseException) -> None:
        self.cleanup_calls += 1
        await asyncio.sleep(0)
        candidate.stopped = True


def test_materialize_and_start_have_independent_state_axes(tmp_path: Path) -> None:
    """兼容查询只物化代码，资源必须等显式 activate 成功后才对外可见。"""
    adapter = _SyncAdapter()
    runtime = CapabilityRuntime(_registry(tmp_path), adapters={"sample": adapter})

    implementation = runtime.materialize("sample.capability", reason="compat_lookup")
    materialized = runtime.snapshot("sample.capability")

    assert implementation is not None
    assert materialized.materialization is CapabilityMaterializationState.RESOLVED
    assert materialized.lifecycle is CapabilityLifecycleState.DISCOVERED
    assert materialized.visible is False
    assert adapter.start_calls == 0

    instance = runtime.activate("sample.capability", reason="first_use")
    running = runtime.snapshot("sample.capability")

    assert runtime.get_running("sample.capability") is instance
    assert running.lifecycle is CapabilityLifecycleState.RUNNING
    assert running.visible is True
    assert running.generation == 2


def test_materialize_failure_does_not_claim_resource_lifecycle_failure(tmp_path: Path) -> None:
    """仅解析代码失败时，资源轴尚未启动，必须保持 DISCOVERED。"""
    adapter = _SyncAdapter()
    adapter.fail_materialize = True
    runtime = CapabilityRuntime(_registry(tmp_path), adapters={"sample": adapter})

    with pytest.raises(CapabilityOperationError, match="materialize failed"):
        runtime.materialize("sample.capability", reason="compat_lookup")

    snapshot = runtime.snapshot("sample.capability")
    assert snapshot.materialization is CapabilityMaterializationState.FAILED
    assert snapshot.lifecycle is CapabilityLifecycleState.DISCOVERED
    assert snapshot.visible is False


def test_state_read_calibrates_consumer_import_without_importing_new_module(tmp_path: Path) -> None:
    """显式旧导入存在时应复用 sys.modules 中的 canonical symbol。"""
    adapter = _SyncAdapter()
    runtime = CapabilityRuntime(_registry(tmp_path), adapters={"sample": adapter})
    module = types.ModuleType("sample_implementation")
    canonical = type("SampleCapability", (), {})
    module.SampleCapability = canonical

    with patch.dict(sys.modules, {"sample_implementation": module}):
        snapshot = runtime.snapshot("sample.capability")
        implementation = runtime.materialize(
            "sample.capability",
            reason="compat_lookup",
        )

    assert snapshot.materialization is CapabilityMaterializationState.RESOLVED
    assert snapshot.lifecycle is CapabilityLifecycleState.DISCOVERED
    assert snapshot.generation == 0
    assert implementation is canonical
    assert adapter.materialize_calls == 0


def test_state_read_does_not_invoke_module_level_lazy_export(tmp_path: Path) -> None:
    """sys.modules 校准只能读模块字典，不能触发模块级 __getattr__。"""
    runtime = CapabilityRuntime(
        _registry(tmp_path),
        adapters={"sample": _SyncAdapter()},
    )
    module = types.ModuleType("sample_implementation")
    lazy_reads = []

    def resolve(name: str) -> object:
        lazy_reads.append(name)
        raise AssertionError("state read must not resolve lazy exports")

    module.__getattr__ = resolve
    with patch.dict(sys.modules, {"sample_implementation": module}):
        snapshot = runtime.snapshot("sample.capability")

    assert snapshot.materialization is CapabilityMaterializationState.UNRESOLVED
    assert lazy_reads == []


def test_sync_activate_is_single_flight_and_publishes_only_after_start(tmp_path: Path) -> None:
    """并发首启只能创建一个候选实例，start 返回前普通查询不可见。"""
    adapter = _SyncAdapter()
    adapter.start_entered = threading.Event()
    adapter.start_release = threading.Event()
    runtime = CapabilityRuntime(_registry(tmp_path), adapters={"sample": adapter})
    results = []
    errors = []

    def activate() -> None:
        try:
            results.append(runtime.activate("sample.capability", reason="concurrent"))
        except BaseException as error:  # pragma: no cover - diagnostic collection
            errors.append(error)

    first = threading.Thread(target=activate)
    second = threading.Thread(target=activate)
    first.start()
    assert adapter.start_entered.wait(timeout=5)
    second.start()

    assert runtime.get_running("sample.capability") is None
    assert runtime.snapshot("sample.capability").lifecycle is CapabilityLifecycleState.STARTING
    adapter.start_release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not errors
    assert len(results) == 2
    assert results[0] is results[1]
    assert adapter.materialize_calls == 1
    assert adapter.create_calls == 1
    assert adapter.start_calls == 1
    assert runtime.snapshot("sample.capability").generation == 1


def test_failed_start_cleans_candidate_and_requires_explicit_retry(tmp_path: Path) -> None:
    """半初始化候选必须清理；FAILED 不得被普通 activate 隐式重试。"""
    adapter = _SyncAdapter()
    adapter.fail_start = True
    runtime = CapabilityRuntime(_registry(tmp_path), adapters={"sample": adapter})

    with pytest.raises(CapabilityOperationError, match="start failed"):
        runtime.activate("sample.capability", reason="first_attempt")

    failed = runtime.snapshot("sample.capability")
    assert failed.materialization is CapabilityMaterializationState.RESOLVED
    assert failed.lifecycle is CapabilityLifecycleState.FAILED
    assert failed.visible is False
    assert adapter.cleanup_calls == 1

    with pytest.raises(CapabilityOperationError, match="显式 retry"):
        runtime.activate("sample.capability", reason="implicit_retry")
    assert adapter.start_calls == 1

    adapter.fail_start = False
    instance = runtime.activate("sample.capability", reason="explicit_retry", retry=True)

    assert instance.started is True
    assert runtime.snapshot("sample.capability").generation == 2
    assert adapter.start_calls == 2


def test_adapter_must_return_candidate_before_start(tmp_path: Path) -> None:
    """create 没有候选对象时不得进入 start 或伪造 RUNNING 可见性。"""
    adapter = _SyncAdapter()
    runtime = CapabilityRuntime(_registry(tmp_path), adapters={"sample": adapter})

    with patch.object(adapter, "create", return_value=None), pytest.raises(
        CapabilityOperationError,
        match="candidate",
    ):
        runtime.activate("sample.capability", reason="invalid_candidate")

    assert adapter.start_calls == 0
    assert runtime.snapshot("sample.capability").lifecycle is CapabilityLifecycleState.FAILED
    assert runtime.get_running("sample.capability") is None


def test_stop_withdraws_visibility_before_adapter_callback(tmp_path: Path) -> None:
    """释放外部资源可能阻塞，但运行实例必须在 stop 回调前撤销发布。"""
    adapter = _SyncAdapter()
    runtime = CapabilityRuntime(_registry(tmp_path), adapters={"sample": adapter})
    instance = runtime.activate("sample.capability", reason="start")
    adapter.stop_entered = threading.Event()
    adapter.stop_release = threading.Event()

    stopper = threading.Thread(
        target=lambda: runtime.stop("sample.capability", reason="configuration_removed")
    )
    stopper.start()
    assert adapter.stop_entered.wait(timeout=5)

    assert runtime.get_running("sample.capability") is None
    assert runtime.snapshot("sample.capability").lifecycle is CapabilityLifecycleState.STOPPING
    adapter.stop_release.set()
    stopper.join(timeout=5)

    assert instance.stopped is True
    assert runtime.snapshot("sample.capability").lifecycle is CapabilityLifecycleState.STOPPED


def test_stop_failure_retains_ownership_until_same_instance_stops(tmp_path: Path) -> None:
    """stop 失败后的隐藏资源必须保留所有权，禁止用 retry 绕过清理。"""
    adapter = _SyncAdapter()
    runtime = CapabilityRuntime(_registry(tmp_path), adapters={"sample": adapter})
    instance = runtime.activate("sample.capability", reason="start")
    adapter.fail_stop = True

    with pytest.raises(CapabilityOperationError, match="stop failed"):
        runtime.stop("sample.capability", reason="configuration_removed")

    assert runtime.get_running("sample.capability") is None
    assert runtime.snapshot("sample.capability").lifecycle is CapabilityLifecycleState.FAILED
    with pytest.raises(CapabilityOperationError, match="stop failed"):
        runtime.activate("sample.capability", reason="unsafe_retry", retry=True)
    assert adapter.create_calls == 1

    adapter.fail_stop = False
    runtime.stop("sample.capability", reason="stop_retry")

    assert adapter.stop_instances == [instance, instance, instance]
    assert runtime.snapshot("sample.capability").lifecycle is CapabilityLifecycleState.STOPPED
    replacement = runtime.activate("sample.capability", reason="after_release")
    assert replacement is not instance
    assert adapter.create_calls == 2


def test_reload_withdraws_old_instance_and_publishes_one_new_generation(tmp_path: Path) -> None:
    """同步 reload 在 stop/start 回调期间不暴露旧实例或半初始化候选。"""
    adapter = _SyncAdapter()
    runtime = CapabilityRuntime(_registry(tmp_path), adapters={"sample": adapter})
    old_instance = runtime.activate("sample.capability", reason="initial")
    adapter.stop_entered = threading.Event()
    adapter.stop_release = threading.Event()
    results = []

    reloader = threading.Thread(
        target=lambda: results.append(runtime.reload("sample.capability", reason="config_changed"))
    )
    reloader.start()
    assert adapter.stop_entered.wait(timeout=5)

    assert runtime.get_running("sample.capability") is None
    assert runtime.snapshot("sample.capability").lifecycle is CapabilityLifecycleState.RELOADING
    adapter.stop_release.set()
    reloader.join(timeout=5)

    assert len(results) == 1
    assert results[0] is not old_instance
    assert runtime.get_running("sample.capability") is results[0]
    assert runtime.snapshot("sample.capability").generation == 2


def test_failed_reload_cleans_candidate_and_keeps_instance_invisible(tmp_path: Path) -> None:
    """reload 新 generation 启动失败时不得恢复旧实例或发布候选。"""
    adapter = _SyncAdapter()
    runtime = CapabilityRuntime(_registry(tmp_path), adapters={"sample": adapter})
    runtime.activate("sample.capability", reason="initial")
    adapter.fail_start = True

    with pytest.raises(CapabilityOperationError, match="start failed"):
        runtime.reload("sample.capability", reason="config_changed")

    snapshot = runtime.snapshot("sample.capability")
    assert snapshot.lifecycle is CapabilityLifecycleState.FAILED
    assert snapshot.visible is False
    assert adapter.cleanup_calls == 1


def test_reload_stop_failure_does_not_create_or_reuse_live_previous(tmp_path: Path) -> None:
    """reload 未释放旧资源时必须失败关闭，不能创建或复用同一活对象。"""
    adapter = _SyncAdapter()
    runtime = CapabilityRuntime(_registry(tmp_path), adapters={"sample": adapter})
    old_instance = runtime.activate("sample.capability", reason="initial")
    adapter.fail_stop = True

    with pytest.raises(CapabilityOperationError, match="stop failed"):
        runtime.reload("sample.capability", reason="config_changed")

    assert runtime.get_running("sample.capability") is None
    assert runtime.snapshot("sample.capability").lifecycle is CapabilityLifecycleState.FAILED
    assert adapter.stop_instances == [old_instance]
    assert adapter.create_calls == 1
    assert adapter.start_calls == 1
    with pytest.raises(CapabilityOperationError, match="重试 stop"):
        runtime.activate("sample.capability", reason="implicit_retry")

    adapter.fail_stop = False
    adapter.stop_entered = threading.Event()
    adapter.stop_release = threading.Event()
    recovered = []
    recovery = threading.Thread(
        target=lambda: recovered.append(
            runtime.activate(
                "sample.capability",
                reason="recover_after_reload",
                retry=True,
            )
        )
    )
    recovery.start()
    assert adapter.stop_entered.wait(timeout=5)
    assert runtime.snapshot("sample.capability").lifecycle is CapabilityLifecycleState.STOPPING
    assert runtime.get_running("sample.capability") is None
    assert adapter.create_calls == 1
    adapter.stop_release.set()
    recovery.join(timeout=5)

    assert len(recovered) == 1
    replacement = recovered[0]
    assert adapter.stop_instances == [old_instance, old_instance]
    assert replacement is not old_instance
    assert adapter.create_calls == 2


def test_shutdown_prevents_inflight_start_from_resurrecting_instance(tmp_path: Path) -> None:
    """shutdown 与首启竞争时，候选只能清理，不能在关闭开始后重新发布。"""
    adapter = _SyncAdapter()
    adapter.start_entered = threading.Event()
    adapter.start_release = threading.Event()
    runtime = CapabilityRuntime(_registry(tmp_path), adapters={"sample": adapter})
    activate_errors = []

    def activate() -> None:
        try:
            runtime.activate("sample.capability", reason="racing_start")
        except BaseException as error:
            activate_errors.append(error)

    starter = threading.Thread(target=activate)
    starter.start()
    assert adapter.start_entered.wait(timeout=5)
    closer = threading.Thread(target=lambda: runtime.shutdown(reason="application_shutdown"))
    closer.start()
    adapter.start_release.set()
    starter.join(timeout=5)
    closer.join(timeout=5)

    assert len(activate_errors) == 1
    assert isinstance(activate_errors[0], CapabilityRuntimeClosedError)
    assert adapter.cleanup_calls == 1
    assert runtime.get_running("sample.capability") is None
    assert runtime.is_shutdown is True
    with pytest.raises(CapabilityRuntimeClosedError):
        runtime.activate("sample.capability", reason="late_start")


def test_shutdown_cannot_return_between_open_check_and_sync_claim(tmp_path: Path) -> None:
    """open check 与 inflight claim 必须共享 barrier，关闭扫描不能漏过首启。"""
    adapter = _SyncAdapter()
    adapter.start_entered = threading.Event()
    adapter.start_release = threading.Event()
    runtime = CapabilityRuntime(_registry(tmp_path), adapters={"sample": adapter})
    check_entered = threading.Event()
    check_release = threading.Event()
    shutdown_returned = threading.Event()
    activate_errors = []
    original_ensure_open = runtime._ensure_open
    first_check = True
    check_lock = threading.Lock()

    def gated_ensure_open() -> None:
        nonlocal first_check
        original_ensure_open()
        with check_lock:
            should_wait = first_check
            first_check = False
        if should_wait:
            check_entered.set()
            assert check_release.wait(timeout=5)

    def activate() -> None:
        try:
            runtime.activate("sample.capability", reason="preclaim_race")
        except BaseException as error:
            activate_errors.append(error)

    def shutdown() -> None:
        runtime.shutdown(reason="application_shutdown")
        shutdown_returned.set()

    with patch.object(runtime, "_ensure_open", side_effect=gated_ensure_open):
        starter = threading.Thread(target=activate)
        starter.start()
        assert check_entered.wait(timeout=5)
        closer = threading.Thread(target=shutdown)
        closer.start()

        assert not shutdown_returned.wait(timeout=0.1)
        check_release.set()
        assert adapter.start_entered.wait(timeout=5)
        assert not shutdown_returned.is_set()
        adapter.start_release.set()
        starter.join(timeout=5)
        closer.join(timeout=5)

    assert shutdown_returned.is_set()
    assert len(activate_errors) <= 1
    assert not activate_errors or isinstance(
        activate_errors[0],
        CapabilityRuntimeClosedError,
    )
    assert runtime.get_running("sample.capability") is None


@pytest.mark.asyncio
async def test_shutdown_cannot_return_between_open_check_and_async_claim(
    tmp_path: Path,
) -> None:
    """异步 activate 的同步 claim 区间也必须受同一关闭 barrier 保护。"""
    adapter = _AsyncAdapter()
    runtime = CapabilityRuntime(_registry(tmp_path), adapters={"sample": adapter})
    check_entered = threading.Event()
    check_release = threading.Event()
    shutdown_returned = threading.Event()
    returned_before_release = []
    closer_threads = []
    original_ensure_open = runtime._ensure_open
    first_check = True
    check_lock = threading.Lock()

    def gated_ensure_open() -> None:
        nonlocal first_check
        original_ensure_open()
        with check_lock:
            should_wait = first_check
            first_check = False
        if should_wait:
            check_entered.set()
            assert check_release.wait(timeout=5)

    def shutdown() -> None:
        asyncio.run(runtime.shutdown_async(reason="application_shutdown"))
        shutdown_returned.set()

    def coordinate_shutdown() -> None:
        assert check_entered.wait(timeout=5)
        closer = threading.Thread(target=shutdown)
        closer_threads.append(closer)
        closer.start()
        returned_before_release.append(shutdown_returned.wait(timeout=0.1))
        check_release.set()

    coordinator = threading.Thread(target=coordinate_shutdown)
    coordinator.start()
    with patch.object(runtime, "_ensure_open", side_effect=gated_ensure_open):
        activate_task = asyncio.create_task(
            runtime.activate_async("sample.capability", reason="preclaim_race")
        )
        await adapter.start_entered.wait()
        await asyncio.to_thread(coordinator.join, 5)
        assert returned_before_release == [False]
        assert not shutdown_returned.is_set()
        adapter.start_release.set()
        try:
            await activate_task
        except CapabilityRuntimeClosedError:
            pass
        await asyncio.to_thread(closer_threads[0].join, 5)

    assert shutdown_returned.is_set()
    assert runtime.get_running("sample.capability") is None


@pytest.mark.asyncio
async def test_async_adapter_uses_same_single_flight_state_machine(tmp_path: Path) -> None:
    """异步回调等待不能阻塞事件循环，并发调用共享同一 generation。"""
    adapter = _AsyncAdapter()
    runtime = CapabilityRuntime(_registry(tmp_path), adapters={"sample": adapter})
    first = asyncio.create_task(runtime.activate_async("sample.capability", reason="first"))
    await adapter.start_entered.wait()
    second = asyncio.create_task(runtime.activate_async("sample.capability", reason="second"))
    await asyncio.sleep(0)

    assert runtime.get_running("sample.capability") is None
    adapter.start_release.set()
    first_instance, second_instance = await asyncio.gather(first, second)

    assert first_instance is second_instance
    assert adapter.materialize_calls == 1
    assert adapter.start_calls == 1
    assert runtime.snapshot("sample.capability").generation == 1


@pytest.mark.asyncio
async def test_stop_async_failure_retains_ownership_for_explicit_retry(tmp_path: Path) -> None:
    """异步 stop 失败后只能重试释放同一实例，不能直接启动新实例。"""
    adapter = _AsyncAdapter()
    runtime = CapabilityRuntime(_registry(tmp_path), adapters={"sample": adapter})
    initial = asyncio.create_task(runtime.activate_async("sample.capability", reason="initial"))
    await adapter.start_entered.wait()
    adapter.start_release.set()
    instance = await initial
    adapter.fail_stop = True

    with pytest.raises(CapabilityOperationError, match="async stop failed"):
        await runtime.stop_async("sample.capability", reason="configuration_removed")

    with pytest.raises(CapabilityOperationError, match="async stop failed"):
        await runtime.activate_async("sample.capability", reason="unsafe_retry", retry=True)
    assert adapter.create_calls == 1

    adapter.fail_stop = False
    await runtime.stop_async("sample.capability", reason="stop_retry")

    assert adapter.stop_instances == [instance, instance, instance]
    assert runtime.snapshot("sample.capability").lifecycle is CapabilityLifecycleState.STOPPED


@pytest.mark.asyncio
async def test_async_reload_uses_reloading_state_and_hides_candidate(tmp_path: Path) -> None:
    """异步 reload 与同步入口遵守相同状态和发布边界。"""
    adapter = _AsyncAdapter()
    runtime = CapabilityRuntime(_registry(tmp_path), adapters={"sample": adapter})
    initial = asyncio.create_task(runtime.activate_async("sample.capability", reason="initial"))
    await adapter.start_entered.wait()
    adapter.start_release.set()
    old_instance = await initial

    adapter.start_entered = asyncio.Event()
    adapter.start_release = asyncio.Event()
    reload_task = asyncio.create_task(
        runtime.reload_async("sample.capability", reason="config_changed")
    )
    await adapter.start_entered.wait()

    assert runtime.get_running("sample.capability") is None
    assert runtime.snapshot("sample.capability").lifecycle is CapabilityLifecycleState.RELOADING
    adapter.start_release.set()
    new_instance = await reload_task

    assert new_instance is not old_instance
    assert runtime.get_running("sample.capability") is new_instance
    assert runtime.snapshot("sample.capability").generation == 2


@pytest.mark.asyncio
async def test_failed_async_reload_cleans_candidate_and_enters_failed(tmp_path: Path) -> None:
    """异步 reload 失败与同步入口一致，不发布半初始化候选。"""
    adapter = _AsyncAdapter()
    runtime = CapabilityRuntime(_registry(tmp_path), adapters={"sample": adapter})
    initial = asyncio.create_task(runtime.activate_async("sample.capability", reason="initial"))
    await adapter.start_entered.wait()
    adapter.start_release.set()
    await initial

    adapter.start_entered = asyncio.Event()
    adapter.start_release = asyncio.Event()
    adapter.fail_start = True
    reload_task = asyncio.create_task(
        runtime.reload_async("sample.capability", reason="config_changed")
    )
    await adapter.start_entered.wait()
    adapter.start_release.set()

    with pytest.raises(CapabilityOperationError, match="async start failed"):
        await reload_task

    snapshot = runtime.snapshot("sample.capability")
    assert snapshot.lifecycle is CapabilityLifecycleState.FAILED
    assert snapshot.visible is False
    assert adapter.cleanup_calls == 1


@pytest.mark.asyncio
async def test_async_reload_stop_failure_retains_previous_without_new_create(
    tmp_path: Path,
) -> None:
    """异步 reload 也必须保留未释放旧实例并禁止创建第二份资源。"""
    adapter = _AsyncAdapter()
    runtime = CapabilityRuntime(_registry(tmp_path), adapters={"sample": adapter})
    initial = asyncio.create_task(runtime.activate_async("sample.capability", reason="initial"))
    await adapter.start_entered.wait()
    adapter.start_release.set()
    old_instance = await initial

    adapter.fail_stop = True

    with pytest.raises(CapabilityOperationError, match="async stop failed"):
        await runtime.reload_async("sample.capability", reason="config_changed")

    assert runtime.get_running("sample.capability") is None
    assert runtime.snapshot("sample.capability").lifecycle is CapabilityLifecycleState.FAILED
    assert adapter.stop_instances == [old_instance]
    assert adapter.create_calls == 1
    assert adapter.start_calls == 1
    with pytest.raises(CapabilityOperationError, match="重试 stop"):
        await runtime.activate_async("sample.capability", reason="implicit_retry")
    adapter.fail_stop = False
    adapter.start_entered = asyncio.Event()
    adapter.start_release = asyncio.Event()
    adapter.stop_entered = asyncio.Event()
    adapter.stop_release = asyncio.Event()
    recovery = asyncio.create_task(
        runtime.activate_async(
            "sample.capability",
            reason="recover_after_reload",
            retry=True,
        )
    )
    await adapter.stop_entered.wait()
    assert runtime.snapshot("sample.capability").lifecycle is CapabilityLifecycleState.STOPPING
    assert runtime.get_running("sample.capability") is None
    assert adapter.create_calls == 1
    adapter.stop_release.set()
    await adapter.start_entered.wait()
    adapter.start_release.set()
    replacement = await recovery

    assert adapter.stop_instances == [old_instance, old_instance]
    assert replacement is not old_instance
    assert adapter.create_calls == 2


def test_one_failed_capability_does_not_remove_specs_or_block_other_capabilities(
    tmp_path: Path,
) -> None:
    """单项失败只改变自身状态，Registry 中的其它声明仍可继续运行。"""
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    (first_dir / "capability.toml").write_text(_MANIFEST.strip() + "\n", encoding="utf-8")
    (second_dir / "capability.toml").write_text(
        _MANIFEST.replace("sample.capability", "other.capability")
        .replace("sample_implementation", "other_implementation")
        .strip()
        + "\n",
        encoding="utf-8",
    )
    registry = CapabilityRegistry.discover(
        roots=[tmp_path],
        kinds={"sample"},
        selector_schemas={},
    )
    adapter = _SyncAdapter()
    runtime = CapabilityRuntime(registry, adapters={"sample": adapter})
    adapter.fail_start = True

    with pytest.raises(CapabilityOperationError):
        runtime.activate("sample.capability", reason="fail")
    adapter.fail_start = False
    other = runtime.activate("other.capability", reason="continue")

    assert other.started is True
    assert {spec.id for spec in runtime.list_specs()} == {
        "sample.capability",
        "other.capability",
    }
    assert runtime.snapshot("sample.capability").error == "start failed"


@pytest.mark.asyncio
async def test_sync_and_async_entrypoints_reject_wrong_adapter_mode(tmp_path: Path) -> None:
    """入口与 adapter 执行模型不匹配时应在执行回调前失败。"""
    async_runtime = CapabilityRuntime(
        _registry(tmp_path / "async"),
        adapters={"sample": _AsyncAdapter()},
    )
    with pytest.raises(CapabilityAdapterModeError):
        async_runtime.activate("sample.capability", reason="wrong_mode")

    sync_runtime = CapabilityRuntime(
        _registry(tmp_path / "sync"),
        adapters={"sample": _SyncAdapter()},
    )
    with pytest.raises(CapabilityAdapterModeError):
        await sync_runtime.activate_async("sample.capability", reason="wrong_mode")


def test_adapter_mode_requires_declared_enum_member(tmp_path: Path) -> None:
    """并发模型必须显式声明 enum，不能依赖字符串相等的偶然兼容。"""
    adapter = _SyncAdapter()
    adapter.execution_mode = "sync"
    runtime = CapabilityRuntime(_registry(tmp_path), adapters={"sample": adapter})

    with pytest.raises(CapabilityAdapterModeError):
        runtime.activate("sample.capability", reason="invalid_mode")
