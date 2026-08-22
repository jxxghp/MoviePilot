from __future__ import annotations

import asyncio
import importlib
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.runtime.capabilities.errors import (
    CapabilityOperationError,
    CapabilityRuntimeClosedError,
)
from app.runtime.capabilities.model import (
    CapabilityLifecycleState,
    CapabilityMaterializationState,
)


@pytest.fixture
def anyio_backend():
    """使用 asyncio 后端运行 anyio 异步测试。

    Agent Capability Runtime 的启动/关闭路径直接使用 ``asyncio.create_task`` /
    ``asyncio.get_running_loop`` 等仅限 asyncio 的原语，在 trio 后端下没有
    running asyncio loop，必然以 ``RuntimeError: no running event loop`` 失败。
    """
    return "asyncio"


@pytest.fixture
def runtime_loader(monkeypatch):
    """为每个用例提供未构建、未关闭的 Agent Capability Runtime。"""
    from app.agent import runtime_loader as module

    monkeypatch.setattr(module, "_agent_runtime", None)
    for implementation_module in (
        "app.agent.orchestrator",
        "app.agent.tools.factory",
    ):
        monkeypatch.delitem(sys.modules, implementation_module, raising=False)
    return module


class _FakeManager:
    """记录异步 service 生命周期，并支持测试控制初始化时序。"""

    def __init__(self) -> None:
        self.initialize_calls = 0
        self.close_calls = 0
        self.fail_initialize = False
        self.initialize_entered: asyncio.Event | None = None
        self.initialize_release: asyncio.Event | None = None

    async def initialize(self) -> None:
        self.initialize_calls += 1
        if self.initialize_entered is not None:
            self.initialize_entered.set()
        if self.initialize_release is not None:
            await self.initialize_release.wait()
        if self.fail_initialize:
            raise RuntimeError("service initialization failed")

    async def close(self) -> None:
        self.close_calls += 1


def _fake_agent_modules(manager: object | None = None) -> dict[str, types.ModuleType]:
    orchestrator = types.ModuleType("app.agent.orchestrator")
    orchestrator.agent_manager = manager if manager is not None else object()
    orchestrator.MoviePilotAgent = type("MoviePilotAgent", (), {})
    tools = types.ModuleType("app.agent.tools.factory")
    tools.MoviePilotToolFactory = type("MoviePilotToolFactory", (), {})
    return {
        orchestrator.__name__: orchestrator,
        tools.__name__: tools,
    }


def test_registry_discovery_does_not_import_agent_implementation(
    runtime_loader,
    monkeypatch,
) -> None:
    """声明发现只能读取 TOML，不得导入 Agent、LLM 或工具实现。"""
    imported = []
    monkeypatch.setattr(
        "app.agent.capabilities.adapter.importlib.import_module",
        lambda name: imported.append(name),
    )

    runtime = runtime_loader._ensure_runtime()

    assert {spec.id for spec in runtime.list_specs()} == {
        "agent.manager",
        "agent.moviepilot_type",
        "agent.service",
        "agent.tool_factory",
    }
    assert imported == []


def test_concurrent_manager_first_use_is_single_flight(
    runtime_loader,
    monkeypatch,
) -> None:
    """并发首用必须只导入一次并向全部调用者发布同一 canonical 对象。"""
    modules = _fake_agent_modules()
    import_calls = []
    import_entered = threading.Event()
    import_release = threading.Event()

    def import_module(name: str):
        import_calls.append(name)
        import_entered.set()
        assert import_release.wait(timeout=5)
        monkeypatch.setitem(sys.modules, name, modules[name])
        return modules[name]

    monkeypatch.setattr(
        "app.agent.capabilities.adapter.importlib.import_module",
        import_module,
    )
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(runtime_loader.get_agent_manager) for _ in range(8)]
        assert import_entered.wait(timeout=5)
        import_release.set()
        results = [future.result(timeout=5) for future in futures]

    assert all(
        result is modules["app.agent.orchestrator"].agent_manager for result in results
    )
    assert (
        runtime_loader.get_moviepilot_agent_type()
        is modules["app.agent.orchestrator"].MoviePilotAgent
    )
    manager_snapshot = runtime_loader._agent_runtime.snapshot("agent.manager")
    assert manager_snapshot.materialization is CapabilityMaterializationState.RESOLVED
    assert manager_snapshot.lifecycle is CapabilityLifecycleState.DISCOVERED
    assert manager_snapshot.visible is False
    assert import_calls == ["app.agent.orchestrator"]


