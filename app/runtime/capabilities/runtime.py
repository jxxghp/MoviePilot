from __future__ import annotations

import asyncio
import inspect
import sys
import threading
import time
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional

from app.runtime.capabilities.errors import (
    CapabilityAdapterContractError,
    CapabilityAdapterModeError,
    CapabilityOperationError,
    CapabilityRuntimeClosedError,
)
from app.runtime.capabilities.model import (
    AdapterExecutionMode,
    CapabilityLifecycleState,
    CapabilityMaterializationState,
    CapabilityObservation,
    CapabilitySnapshot,
    CapabilitySpec,
)
from app.runtime.capabilities.registry import CapabilityRegistry


@dataclass(slots=True)
class _CapabilityState:
    """Runtime 私有状态；所有可变字段均由 lock 保护。"""

    spec: CapabilitySpec
    lock: threading.RLock = field(default_factory=threading.RLock)
    materialization: CapabilityMaterializationState = CapabilityMaterializationState.UNRESOLVED
    lifecycle: CapabilityLifecycleState = CapabilityLifecycleState.DISCOVERED
    generation: int = 0
    implementation: Any = None
    instance: Any = None
    # 撤销可见性后仍未确认释放的资源由 Runtime 持有，禁止并行创建第二实例。
    pending_stop: Any = None
    pending_cleanup: Any = None
    pending_cleanup_error: Optional[BaseException] = None
    last_error: Optional[BaseException] = None
    inflight: Optional[Future[Any]] = None
    inflight_operation: Optional[str] = None


