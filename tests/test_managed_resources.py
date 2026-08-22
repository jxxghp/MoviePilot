"""Managed Resource 与 Capability Runtime 的集成合同测试。"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from app.runtime.capabilities.errors import (
    CapabilityOperationError,
    CapabilityRuntimeClosedError,
)
from app.runtime.capabilities.runtime import CapabilityRuntime
from app.runtime.extensions.lifecycle.managed_resource_adapter import (
    AsyncManagedResourceAdapter,
    SyncManagedResourceAdapter,
    build_managed_resource_registry,
)
from app.runtime import managed_resources as managed_resource_facade
from app.runtime.managed_resources import (
    MANAGED_RESOURCE_ASYNC_KIND,
    MANAGED_RESOURCE_SYNC_KIND,
    acquire_managed_resource,
    acquire_managed_resource_async,
    configure_managed_resource_runtime,
    managed_resource_observations,
    managed_resource_snapshot,
    shutdown_managed_resource_runtime,
)


PROJECT_ROOT = Path(__file__).parents[1]


@pytest.fixture(autouse=True)
def isolate_managed_resource_facade(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例使用独立 Runtime，避免不可逆关闭态泄漏到后续测试。"""
    monkeypatch.setattr(
        managed_resource_facade,
        "_managed_resource_runtime",
        None,
    )


def _write_manifest(
    root: Path, *, capability_id: str, kind: str, entrypoint: str
) -> None:
    """写入一个最小 on-first-use 托管资源声明。"""
    resource_dir = root / capability_id.replace(".", "_")
    resource_dir.mkdir(parents=True)
    (resource_dir / "capability.toml").write_text(
        "\n".join(
            (
                "schema_version = 1",
                f'id = "{capability_id}"',
                f'kind = "{kind}"',
                f'entrypoint = "{entrypoint}"',
                "depends_on = []",
                "",
                "[metadata]",
                f'name = "{capability_id}"',
                "",
                "[activation]",
                'policy = "on_first_use"',
                "watch = []",
                "",
            )
        ),
        encoding="utf-8",
    )


def _runtime(root: Path) -> CapabilityRuntime:
    """构造同时支持同步与异步资源的测试 Runtime。"""
    registry = build_managed_resource_registry((root,))
    return CapabilityRuntime(
        registry,
        adapters={
            MANAGED_RESOURCE_SYNC_KIND: SyncManagedResourceAdapter(),
            MANAGED_RESOURCE_ASYNC_KIND: AsyncManagedResourceAdapter(),
        },
    )