def test_tool_factory_has_independent_first_use_entrypoint(
    runtime_loader,
    monkeypatch,
) -> None:
    """工具工厂可独立首用，不需要先解析完整 Agent 编排模块。"""
    modules = _fake_agent_modules()
    import_calls = []

    def import_module(name: str):
        import_calls.append(name)
        monkeypatch.setitem(sys.modules, name, modules[name])
        return modules[name]

    monkeypatch.setattr(
        "app.agent.capabilities.adapter.importlib.import_module",
        import_module,
    )

    assert runtime_loader.is_tool_factory_materialized() is False
    assert (
        runtime_loader.get_tool_factory()
        is modules["app.agent.tools.factory"].MoviePilotToolFactory
    )
    assert runtime_loader.is_tool_factory_materialized() is True
    assert import_calls == ["app.agent.tools.factory"]


def test_materialization_query_does_not_construct_runtime(
    runtime_loader,
    monkeypatch,
) -> None:
    """只读物化查询在 Runtime 未构建时必须直接返回 False。"""
    build_calls = []
    monkeypatch.setattr(
        runtime_loader,
        "_build_agent_runtime",
        lambda: build_calls.append(True),
    )

    assert runtime_loader.is_tool_factory_materialized() is False
    assert runtime_loader.get_running_agent_manager() is None
    assert build_calls == []


@pytest.mark.anyio
async def test_service_first_and_entrypoint_first_share_canonical_identity(
    runtime_loader,
    monkeypatch,
) -> None:
    """资源轴和兼容物化轴无论谁先解析，都必须共享 canonical manager。"""
    manager = _FakeManager()
    modules = _fake_agent_modules(manager)
    monkeypatch.setattr(
        "app.agent.capabilities.adapter.settings.AI_AGENT_ENABLE",
        True,
    )

    def import_module(name: str):
        monkeypatch.setitem(sys.modules, name, modules[name])
        return modules[name]

    monkeypatch.setattr(
        "app.agent.capabilities.adapter.importlib.import_module",
        import_module,
    )

    service_first = await runtime_loader.activate_agent_service()
    compat_after = runtime_loader.get_agent_manager()

    assert service_first is manager
    assert compat_after is manager
    assert runtime_loader.get_running_agent_manager() is manager
    assert manager.initialize_calls == 1
    snapshot = runtime_loader._agent_runtime.snapshot("agent.service")
    assert snapshot.lifecycle is CapabilityLifecycleState.RUNNING
    assert snapshot.visible is True

    await runtime_loader.begin_agent_shutdown()

    assert manager.close_calls == 1
    assert runtime_loader.get_running_agent_manager() is None
    with pytest.raises(CapabilityRuntimeClosedError):
        runtime_loader.get_agent_manager()
    with pytest.raises(CapabilityRuntimeClosedError):
        runtime_loader.get_moviepilot_agent_type()
    with pytest.raises(CapabilityRuntimeClosedError):
        runtime_loader.get_tool_factory()
    with pytest.raises(CapabilityRuntimeClosedError):
        await runtime_loader.activate_agent_service()


@pytest.mark.anyio
async def test_entrypoint_first_then_service_initializes_same_manager_once(
    runtime_loader,
    monkeypatch,
) -> None:
    """兼容 getter 先物化时不初始化，随后 service 只初始化同一对象一次。"""
    manager = _FakeManager()
    modules = _fake_agent_modules(manager)
    monkeypatch.setattr(
        "app.agent.capabilities.adapter.settings.AI_AGENT_ENABLE",
        True,
    )

    def import_module(name: str):
        monkeypatch.setitem(sys.modules, name, modules[name])
        return modules[name]

    monkeypatch.setattr(
        "app.agent.capabilities.adapter.importlib.import_module",
        import_module,
    )

    assert runtime_loader.get_agent_manager() is manager
    assert manager.initialize_calls == 0
    assert await runtime_loader.activate_agent_service() is manager
    assert await runtime_loader.activate_agent_service() is manager
    assert manager.initialize_calls == 1


