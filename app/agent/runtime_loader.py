"""Agent 重量级 canonical 对象的轻量首用入口。"""

from __future__ import annotations

import sys
import threading
from typing import Any

from app.agent.capabilities import (
    AGENT_ENTRYPOINT_KIND,
    AGENT_MANAGER_CAPABILITY_ID,
    AGENT_SERVICE_CAPABILITY_ID,
    AGENT_SERVICE_KIND,
    MOVIEPILOT_AGENT_TYPE_CAPABILITY_ID,
    TOOL_FACTORY_CAPABILITY_ID,
)
from app.agent.capabilities.adapter import (
    AgentEntrypointAdapter,
    AgentServiceAdapter,
    build_agent_capability_registry,
    should_run_agent_service,
)
from app.runtime.capabilities.model import CapabilityMaterializationState
from app.runtime.capabilities.runtime import CapabilityRuntime


_runtime_lock = threading.RLock()
_agent_runtime: CapabilityRuntime | None = None


def _build_agent_runtime() -> CapabilityRuntime:
    """装配 Agent Runtime；构建阶段只解析 manifests。"""
    return CapabilityRuntime(
        build_agent_capability_registry(),
        adapters={
            AGENT_ENTRYPOINT_KIND: AgentEntrypointAdapter(),
            AGENT_SERVICE_KIND: AgentServiceAdapter(),
        },
    )


def _ensure_runtime() -> CapabilityRuntime:
    """返回进程唯一 Runtime，同进程关闭后不重新创建。"""
    global _agent_runtime
    with _runtime_lock:
        if _agent_runtime is None:
            _agent_runtime = _build_agent_runtime()
        return _agent_runtime


def _materialize_entrypoint(capability_id: str) -> Any:
    """通过通用 Runtime 完成并发 single-flight 物化，不声明资源运行态。"""
    return _ensure_runtime().materialize(
        capability_id,
        reason="agent_entrypoint_first_use",
    )


def get_agent_manager() -> Any:
    """返回 canonical Agent Manager；关闭门禁生效后稳定拒绝首用。"""
    return _materialize_entrypoint(AGENT_MANAGER_CAPABILITY_ID)


async def reconcile_agent_service(
    *,
    reason: str,
    changed_keys: set[str] | None = None,
    retry: bool = False,
) -> Any | None:
    """按 manifest watch/selector 协调唯一 Agent Service 生命周期。"""
    runtime = _ensure_runtime()
    spec = runtime.get_spec(AGENT_SERVICE_CAPABILITY_ID)
    if spec is None:
        raise RuntimeError("缺少 agent.service capability")
    if changed_keys is not None and not changed_keys.intersection(spec.watch):
        return runtime.get_running(AGENT_SERVICE_CAPABILITY_ID)
    if not should_run_agent_service(spec):
        # stop_async 会等待并发首启后再撤销实例；未物化能力则保持零导入。
        await runtime.stop_async(
            AGENT_SERVICE_CAPABILITY_ID,
            reason=reason,
        )
        return None
    return await runtime.activate_async(
        AGENT_SERVICE_CAPABILITY_ID,
        reason=reason,
        retry=retry,
    )


async def activate_agent_service(*, retry: bool = False) -> Any | None:
    """执行启动期协调；selector 未启用时保持 service 未物化。"""
    return await reconcile_agent_service(
        reason="agent_service_startup_reconcile",
        retry=retry,
    )


def get_running_agent_manager() -> Any | None:
    """只读返回 RUNNING Agent Service；未构建 Runtime 时不触发声明发现。"""
    with _runtime_lock:
        runtime = _agent_runtime
    if runtime is None:
        return None
    return runtime.get_running(AGENT_SERVICE_CAPABILITY_ID)


def get_moviepilot_agent_type() -> type:
    """返回 canonical MoviePilotAgent 类型。"""
    agent_type = _materialize_entrypoint(MOVIEPILOT_AGENT_TYPE_CAPABILITY_ID)
    if not isinstance(agent_type, type):
        raise TypeError("MoviePilot Agent entrypoint 必须是类型")
    return agent_type


def get_tool_factory() -> type:
    """返回 canonical 工具工厂类型。"""
    factory_type = _materialize_entrypoint(TOOL_FACTORY_CAPABILITY_ID)
    if not isinstance(factory_type, type):
        raise TypeError("Agent Tool Factory entrypoint 必须是类型")
    return factory_type


def is_tool_factory_materialized() -> bool:
    """只读判断工具工厂是否已解析；未建 Runtime 时不触发发现或导入。"""
    with _runtime_lock:
        runtime = _agent_runtime
    if runtime is None:
        return False
    return (
        runtime.snapshot(TOOL_FACTORY_CAPABILITY_ID).materialization
        is CapabilityMaterializationState.RESOLVED
    )


async def close_materialized_terminal_sessions() -> None:
    """关闭已物化的终端会话管理器，不触发新的 Agent 工具导入。"""
    module = sys.modules.get("app.agent.tools.impl._terminal_session")
    manager = getattr(module, "terminal_session_manager", None) if module else None
    close = getattr(manager, "close", None)
    if callable(close):
        await close()


async def begin_agent_shutdown() -> None:
    """不可逆关闭首用闸门，并等待全部同步及异步能力释放。"""
    try:
        await _ensure_runtime().shutdown_async(reason="application_shutdown")
    finally:
        await close_materialized_terminal_sessions()