class CapabilityRuntime:
    """协调能力实现物化和资源生命周期的通用运行时。"""

    def __init__(
        self,
        registry: CapabilityRegistry,
        *,
        adapters: Mapping[str, Any],
        observer: Optional[Callable[[CapabilityObservation], None]] = None,
        observation_limit: int = 1024,
    ) -> None:
        if observation_limit <= 0:
            raise ValueError("observation_limit 必须大于 0")
        missing_adapters = {spec.kind for spec in registry.list_specs()} - set(adapters)
        if missing_adapters:
            raise CapabilityAdapterContractError(
                f"缺少 capability adapter：{sorted(missing_adapters)}"
            )
        self._registry = registry
        self._adapters = MappingProxyType(dict(adapters))
        self._states = {
            spec.id: _CapabilityState(spec=spec)
            for spec in registry.list_specs()
        }
        self._observer = observer
        self._observations: deque[CapabilityObservation] = deque(maxlen=observation_limit)
        self._observation_lock = threading.Lock()
        self._shutdown_lock = threading.Lock()
        self._shutdown = False

    @property
    def is_shutdown(self) -> bool:
        """返回 Runtime 是否已进入不可逆关闭态。"""
        return self._shutdown

    def _state(self, capability_id: str) -> _CapabilityState:
        self._registry.require_spec(capability_id)
        return self._states[capability_id]

    def get_spec(self, capability_id: str) -> CapabilitySpec | None:
        """返回保留在 Registry 中的声明，不受运行失败影响。"""
        return self._registry.get_spec(capability_id)

    def list_specs(self) -> tuple[CapabilitySpec, ...]:
        """返回全部声明；FAILED 能力不会从列表中消失。"""
        return self._registry.list_specs()

    def _adapter(self, state: _CapabilityState, expected: AdapterExecutionMode) -> Any:
        adapter = self._adapters[state.spec.kind]
        actual = getattr(adapter, "execution_mode", None)
        if actual is not expected:
            raise CapabilityAdapterModeError(
                f"能力 {state.spec.id} 的 adapter mode={actual!r}，"
                f"不能通过 {expected.value} 入口调用"
            )
        return adapter

    def _ensure_open(self) -> None:
        if self._shutdown:
            raise CapabilityRuntimeClosedError("Capability Runtime 已关闭")

    @staticmethod
    def _sync_callback(adapter: Any, name: str, *args) -> Any:
        callback = getattr(adapter, name, None)
        if not callable(callback):
            raise CapabilityAdapterContractError(f"同步 adapter 缺少 {name}()")
        result = callback(*args)
        if inspect.isawaitable(result):
            close = getattr(result, "close", None)
            if callable(close):
                close()
            raise CapabilityAdapterContractError(
                f"同步 adapter 的 {name}() 不能返回 awaitable"
            )
        return result

    @staticmethod
    async def _async_callback(adapter: Any, name: str, *args) -> Any:
        callback = getattr(adapter, name, None)
        if not callable(callback):
            raise CapabilityAdapterContractError(f"异步 adapter 缺少 {name}()")
        result = callback(*args)
        if not inspect.isawaitable(result):
            raise CapabilityAdapterContractError(
                f"异步 adapter 的 {name}() 必须返回 awaitable"
            )
        return await result

    @staticmethod
    def _wait_sync(future: Future[Any]) -> Any:
        return future.result()

    @staticmethod
    async def _wait_async(future: Future[Any]) -> Any:
        return await asyncio.shield(asyncio.wrap_future(future))

    def _emit(
        self,
        state: _CapabilityState,
        *,
        generation: int,
        operation: str,
        outcome: str,
        reason: str,
        started_at: float,
        error: Optional[BaseException] = None,
    ) -> None:
        with state.lock:
            observation = CapabilityObservation(
                capability_id=state.spec.id,
                generation=generation,
                operation=operation,
                outcome=outcome,
                reason=reason,
                materialization=state.materialization,
                lifecycle=state.lifecycle,
                duration_ms=max(0.0, (time.monotonic() - started_at) * 1000),
                error=str(error) if error else None,
            )
        with self._observation_lock:
            self._observations.append(observation)
        if self._observer:
            try:
                self._observer(observation)
            except Exception:
                # 观测消费者不能改变能力生命周期的成功或失败语义。
                pass

    @staticmethod
    def _finish_transition(
        state: _CapabilityState,
        future: Future[Any],
        *,
        result: Any = None,
        error: Optional[BaseException] = None,
    ) -> None:
        with state.lock:
            if state.inflight is future:
                state.inflight = None
                state.inflight_operation = None
        if error is None:
            future.set_result(result)
        else:
            future.set_exception(error)

    def _calibrate_consumer_materialization(self, state: _CapabilityState) -> None:
        """从 sys.modules 校准显式 consumer import，不触发任何新导入。"""
        module_name, symbol_name = state.spec.entrypoint.split(":", maxsplit=1)
        module = sys.modules.get(module_name)
        namespace = getattr(module, "__dict__", None) if module is not None else None
        if not isinstance(namespace, dict) or symbol_name not in namespace:
            return
        canonical = namespace[symbol_name]
        with state.lock:
            if state.inflight is not None:
                return
            if state.materialization is CapabilityMaterializationState.RESOLVED:
                if state.implementation is not canonical:
                    state.last_error = CapabilityAdapterContractError(
                        f"能力 {state.spec.id} 的 canonical implementation identity 发生变化"
                    )
                return
            state.implementation = canonical
            state.materialization = CapabilityMaterializationState.RESOLVED
            if state.lifecycle is CapabilityLifecycleState.DISCOVERED:
                state.last_error = None
            generation = state.generation
        self._emit(
            state,
            generation=generation,
            operation="consumer_materialize",
            outcome="succeeded",
            reason="sys_modules",
            started_at=time.monotonic(),
        )

    def snapshot(self, capability_id: str) -> CapabilitySnapshot:
        """返回状态快照，并校准外部显式导入产生的 canonical 对象。"""
        state = self._state(capability_id)
        self._calibrate_consumer_materialization(state)
        with state.lock:
            return CapabilitySnapshot(
                capability_id=state.spec.id,
                materialization=state.materialization,
                lifecycle=state.lifecycle,
                generation=state.generation,
                visible=state.instance is not None,
                error=str(state.last_error) if state.last_error else None,
            )

    def observations(self, capability_id: Optional[str] = None) -> tuple[CapabilityObservation, ...]:
        """返回按发生顺序记录的不可变观测快照。"""
        with self._observation_lock:
            items = tuple(self._observations)
        if capability_id is None:
            return items
        return tuple(item for item in items if item.capability_id == capability_id)

    def get_running(self, capability_id: str) -> Any:
        """只查询已发布实例，不触发物化或启动。"""
        state = self._state(capability_id)
        with state.lock:
            return state.instance

    def materialize(self, capability_id: str, *, reason: str, retry: bool = False) -> Any:
        """通过同步 adapter 显式物化实现，不启动领域资源。"""
        state = self._state(capability_id)
        adapter = self._adapter(state, AdapterExecutionMode.SYNC)
        self._calibrate_consumer_materialization(state)
        while True:
            with self._shutdown_lock:
                self._ensure_open()
                with state.lock:
                    if state.materialization is CapabilityMaterializationState.RESOLVED:
                        return state.implementation
                    if state.materialization is CapabilityMaterializationState.FAILED and not retry:
                        raise CapabilityOperationError(
                            state.spec.id,
                            "materialize",
                            RuntimeError("能力处于 FAILED，必须显式 retry=True"),
                        )
                    if state.inflight is not None:
                        waiter = state.inflight
                        waiter_operation = state.inflight_operation
                        owner = None
                    else:
                        state.generation += 1
                        generation = state.generation
                        future: Future[Any] = Future()
                        state.inflight = future
                        state.inflight_operation = "materialize"
                        state.materialization = CapabilityMaterializationState.RESOLVING
                        waiter = None
                        waiter_operation = None
                        owner = (generation, future)
            if waiter is not None:
                result = self._wait_sync(waiter)
                if waiter_operation == "materialize":
                    return result
                continue
            break

        generation, future = owner
        started_at = time.monotonic()
        self._emit(
            state,
            generation=generation,
            operation="materialize",
            outcome="started",
            reason=reason,
            started_at=started_at,
        )
        try:
            implementation = self._sync_callback(adapter, "materialize", state.spec)
            implementation = self._canonical_implementation(state.spec, implementation)
            self._ensure_open()
            with state.lock:
                state.implementation = implementation
                state.materialization = CapabilityMaterializationState.RESOLVED
                state.last_error = None
            self._finish_transition(state, future, result=implementation)
            self._emit(
                state,
                generation=generation,
                operation="materialize",
                outcome="succeeded",
                reason=reason,
                started_at=started_at,
            )
            return implementation
        except BaseException as error:
            with state.lock:
                state.materialization = CapabilityMaterializationState.FAILED
                state.last_error = error
            operation_error = self._wrap_error(state.spec.id, "materialize", error)
            self._finish_transition(state, future, error=operation_error)
            self._emit(
                state,
                generation=generation,
                operation="materialize",
                outcome="failed",
                reason=reason,
                started_at=started_at,
                error=error,
            )
            raise operation_error from error

    async def materialize_async(
        self,
        capability_id: str,
        *,
        reason: str,
        retry: bool = False,
    ) -> Any:
        """通过异步 adapter 显式物化实现，不阻塞事件循环。"""
        state = self._state(capability_id)
        adapter = self._adapter(state, AdapterExecutionMode.ASYNC)
        self._calibrate_consumer_materialization(state)
        while True:
            with self._shutdown_lock:
                self._ensure_open()
                with state.lock:
                    if state.materialization is CapabilityMaterializationState.RESOLVED:
                        return state.implementation
                    if state.materialization is CapabilityMaterializationState.FAILED and not retry:
                        raise CapabilityOperationError(
                            state.spec.id,
                            "materialize",
                            RuntimeError("能力处于 FAILED，必须显式 retry=True"),
                        )
                    if state.inflight is not None:
                        waiter = state.inflight
                        waiter_operation = state.inflight_operation
                        owner = None
                    else:
                        state.generation += 1
                        generation = state.generation
                        future: Future[Any] = Future()
                        state.inflight = future
                        state.inflight_operation = "materialize"
                        state.materialization = CapabilityMaterializationState.RESOLVING
                        waiter = None
                        waiter_operation = None
                        owner = (generation, future)
            if waiter is not None:
                result = await self._wait_async(waiter)
                if waiter_operation == "materialize":
                    return result
                continue
            break

        generation, future = owner
        started_at = time.monotonic()
        self._emit(
            state,
            generation=generation,
            operation="materialize",
            outcome="started",
            reason=reason,
            started_at=started_at,
        )
        try:
            implementation = await self._async_callback(adapter, "materialize", state.spec)
            implementation = self._canonical_implementation(state.spec, implementation)
            self._ensure_open()
            with state.lock:
                state.implementation = implementation
                state.materialization = CapabilityMaterializationState.RESOLVED
                state.last_error = None
            self._finish_transition(state, future, result=implementation)
            self._emit(
                state,
                generation=generation,
                operation="materialize",
                outcome="succeeded",
                reason=reason,
                started_at=started_at,
            )
            return implementation
        except BaseException as error:
            with state.lock:
                state.materialization = CapabilityMaterializationState.FAILED
                state.last_error = error
            operation_error = self._wrap_error(state.spec.id, "materialize", error)
            self._finish_transition(state, future, error=operation_error)
            self._emit(
                state,
                generation=generation,
                operation="materialize",
                outcome="failed",
                reason=reason,
                started_at=started_at,
                error=error,
            )
            raise operation_error from error

    @staticmethod
    def _canonical_implementation(spec: CapabilitySpec, implementation: Any) -> Any:
        if implementation is None:
            raise CapabilityAdapterContractError(
                f"adapter 没有返回 {spec.id} 的 implementation"
            )
        module_name, symbol_name = spec.entrypoint.split(":", maxsplit=1)
        module = sys.modules.get(module_name)
        namespace = getattr(module, "__dict__", None) if module is not None else None
        if not isinstance(namespace, dict):
            raise CapabilityAdapterContractError(
                f"adapter 返回 {spec.id} 的 implementation 后，"
                f"canonical 模块 {module_name} 未加载"
            )
        if symbol_name not in namespace:
            raise CapabilityAdapterContractError(
                f"adapter 返回 {spec.id} 的 implementation 后，"
                f"canonical 符号 {spec.entrypoint} 不存在"
            )
        canonical = namespace[symbol_name]
        if implementation is not canonical:
            raise CapabilityAdapterContractError(
                f"adapter 返回的 {spec.id} 实现不是 sys.modules 中的 canonical 对象"
            )
        return canonical

    @staticmethod
    def _wrap_error(capability_id: str, operation: str, error: BaseException) -> BaseException:
        if isinstance(error, (CapabilityOperationError, CapabilityRuntimeClosedError)):
            return error
        return CapabilityOperationError(capability_id, operation, error)

    def activate(self, capability_id: str, *, reason: str, retry: bool = False) -> Any:
        """同步启动能力，并在 start 成功后原子发布候选实例。"""
        return self._activate_sync(capability_id, reason=reason, retry=retry, previous=None)

    async def activate_async(
        self,
        capability_id: str,
        *,
        reason: str,
        retry: bool = False,
    ) -> Any:
        """异步启动能力，并以 Future 协调并发调用者。"""
        return await self._activate_async(capability_id, reason=reason, retry=retry, previous=None)

    def _activate_sync(
        self,
        capability_id: str,
        *,
        reason: str,
        retry: bool,
        previous: Any,
        operation: str = "activate",
    ) -> Any:
        state = self._state(capability_id)
        adapter = self._adapter(state, AdapterExecutionMode.SYNC)
        self._calibrate_consumer_materialization(state)
        while True:
            with self._shutdown_lock:
                self._ensure_open()
                with state.lock:
                    if operation == "activate" and state.instance is not None:
                        return state.instance
                    if state.pending_stop is not None and not retry:
                        raise CapabilityOperationError(
                            state.spec.id,
                            operation,
                            RuntimeError("能力仍持有未释放资源，必须先重试 stop"),
                        )
                    if state.lifecycle is CapabilityLifecycleState.FAILED and not retry:
                        raise CapabilityOperationError(
                            state.spec.id,
                            operation,
                            RuntimeError("能力处于 FAILED，必须显式 retry=True"),
                        )
                    if state.inflight is not None:
                        waiter = state.inflight
                        waiter_operation = state.inflight_operation
                        owner = None
                    else:
                        state.generation += 1
                        generation = state.generation
                        future: Future[Any] = Future()
                        state.inflight = future
                        state.inflight_operation = operation
                        pending_stop = state.pending_stop
                        state.lifecycle = (
                            CapabilityLifecycleState.STOPPING
                            if pending_stop is not None
                            else CapabilityLifecycleState.STARTING
                        )
                        if state.implementation is None:
                            state.materialization = CapabilityMaterializationState.RESOLVING
                        pending_cleanup = state.pending_cleanup
                        pending_error = state.pending_cleanup_error
                        state.pending_stop = None
                        state.pending_cleanup = None
                        state.pending_cleanup_error = None
                        implementation = state.implementation
                        waiter = None
                        waiter_operation = None
                        owner = (
                            generation,
                            future,
                            implementation,
                            pending_stop,
                            pending_cleanup,
                            pending_error,
                        )
            if waiter is not None:
                result = self._wait_sync(waiter)
                if waiter_operation == operation:
                    return result
                continue
            break

        (
            generation,
            future,
            implementation,
            pending_stop,
            pending_cleanup,
            pending_error,
        ) = owner
        started_at = time.monotonic()
        self._emit(
            state,
            generation=generation,
            operation=operation,
            outcome="started",
            reason=reason,
            started_at=started_at,
        )
        candidate = None
        try:
            if pending_stop is not None:
                self._sync_callback(
                    adapter,
                    "stop",
                    state.spec,
                    pending_stop,
                    generation,
                )
                pending_stop = None
                with state.lock:
                    state.lifecycle = CapabilityLifecycleState.STARTING
            if pending_cleanup is not None:
                try:
                    self._sync_callback(
                        adapter,
                        "cleanup",
                        state.spec,
                        pending_cleanup,
                        generation,
                        pending_error or RuntimeError("pending cleanup"),
                    )
                except BaseException:
                    with state.lock:
                        state.pending_cleanup = pending_cleanup
                        state.pending_cleanup_error = pending_error
                    raise
            if implementation is None:
                materialized = self._sync_callback(adapter, "materialize", state.spec)
                implementation = self._canonical_implementation(state.spec, materialized)
            candidate = self._sync_callback(
                adapter,
                "create",
                state.spec,
                implementation,
                generation,
                previous,
            )
            if candidate is None:
                raise CapabilityAdapterContractError(
                    f"adapter 没有返回 {state.spec.id} 的 candidate"
                )
            self._sync_callback(adapter, "start", state.spec, candidate, generation)
            self._ensure_open()
            with state.lock:
                state.implementation = implementation
                state.materialization = CapabilityMaterializationState.RESOLVED
                state.instance = candidate
                state.lifecycle = CapabilityLifecycleState.RUNNING
                state.last_error = None
            self._finish_transition(state, future, result=candidate)
            self._emit(
                state,
                generation=generation,
                operation=operation,
                outcome="succeeded",
                reason=reason,
                started_at=started_at,
            )
            return candidate
        except BaseException as error:
            cleanup_error = self._cleanup_failed_sync(
                state,
                adapter,
                candidate,
                generation,
                error,
            )
            final_error = cleanup_error or error
            with state.lock:
                state.implementation = implementation
                state.materialization = (
                    CapabilityMaterializationState.RESOLVED
                    if implementation is not None
                    else CapabilityMaterializationState.FAILED
                )
                state.instance = None
                if pending_stop is not None:
                    state.pending_stop = pending_stop
                state.lifecycle = (
                    CapabilityLifecycleState.STOPPED
                    if isinstance(error, CapabilityRuntimeClosedError)
                    else CapabilityLifecycleState.FAILED
                )
                state.last_error = final_error
            operation_error = self._wrap_error(state.spec.id, operation, final_error)
            self._finish_transition(state, future, error=operation_error)
            self._emit(
                state,
                generation=generation,
                operation=operation,
                outcome="failed",
                reason=reason,
                started_at=started_at,
                error=final_error,
            )
            raise operation_error from error

    async def _activate_async(
        self,
        capability_id: str,
        *,
        reason: str,
        retry: bool,
        previous: Any,
        operation: str = "activate",
    ) -> Any:
        state = self._state(capability_id)
        adapter = self._adapter(state, AdapterExecutionMode.ASYNC)
        self._calibrate_consumer_materialization(state)
        while True:
            with self._shutdown_lock:
                self._ensure_open()
                with state.lock:
                    if operation == "activate" and state.instance is not None:
                        return state.instance
                    if state.pending_stop is not None and not retry:
                        raise CapabilityOperationError(
                            state.spec.id,
                            operation,
                            RuntimeError("能力仍持有未释放资源，必须先重试 stop"),
                        )
                    if state.lifecycle is CapabilityLifecycleState.FAILED and not retry:
                        raise CapabilityOperationError(
                            state.spec.id,
                            operation,
                            RuntimeError("能力处于 FAILED，必须显式 retry=True"),
                        )
                    if state.inflight is not None:
                        waiter = state.inflight
                        waiter_operation = state.inflight_operation
                        owner = None
                    else:
                        state.generation += 1
                        generation = state.generation
                        future: Future[Any] = Future()
                        state.inflight = future
                        state.inflight_operation = operation
                        pending_stop = state.pending_stop
                        state.lifecycle = (
                            CapabilityLifecycleState.STOPPING
                            if pending_stop is not None
                            else CapabilityLifecycleState.STARTING
                        )
                        if state.implementation is None:
                            state.materialization = CapabilityMaterializationState.RESOLVING
                        pending_cleanup = state.pending_cleanup
                        pending_error = state.pending_cleanup_error
                        state.pending_stop = None
                        state.pending_cleanup = None
                        state.pending_cleanup_error = None
                        implementation = state.implementation
                        waiter = None
                        waiter_operation = None
                        owner = (
                            generation,
                            future,
                            implementation,
                            pending_stop,
                            pending_cleanup,
                            pending_error,
                        )
            if waiter is not None:
                result = await self._wait_async(waiter)
                if waiter_operation == operation:
                    return result
                continue
            break

        (
            generation,
            future,
            implementation,
            pending_stop,
            pending_cleanup,
            pending_error,
        ) = owner
        started_at = time.monotonic()
        self._emit(
            state,
            generation=generation,
            operation=operation,
            outcome="started",
            reason=reason,
            started_at=started_at,
        )
        candidate = None
        try:
            if pending_stop is not None:
                await self._async_callback(
                    adapter,
                    "stop",
                    state.spec,
                    pending_stop,
                    generation,
                )
                pending_stop = None
                with state.lock:
                    state.lifecycle = CapabilityLifecycleState.STARTING
            if pending_cleanup is not None:
                try:
                    await self._async_callback(
                        adapter,
                        "cleanup",
                        state.spec,
                        pending_cleanup,
                        generation,
                        pending_error or RuntimeError("pending cleanup"),
                    )
                except BaseException:
                    with state.lock:
                        state.pending_cleanup = pending_cleanup
                        state.pending_cleanup_error = pending_error
                    raise
            if implementation is None:
                materialized = await self._async_callback(adapter, "materialize", state.spec)
                implementation = self._canonical_implementation(state.spec, materialized)
            candidate = await self._async_callback(
                adapter,
                "create",
                state.spec,
                implementation,
                generation,
                previous,
            )
            if candidate is None:
                raise CapabilityAdapterContractError(
                    f"adapter 没有返回 {state.spec.id} 的 candidate"
                )
            await self._async_callback(adapter, "start", state.spec, candidate, generation)
            self._ensure_open()
            with state.lock:
                state.implementation = implementation
                state.materialization = CapabilityMaterializationState.RESOLVED
                state.instance = candidate
                state.lifecycle = CapabilityLifecycleState.RUNNING
                state.last_error = None
            self._finish_transition(state, future, result=candidate)
            self._emit(
                state,
                generation=generation,
                operation=operation,
                outcome="succeeded",
                reason=reason,
                started_at=started_at,
            )
            return candidate
        except BaseException as error:
            cleanup_error = await self._cleanup_failed_async(
                state,
                adapter,
                candidate,
                generation,
                error,
            )
            final_error = cleanup_error or error
            with state.lock:
                state.implementation = implementation
                state.materialization = (
                    CapabilityMaterializationState.RESOLVED
                    if implementation is not None
                    else CapabilityMaterializationState.FAILED
                )
                state.instance = None
                if pending_stop is not None:
                    state.pending_stop = pending_stop
                state.lifecycle = (
                    CapabilityLifecycleState.STOPPED
                    if isinstance(error, CapabilityRuntimeClosedError)
                    else CapabilityLifecycleState.FAILED
                )
                state.last_error = final_error
            operation_error = self._wrap_error(state.spec.id, operation, final_error)
            self._finish_transition(state, future, error=operation_error)
            self._emit(
                state,
                generation=generation,
                operation=operation,
                outcome="failed",
                reason=reason,
                started_at=started_at,
                error=final_error,
            )
            raise operation_error from error

    def _cleanup_failed_sync(
        self,
        state: _CapabilityState,
        adapter: Any,
        candidate: Any,
        generation: int,
        error: BaseException,
    ) -> Optional[BaseException]:
        if candidate is None:
            return None
        try:
            self._sync_callback(
                adapter,
                "cleanup",
                state.spec,
                candidate,
                generation,
                error,
            )
            return None
        except BaseException as cleanup_error:
            with state.lock:
                state.pending_cleanup = candidate
                state.pending_cleanup_error = cleanup_error
            return RuntimeError(f"{error}; cleanup failed: {cleanup_error}")

    async def _cleanup_failed_async(
        self,
        state: _CapabilityState,
        adapter: Any,
        candidate: Any,
        generation: int,
        error: BaseException,
    ) -> Optional[BaseException]:
        if candidate is None:
            return None
        try:
            await self._async_callback(
                adapter,
                "cleanup",
                state.spec,
                candidate,
                generation,
                error,
            )
            return None
        except BaseException as cleanup_error:
            with state.lock:
                state.pending_cleanup = candidate
                state.pending_cleanup_error = cleanup_error
            return RuntimeError(f"{error}; cleanup failed: {cleanup_error}")

    def reload(self, capability_id: str, *, reason: str) -> Any:
        """撤销当前实例后，通过同步 adapter 完成新 generation 的资源切换。"""
        state = self._state(capability_id)
        adapter = self._adapter(state, AdapterExecutionMode.SYNC)
        while True:
            with self._shutdown_lock:
                self._ensure_open()
                with state.lock:
                    if state.inflight is not None:
                        waiter = state.inflight
                        waiter_operation = state.inflight_operation
                    else:
                        waiter = None
                        waiter_operation = None
                        if state.pending_stop is not None:
                            raise CapabilityOperationError(
                                state.spec.id,
                                "reload",
                                RuntimeError("能力仍持有未释放资源，必须先重试 stop"),
                            )
                        if state.instance is None:
                            raise CapabilityOperationError(
                                state.spec.id,
                                "reload",
                                RuntimeError("只有 RUNNING 能力可以 reload"),
                            )
                        previous = state.instance
                        state.generation += 1
                        generation = state.generation
                        future: Future[Any] = Future()
                        state.inflight = future
                        state.inflight_operation = "reload"
                        state.instance = None
                        state.lifecycle = CapabilityLifecycleState.RELOADING
                        implementation = state.implementation
                        break
            result = self._wait_sync(waiter)
            if waiter_operation == "reload":
                return result
        started_at = time.monotonic()
        self._emit(
            state,
            generation=generation,
            operation="reload",
            outcome="started",
            reason=reason,
            started_at=started_at,
        )
        candidate = None
        previous_released = False
        try:
            self._sync_callback(adapter, "stop", state.spec, previous, generation)
            previous_released = True
            candidate = self._sync_callback(
                adapter,
                "create",
                state.spec,
                implementation,
                generation,
                previous,
            )
            if candidate is None:
                raise CapabilityAdapterContractError(
                    f"adapter 没有返回 {state.spec.id} 的 candidate"
                )
            self._sync_callback(adapter, "start", state.spec, candidate, generation)
            self._ensure_open()
            with state.lock:
                state.instance = candidate
                state.lifecycle = CapabilityLifecycleState.RUNNING
                state.last_error = None
            self._finish_transition(state, future, result=candidate)
            self._emit(
                state,
                generation=generation,
                operation="reload",
                outcome="succeeded",
                reason=reason,
                started_at=started_at,
            )
            return candidate
        except BaseException as error:
            cleanup_error = self._cleanup_failed_sync(
                state,
                adapter,
                candidate,
                generation,
                error,
            )
            final_error = cleanup_error or error
            with state.lock:
                state.instance = None
                if not previous_released:
                    state.pending_stop = previous
                state.lifecycle = (
                    CapabilityLifecycleState.STOPPED
                    if isinstance(error, CapabilityRuntimeClosedError)
                    else CapabilityLifecycleState.FAILED
                )
                state.last_error = final_error
            operation_error = self._wrap_error(state.spec.id, "reload", final_error)
            self._finish_transition(state, future, error=operation_error)
            self._emit(
                state,
                generation=generation,
                operation="reload",
                outcome="failed",
                reason=reason,
                started_at=started_at,
                error=final_error,
            )
            raise operation_error from error

    async def reload_async(self, capability_id: str, *, reason: str) -> Any:
        """撤销当前实例后，通过异步 adapter 完成新 generation 的资源切换。"""
        state = self._state(capability_id)
        adapter = self._adapter(state, AdapterExecutionMode.ASYNC)
        while True:
            with self._shutdown_lock:
                self._ensure_open()
                with state.lock:
                    if state.inflight is not None:
                        waiter = state.inflight
                        waiter_operation = state.inflight_operation
                    else:
                        waiter = None
                        waiter_operation = None
                        if state.pending_stop is not None:
                            raise CapabilityOperationError(
                                state.spec.id,
                                "reload",
                                RuntimeError("能力仍持有未释放资源，必须先重试 stop"),
                            )
                        if state.instance is None:
                            raise CapabilityOperationError(
                                state.spec.id,
                                "reload",
                                RuntimeError("只有 RUNNING 能力可以 reload"),
                            )
                        previous = state.instance
                        state.generation += 1
                        generation = state.generation
                        future: Future[Any] = Future()
                        state.inflight = future
                        state.inflight_operation = "reload"
                        state.instance = None
                        state.lifecycle = CapabilityLifecycleState.RELOADING
                        implementation = state.implementation
                        break
            result = await self._wait_async(waiter)
            if waiter_operation == "reload":
                return result
        started_at = time.monotonic()
        self._emit(
            state,
            generation=generation,
            operation="reload",
            outcome="started",
            reason=reason,
            started_at=started_at,
        )
        candidate = None
        previous_released = False
        try:
            await self._async_callback(adapter, "stop", state.spec, previous, generation)
            previous_released = True
            candidate = await self._async_callback(
                adapter,
                "create",
                state.spec,
                implementation,
                generation,
                previous,
            )
            if candidate is None:
                raise CapabilityAdapterContractError(
                    f"adapter 没有返回 {state.spec.id} 的 candidate"
                )
            await self._async_callback(adapter, "start", state.spec, candidate, generation)
            self._ensure_open()
            with state.lock:
                state.instance = candidate
                state.lifecycle = CapabilityLifecycleState.RUNNING
                state.last_error = None
            self._finish_transition(state, future, result=candidate)
            self._emit(
                state,
                generation=generation,
                operation="reload",
                outcome="succeeded",
                reason=reason,
                started_at=started_at,
            )
            return candidate
        except BaseException as error:
            cleanup_error = await self._cleanup_failed_async(
                state,
                adapter,
                candidate,
                generation,
                error,
            )
            final_error = cleanup_error or error
            with state.lock:
                state.instance = None
                if not previous_released:
                    state.pending_stop = previous
                state.lifecycle = (
                    CapabilityLifecycleState.STOPPED
                    if isinstance(error, CapabilityRuntimeClosedError)
                    else CapabilityLifecycleState.FAILED
                )
                state.last_error = final_error
            operation_error = self._wrap_error(state.spec.id, "reload", final_error)
            self._finish_transition(state, future, error=operation_error)
            self._emit(
                state,
                generation=generation,
                operation="reload",
                outcome="failed",
                reason=reason,
                started_at=started_at,
                error=final_error,
            )
            raise operation_error from error

    def stop(self, capability_id: str, *, reason: str) -> None:
        """同步撤销并停止能力资源；物化实现保留供后续显式重启。"""
        self._stop_sync(capability_id, reason=reason, shutdown=False)

    def _stop_sync(self, capability_id: str, *, reason: str, shutdown: bool) -> None:
        state = self._state(capability_id)
        adapter = self._adapter(state, AdapterExecutionMode.SYNC)
        while True:
            with state.lock:
                if state.inflight is not None:
                    waiter = state.inflight
                    waiter_operation = state.inflight_operation
                    owner = None
                else:
                    stop_owner = (
                        state.instance
                        if state.instance is not None
                        else state.pending_stop
                    )
                    pending = state.pending_cleanup
                    pending_error = state.pending_cleanup_error
                    if stop_owner is None and pending is None:
                        if state.lifecycle is not CapabilityLifecycleState.FAILED:
                            state.lifecycle = CapabilityLifecycleState.STOPPED
                        return
                    state.generation += 1
                    generation = state.generation
                    future: Future[Any] = Future()
                    state.inflight = future
                    state.inflight_operation = "stop"
                    state.instance = None
                    state.pending_stop = None
                    state.pending_cleanup = None
                    state.pending_cleanup_error = None
                    state.lifecycle = CapabilityLifecycleState.STOPPING
                    waiter = None
                    waiter_operation = None
                    owner = (generation, future, stop_owner, pending, pending_error)
            if waiter is not None:
                try:
                    self._wait_sync(waiter)
                except BaseException:
                    if not shutdown:
                        raise
                if waiter_operation == "stop":
                    return
                continue
            break

        generation, future, stop_owner, pending, pending_error = owner
        started_at = time.monotonic()
        self._emit(
            state,
            generation=generation,
            operation="stop",
            outcome="started",
            reason=reason,
            started_at=started_at,
        )
        try:
            if stop_owner is not None:
                self._sync_callback(adapter, "stop", state.spec, stop_owner, generation)
                stop_owner = None
            if pending is not None:
                self._sync_callback(
                    adapter,
                    "cleanup",
                    state.spec,
                    pending,
                    generation,
                    pending_error or RuntimeError("pending cleanup"),
                )
                pending = None
            with state.lock:
                state.lifecycle = CapabilityLifecycleState.STOPPED
                state.last_error = None
            self._finish_transition(state, future, result=None)
            self._emit(
                state,
                generation=generation,
                operation="stop",
                outcome="succeeded",
                reason=reason,
                started_at=started_at,
            )
        except BaseException as error:
            with state.lock:
                state.lifecycle = CapabilityLifecycleState.FAILED
                state.last_error = error
                if stop_owner is not None:
                    state.pending_stop = stop_owner
                if pending is not None:
                    state.pending_cleanup = pending
                    state.pending_cleanup_error = error
            operation_error = self._wrap_error(state.spec.id, "stop", error)
            self._finish_transition(state, future, error=operation_error)
            self._emit(
                state,
                generation=generation,
                operation="stop",
                outcome="failed",
                reason=reason,
                started_at=started_at,
                error=error,
            )
            if not shutdown:
                raise operation_error from error

    async def stop_async(self, capability_id: str, *, reason: str) -> None:
        """异步撤销并停止能力资源。"""
        await self._stop_async(capability_id, reason=reason, shutdown=False)

    async def _stop_async(self, capability_id: str, *, reason: str, shutdown: bool) -> None:
        state = self._state(capability_id)
        adapter = self._adapter(state, AdapterExecutionMode.ASYNC)
        while True:
            with state.lock:
                if state.inflight is not None:
                    waiter = state.inflight
                    waiter_operation = state.inflight_operation
                    owner = None
                else:
                    stop_owner = (
                        state.instance
                        if state.instance is not None
                        else state.pending_stop
                    )
                    pending = state.pending_cleanup
                    pending_error = state.pending_cleanup_error
                    if stop_owner is None and pending is None:
                        if state.lifecycle is not CapabilityLifecycleState.FAILED:
                            state.lifecycle = CapabilityLifecycleState.STOPPED
                        return
                    state.generation += 1
                    generation = state.generation
                    future: Future[Any] = Future()
                    state.inflight = future
                    state.inflight_operation = "stop"
                    state.instance = None
                    state.pending_stop = None
                    state.pending_cleanup = None
                    state.pending_cleanup_error = None
                    state.lifecycle = CapabilityLifecycleState.STOPPING
                    waiter = None
                    waiter_operation = None
                    owner = (generation, future, stop_owner, pending, pending_error)
            if waiter is not None:
                try:
                    await self._wait_async(waiter)
                except BaseException:
                    if not shutdown:
                        raise
                if waiter_operation == "stop":
                    return
                continue
            break

        generation, future, stop_owner, pending, pending_error = owner
        started_at = time.monotonic()
        self._emit(
            state,
            generation=generation,
            operation="stop",
            outcome="started",
            reason=reason,
            started_at=started_at,
        )
        try:
            if stop_owner is not None:
                await self._async_callback(
                    adapter,
                    "stop",
                    state.spec,
                    stop_owner,
                    generation,
                )
                stop_owner = None
            if pending is not None:
                await self._async_callback(
                    adapter,
                    "cleanup",
                    state.spec,
                    pending,
                    generation,
                    pending_error or RuntimeError("pending cleanup"),
                )
                pending = None
            with state.lock:
                state.lifecycle = CapabilityLifecycleState.STOPPED
                state.last_error = None
            self._finish_transition(state, future, result=None)
            self._emit(
                state,
                generation=generation,
                operation="stop",
                outcome="succeeded",
                reason=reason,
                started_at=started_at,
            )
        except BaseException as error:
            with state.lock:
                state.lifecycle = CapabilityLifecycleState.FAILED
                state.last_error = error
                if stop_owner is not None:
                    state.pending_stop = stop_owner
                if pending is not None:
                    state.pending_cleanup = pending
                    state.pending_cleanup_error = error
            operation_error = self._wrap_error(state.spec.id, "stop", error)
            self._finish_transition(state, future, error=operation_error)
            self._emit(
                state,
                generation=generation,
                operation="stop",
                outcome="failed",
                reason=reason,
                started_at=started_at,
                error=error,
            )
            if not shutdown:
                raise operation_error from error

    def shutdown(self, *, reason: str) -> None:
        """不可逆关闭仅含同步 adapter 的 Runtime，并阻止并发首启重新发布。"""
        async_kinds = {
            kind
            for kind, adapter in self._adapters.items()
            if getattr(adapter, "execution_mode", None) is AdapterExecutionMode.ASYNC
        }
        if async_kinds:
            raise CapabilityAdapterModeError(
                f"同步 shutdown 不能处理异步 adapter：{sorted(async_kinds)}"
            )
        with self._shutdown_lock:
            self._shutdown = True
        for spec in self._registry.list_specs():
            self._stop_sync(spec.id, reason=reason, shutdown=True)

    async def shutdown_async(self, *, reason: str) -> None:
        """不可逆关闭混合同步/异步 adapter 的 Runtime。"""
        with self._shutdown_lock:
            self._shutdown = True
        for spec in self._registry.list_specs():
            adapter = self._adapters[spec.kind]
            if getattr(adapter, "execution_mode", None) is AdapterExecutionMode.ASYNC:
                await self._stop_async(spec.id, reason=reason, shutdown=True)
            else:
                await asyncio.to_thread(
                    self._stop_sync,
                    spec.id,
                    reason=reason,
                    shutdown=True,
                )