@pytest.mark.anyio
async def test_concurrent_service_first_use_initializes_once(
    runtime_loader,
    monkeypatch,
) -> None:
    """并发 service 首启只能 initialize 一次并发布同一实例。"""
    manager = _FakeManager()
    manager.initialize_entered = asyncio.Event()
    manager.initialize_release = asyncio.Event()
    modules = _fake_agent_modules(manager)
    monkeypatch.setattr(
        "app.agent.capabilities.adapter.settings.AI_AGENT_ENABLE",
        True,
    )
    monkeypatch.setattr(
        "app.agent.capabilities.adapter.importlib.import_module",
        lambda name: (
            monkeypatch.setitem(sys.modules, name, modules[name]) or modules[name]
        ),
    )

    tasks = [
        asyncio.create_task(runtime_loader.activate_agent_service()) for _ in range(8)
    ]
    await manager.initialize_entered.wait()
    assert runtime_loader.get_agent_manager() is manager
    assert runtime_loader.get_running_agent_manager() is None
    manager.initialize_release.set()
    results = await asyncio.gather(*tasks)

    assert all(result is manager for result in results)
    assert manager.initialize_calls == 1
    assert runtime_loader.get_running_agent_manager() is manager


@pytest.mark.anyio
async def test_concurrent_service_and_entrypoint_first_use_share_identity(
    runtime_loader,
    monkeypatch,
) -> None:
    """两个 spec 并发首解析时也只能引用同一个 canonical manager。"""
    manager = _FakeManager()
    modules = _fake_agent_modules(manager)
    import_barrier = threading.Barrier(2)
    import_calls = []
    monkeypatch.setattr(
        "app.agent.capabilities.adapter.settings.AI_AGENT_ENABLE",
        True,
    )

    def import_module(name: str):
        import_calls.append(name)
        import_barrier.wait(timeout=5)
        monkeypatch.setitem(sys.modules, name, modules[name])
        return modules[name]

    monkeypatch.setattr(
        "app.agent.capabilities.adapter.importlib.import_module",
        import_module,
    )

    service_task = asyncio.create_task(runtime_loader.activate_agent_service())
    entrypoint_task = asyncio.create_task(
        asyncio.to_thread(runtime_loader.get_agent_manager)
    )
    service, entrypoint = await asyncio.gather(service_task, entrypoint_task)

    assert service is manager
    assert entrypoint is manager
    assert runtime_loader.get_running_agent_manager() is manager
    assert manager.initialize_calls == 1
    assert import_calls == ["app.agent.orchestrator"] * 2

    await runtime_loader.begin_agent_shutdown()


@pytest.mark.anyio
async def test_service_failure_is_not_published_and_requires_retry(
    runtime_loader,
    monkeypatch,
) -> None:
    """初始化失败必须清理候选、保持不可见，并要求显式 retry。"""
    manager = _FakeManager()
    manager.fail_initialize = True
    modules = _fake_agent_modules(manager)
    monkeypatch.setattr(
        "app.agent.capabilities.adapter.settings.AI_AGENT_ENABLE",
        True,
    )
    monkeypatch.setattr(
        "app.agent.capabilities.adapter.importlib.import_module",
        lambda name: (
            monkeypatch.setitem(sys.modules, name, modules[name]) or modules[name]
        ),
    )

    with pytest.raises(CapabilityOperationError, match="initialization failed"):
        await runtime_loader.activate_agent_service()

    assert runtime_loader.get_running_agent_manager() is None
    failed = runtime_loader._agent_runtime.snapshot("agent.service")
    assert failed.lifecycle is CapabilityLifecycleState.FAILED
    assert failed.visible is False
    assert manager.close_calls == 1
    with pytest.raises(CapabilityOperationError, match="retry=True"):
        await runtime_loader.activate_agent_service()

    manager.fail_initialize = False
    assert await runtime_loader.activate_agent_service(retry=True) is manager
    assert manager.initialize_calls == 2


@pytest.mark.anyio
async def test_shutdown_racing_service_first_use_fails_closed(
    runtime_loader,
    monkeypatch,
) -> None:
    """关闭与 service 首启竞争时不得发布对象，并须清理已初始化候选。"""
    manager = _FakeManager()
    manager.initialize_entered = asyncio.Event()
    manager.initialize_release = asyncio.Event()
    modules = _fake_agent_modules(manager)
    monkeypatch.setattr(
        "app.agent.capabilities.adapter.settings.AI_AGENT_ENABLE",
        True,
    )
    monkeypatch.setattr(
        "app.agent.capabilities.adapter.importlib.import_module",
        lambda name: (
            monkeypatch.setitem(sys.modules, name, modules[name]) or modules[name]
        ),
    )

    activation = asyncio.create_task(runtime_loader.activate_agent_service())
    await manager.initialize_entered.wait()
    shutdown = asyncio.create_task(runtime_loader.begin_agent_shutdown())
    await asyncio.sleep(0)
    manager.initialize_release.set()

    with pytest.raises(CapabilityRuntimeClosedError):
        await activation
    await shutdown

    assert runtime_loader.get_running_agent_manager() is None
    assert manager.initialize_calls == 1
    assert manager.close_calls == 1
    snapshot = runtime_loader._agent_runtime.snapshot("agent.service")
    assert snapshot.lifecycle is CapabilityLifecycleState.STOPPED
    assert snapshot.visible is False