def test_sync_managed_resource_is_single_flight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并发首用只能发布一个同步资源实例。"""
    module_name = "fixture_sync_managed_resource"
    module = ModuleType(module_name)

    class SyncResource:
        """记录同步资源的创建、启动和停止次数。"""

        instances: list["SyncResource"] = []

        def __init__(self) -> None:
            self.started = 0
            self.stopped = 0
            type(self).instances.append(self)

        def start(self) -> None:
            self.started += 1

        def stop(self) -> None:
            self.stopped += 1

    module.SyncResource = SyncResource
    monkeypatch.setitem(sys.modules, module_name, module)
    _write_manifest(
        tmp_path,
        capability_id="fixture.sync",
        kind=MANAGED_RESOURCE_SYNC_KIND,
        entrypoint=f"{module_name}:SyncResource",
    )
    runtime = _runtime(tmp_path)
    configure_managed_resource_runtime(runtime)

    barrier = threading.Barrier(8)

    def activate() -> SyncResource:
        barrier.wait(timeout=2)
        return acquire_managed_resource("fixture.sync", reason="test")

    with ThreadPoolExecutor(max_workers=8) as executor:
        resources = list(executor.map(lambda _index: activate(), range(8)))

    assert len({id(resource) for resource in resources}) == 1
    assert len(SyncResource.instances) == 1
    assert SyncResource.instances[0].started == 1
    assert managed_resource_snapshot("fixture.sync").generation == 1
    assert [
        observation.outcome
        for observation in managed_resource_observations("fixture.sync")
        if observation.operation == "activate"
    ] == ["started", "succeeded"]

    asyncio.run(shutdown_managed_resource_runtime(reason="test_shutdown"))

    assert SyncResource.instances[0].stopped == 1
    with pytest.raises(CapabilityRuntimeClosedError):
        acquire_managed_resource("fixture.sync", reason="after_shutdown")


def test_async_managed_resource_uses_async_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """异步资源通过异步 Runtime 入口启动并关闭。"""
    module_name = "fixture_async_managed_resource"
    module = ModuleType(module_name)

    class AsyncResource:
        """记录异步资源生命周期调用。"""

        instances: list["AsyncResource"] = []

        def __init__(self) -> None:
            self.events: list[str] = []
            type(self).instances.append(self)

        async def start(self) -> None:
            self.events.append("start")

        async def stop(self) -> None:
            self.events.append("stop")

    module.AsyncResource = AsyncResource
    monkeypatch.setitem(sys.modules, module_name, module)
    _write_manifest(
        tmp_path,
        capability_id="fixture.async",
        kind=MANAGED_RESOURCE_ASYNC_KIND,
        entrypoint=f"{module_name}:AsyncResource",
    )
    runtime = _runtime(tmp_path)
    configure_managed_resource_runtime(runtime)

    async def exercise() -> AsyncResource:
        resource = await acquire_managed_resource_async(
            "fixture.async",
            reason="test",
        )
        await shutdown_managed_resource_runtime(reason="test_shutdown")
        return resource

    resource = asyncio.run(exercise())

    assert resource.events == ["start", "stop"]
    assert AsyncResource.instances == [resource]


def test_failed_start_is_cleaned_before_explicit_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启动失败的候选必须先清理，显式 retry 才能发布下一代资源。"""
    module_name = "fixture_retry_managed_resource"
    module = ModuleType(module_name)

    class RetryResource:
        """首个候选启动失败，后续候选正常启动。"""

        instances: list["RetryResource"] = []

        def __init__(self) -> None:
            self.events: list[str] = []
            self.fail_start = not type(self).instances
            type(self).instances.append(self)

        def start(self) -> None:
            self.events.append("start")
            if self.fail_start:
                raise RuntimeError("start failed")

        def stop(self) -> None:
            self.events.append("stop")

    module.RetryResource = RetryResource
    monkeypatch.setitem(sys.modules, module_name, module)
    _write_manifest(
        tmp_path,
        capability_id="fixture.retry",
        kind=MANAGED_RESOURCE_SYNC_KIND,
        entrypoint=f"{module_name}:RetryResource",
    )
    configure_managed_resource_runtime(_runtime(tmp_path))

    with pytest.raises(CapabilityOperationError, match="start failed"):
        acquire_managed_resource(
            "fixture.retry",
            reason="first_use",
            retry=False,
        )

    resource = acquire_managed_resource(
        "fixture.retry",
        reason="retry",
        retry=True,
    )

    assert RetryResource.instances[0].events == ["start", "stop"]
    assert resource is RetryResource.instances[1]
    assert resource.events == ["start"]

    asyncio.run(shutdown_managed_resource_runtime(reason="test_shutdown"))

    assert resource.events == ["start", "stop"]


def test_shutdown_does_not_materialize_unused_resource(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关闭未激活 Runtime 时不得构造资源或调用 start。"""
    module_name = "fixture_unused_managed_resource"
    module = ModuleType(module_name)

    class UnusedResource:
        """任何实例化都表示关闭路径发生反向激活。"""

        def __init__(self) -> None:
            raise AssertionError("unused resource must not be materialized")

        def start(self) -> None:
            raise AssertionError("unused resource must not start")

        def stop(self) -> None:
            raise AssertionError("unused resource must not stop")

    module.UnusedResource = UnusedResource
    monkeypatch.setitem(sys.modules, module_name, module)
    _write_manifest(
        tmp_path,
        capability_id="fixture.unused",
        kind=MANAGED_RESOURCE_SYNC_KIND,
        entrypoint=f"{module_name}:UnusedResource",
    )
    configure_managed_resource_runtime(_runtime(tmp_path))

    asyncio.run(shutdown_managed_resource_runtime(reason="test_shutdown"))


def test_startup_initializer_discovers_manifest_without_importing_resource() -> None:
    """启动装配只能读取声明，不得提前导入或构造虚拟显示实现。"""
    script = """
import asyncio
import sys
from app.startup.managed_resources_initializer import (
    init_managed_resources,
    stop_managed_resources,
)

runtime = init_managed_resources()
assert runtime.get_running("host.display") is None
assert "app.adapters.system.display.resource" not in sys.modules
assert "pyvirtualdisplay" not in sys.modules
asyncio.run(stop_managed_resources())
assert "app.adapters.system.display.resource" not in sys.modules
assert "pyvirtualdisplay" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_startup_shutdown_without_init_does_not_build_registry(monkeypatch) -> None:
    """未执行启动装配时，关闭入口不得通过发现声明反向初始化 Runtime。"""
    from app.startup import managed_resources_initializer

    build_registry = MagicMock(side_effect=AssertionError("must not discover"))
    monkeypatch.setattr(
        managed_resources_initializer,
        "_managed_resource_runtime",
        None,
    )
    monkeypatch.setattr(
        managed_resources_initializer,
        "build_managed_resource_registry",
        build_registry,
    )

    asyncio.run(managed_resources_initializer.stop_managed_resources())

    build_registry.assert_not_called()