@pytest.mark.anyio
async def test_disabled_service_reconcile_stays_unmaterialized(
    runtime_loader,
    monkeypatch,
) -> None:
    """selector 为 false 时协调结果为空且不得导入 orchestrator。"""
    imported = []
    monkeypatch.setattr(
        "app.agent.capabilities.adapter.settings.AI_AGENT_ENABLE",
        False,
    )
    monkeypatch.setattr(
        "app.agent.capabilities.adapter.importlib.import_module",
        lambda name: imported.append(name),
    )

    assert await runtime_loader.activate_agent_service() is None
    assert runtime_loader.get_running_agent_manager() is None
    snapshot = runtime_loader._agent_runtime.snapshot("agent.service")
    assert snapshot.materialization is CapabilityMaterializationState.UNRESOLVED
    assert snapshot.lifecycle is CapabilityLifecycleState.STOPPED
    assert imported == []


@pytest.mark.anyio
async def test_empty_shutdown_does_not_import_agent_or_tools(
    runtime_loader,
    monkeypatch,
) -> None:
    """空载关闭只解析 data-only manifests，不导入编排器或工具实现。"""
    imported = []
    monkeypatch.setattr(
        "app.agent.capabilities.adapter.importlib.import_module",
        lambda name: imported.append(name),
    )

    await runtime_loader.begin_agent_shutdown()

    assert imported == []
    assert runtime_loader.get_running_agent_manager() is None
    assert runtime_loader.is_tool_factory_materialized() is False


@pytest.mark.anyio
async def test_disable_reconcile_waits_for_concurrent_service_start(
    runtime_loader,
    monkeypatch,
) -> None:
    """关闭配置与首启竞争时必须等待启动并最终撤销实例。"""
    manager = _FakeManager()
    manager.initialize_entered = asyncio.Event()
    manager.initialize_release = asyncio.Event()
    modules = _fake_agent_modules(manager)
    monkeypatch.setattr(
        "app.agent.capabilities.adapter.settings.AI_AGENT_ENABLE",
        True,
    )
    monkeypatch.setattr(
        "app.agent.capabilities.adapter.importlib.import_module",
        lambda name: (
            monkeypatch.setitem(sys.modules, name, modules[name]) or modules[name]
        ),
    )

    activation = asyncio.create_task(runtime_loader.activate_agent_service())
    await manager.initialize_entered.wait()
    monkeypatch.setattr(
        "app.agent.capabilities.adapter.settings.AI_AGENT_ENABLE",
        False,
    )
    disable = asyncio.create_task(
        runtime_loader.reconcile_agent_service(
            reason="config_changed",
            changed_keys={"AI_AGENT_ENABLE"},
            retry=True,
        )
    )
    await asyncio.sleep(0)
    manager.initialize_release.set()

    assert await activation is manager
    assert await disable is None
    assert runtime_loader.get_running_agent_manager() is None
    assert manager.initialize_calls == 1
    assert manager.close_calls == 1


@pytest.mark.anyio
async def test_config_reconcile_hot_switches_service_generations(
    runtime_loader,
    monkeypatch,
) -> None:
    """watch 命中的配置切换应停止并重启同一 canonical service。"""
    manager = _FakeManager()
    modules = _fake_agent_modules(manager)
    monkeypatch.setattr(
        "app.agent.capabilities.adapter.importlib.import_module",
        lambda name: (
            monkeypatch.setitem(sys.modules, name, modules[name]) or modules[name]
        ),
    )
    monkeypatch.setattr(
        "app.agent.capabilities.adapter.settings.AI_AGENT_ENABLE",
        False,
    )

    assert await runtime_loader.activate_agent_service() is None
    assert (
        await runtime_loader.reconcile_agent_service(
            reason="config_changed",
            changed_keys={"UNRELATED"},
            retry=True,
        )
        is None
    )
    assert manager.initialize_calls == 0

    monkeypatch.setattr(
        "app.agent.capabilities.adapter.settings.AI_AGENT_ENABLE",
        True,
    )
    assert (
        await runtime_loader.reconcile_agent_service(
            reason="config_changed",
            changed_keys={"AI_AGENT_ENABLE"},
            retry=True,
        )
        is manager
    )
    assert manager.initialize_calls == 1

    monkeypatch.setattr(
        "app.agent.capabilities.adapter.settings.AI_AGENT_ENABLE",
        False,
    )
    assert (
        await runtime_loader.reconcile_agent_service(
            reason="config_changed",
            changed_keys={"AI_AGENT_ENABLE"},
            retry=True,
        )
        is None
    )
    assert manager.close_calls == 1
    assert runtime_loader.get_running_agent_manager() is None

    monkeypatch.setattr(
        "app.agent.capabilities.adapter.settings.AI_AGENT_ENABLE",
        True,
    )
    assert (
        await runtime_loader.reconcile_agent_service(
            reason="config_changed",
            changed_keys={"AI_AGENT_ENABLE"},
            retry=True,
        )
        is manager
    )
    assert manager.initialize_calls == 2
    assert runtime_loader._agent_runtime.snapshot("agent.service").generation == 3


@pytest.mark.anyio
async def test_config_reconcile_after_shutdown_cannot_restart_service(
    runtime_loader,
    monkeypatch,
) -> None:
    """Runtime 关闭后即使 selector 再次为 true，配置协调也必须拒绝重启。"""
    manager = _FakeManager()
    modules = _fake_agent_modules(manager)
    monkeypatch.setattr(
        "app.agent.capabilities.adapter.settings.AI_AGENT_ENABLE",
        True,
    )
    monkeypatch.setattr(
        "app.agent.capabilities.adapter.importlib.import_module",
        lambda name: (
            monkeypatch.setitem(sys.modules, name, modules[name]) or modules[name]
        ),
    )

    assert await runtime_loader.activate_agent_service() is manager
    await runtime_loader.begin_agent_shutdown()

    with pytest.raises(CapabilityRuntimeClosedError):
        await runtime_loader.reconcile_agent_service(
            reason="config_changed",
            changed_keys={"AI_AGENT_ENABLE"},
            retry=True,
        )
    assert manager.initialize_calls == 1
    assert manager.close_calls == 1
    assert runtime_loader.get_running_agent_manager() is None


@pytest.mark.anyio
async def test_real_agent_manager_can_restart_across_config_generations(
    runtime_loader,
    monkeypatch,
) -> None:
    """真实 AgentManager 在配置热切换后必须重新建立运行代际。"""
    orchestrator = importlib.import_module("app.agent.orchestrator")

    manager = orchestrator.AgentManager()
    memory_events = []

    async def close_memory() -> None:
        memory_events.append("close")

    monkeypatch.setattr(
        orchestrator.memory_manager,
        "initialize",
        lambda: memory_events.append("initialize"),
    )
    monkeypatch.setattr(orchestrator.memory_manager, "close", close_memory)
    monkeypatch.setattr(orchestrator, "agent_manager", manager)
    monkeypatch.setattr(
        "app.agent.capabilities.adapter.settings.AI_AGENT_ENABLE",
        False,
    )

    assert await runtime_loader.activate_agent_service() is None

    monkeypatch.setattr(
        "app.agent.capabilities.adapter.settings.AI_AGENT_ENABLE",
        True,
    )
    assert (
        await runtime_loader.reconcile_agent_service(
            reason="config_changed",
            changed_keys={"AI_AGENT_ENABLE"},
            retry=True,
        )
        is manager
    )
    assert manager._accepting_tasks is True

    monkeypatch.setattr(
        "app.agent.capabilities.adapter.settings.AI_AGENT_ENABLE",
        False,
    )
    assert (
        await runtime_loader.reconcile_agent_service(
            reason="config_changed",
            changed_keys={"AI_AGENT_ENABLE"},
            retry=True,
        )
        is None
    )
    assert manager._accepting_tasks is False

    monkeypatch.setattr(
        "app.agent.capabilities.adapter.settings.AI_AGENT_ENABLE",
        True,
    )
    assert (
        await runtime_loader.reconcile_agent_service(
            reason="config_changed",
            changed_keys={"AI_AGENT_ENABLE"},
            retry=True,
        )
        is manager
    )
    assert manager._accepting_tasks is True
    assert memory_events == ["initialize", "close", "initialize"]

    await runtime_loader.begin_agent_shutdown()

    assert manager._accepting_tasks is False
    assert memory_events == ["initialize", "close", "initialize", "close"]
