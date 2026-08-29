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
from typing import Any, Callable, Mapping, Optional, Union

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


@dataclass(frozen=True, slots=True)
class _PublishedTransition:
    """表示状态机已经拥有可直接返回的发布结果。"""

    result: Any


@dataclass(frozen=True, slots=True)
class _SettledTransition:
    """表示停止状态无需 I/O 即已真实收敛。"""


@dataclass(frozen=True, slots=True)
class _TransitionWait:
    """表示调用方必须等待另一位 owner 完成当前状态转换。"""

    future: Future[Any]
    operation: Optional[str]


@dataclass(frozen=True, slots=True)
class _MaterializationClaim:
    """记录一次物化状态转换的唯一 owner。"""

    generation: int
    future: Future[Any]


@dataclass(frozen=True, slots=True)
class _ActivationClaim:
    """记录启动状态转换以及需要先收敛的遗留资源。"""

    generation: int
    future: Future[Any]
    implementation: Any
    pending_stop: Any
    pending_cleanup: Any
    pending_cleanup_error: Optional[BaseException]


@dataclass(frozen=True, slots=True)
class _ReloadClaim:
    """记录重载状态转换及已撤销可见性的旧实例。"""

    generation: int
    future: Future[Any]
    implementation: Any
    previous: Any


@dataclass(frozen=True, slots=True)
class _StopClaim:
    """记录停止状态转换及 Runtime 仍持有的全部资源 owner。"""

    generation: int
    future: Future[Any]
    stop_owner: Any
    pending_cleanup: Any
    pending_cleanup_error: Optional[BaseException]


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

    @staticmethod
    def _claim_materialization(
        state: _CapabilityState,
        *,
        retry: bool,
    ) -> Union[_PublishedTransition, _TransitionWait, _MaterializationClaim]:
        """声明一次物化转换，且不执行任何 adapter I/O。"""
        with state.lock:
            if state.materialization is CapabilityMaterializationState.RESOLVED:
                return _PublishedTransition(state.implementation)
            if state.materialization is CapabilityMaterializationState.FAILED and not retry:
                raise CapabilityOperationError(
                    state.spec.id,
                    "materialize",
                    RuntimeError("能力处于 FAILED，必须显式 retry=True"),
                )
            if state.inflight is not None:
                return _TransitionWait(state.inflight, state.inflight_operation)
            state.generation += 1
            future: Future[Any] = Future()
            state.inflight = future
            state.inflight_operation = "materialize"
            state.materialization = CapabilityMaterializationState.RESOLVING
            return _MaterializationClaim(state.generation, future)

    @staticmethod
    def _claim_activation(
        state: _CapabilityState,
        *,
        operation: str,
        retry: bool,
    ) -> Union[_PublishedTransition, _TransitionWait, _ActivationClaim]:
        """声明一次启动转换并原子撤销遗留 owner 的共享状态。"""
        with state.lock:
            if operation == "activate" and state.instance is not None:
                return _PublishedTransition(state.instance)
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
                return _TransitionWait(state.inflight, state.inflight_operation)
            state.generation += 1
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
            claim = _ActivationClaim(
                generation=state.generation,
                future=future,
                implementation=state.implementation,
                pending_stop=pending_stop,
                pending_cleanup=state.pending_cleanup,
                pending_cleanup_error=state.pending_cleanup_error,
            )
            state.pending_stop = None
            state.pending_cleanup = None
            state.pending_cleanup_error = None
            return claim

    @staticmethod
    def _claim_reload(
        state: _CapabilityState,
    ) -> Union[_TransitionWait, _ReloadClaim]:
        """声明一次重载转换并先撤销旧实例的外部可见性。"""
        with state.lock:
            if state.inflight is not None:
                return _TransitionWait(state.inflight, state.inflight_operation)
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
            state.generation += 1
            future: Future[Any] = Future()
            state.inflight = future
            state.inflight_operation = "reload"
            claim = _ReloadClaim(
                generation=state.generation,
                future=future,
                implementation=state.implementation,
                previous=state.instance,
            )
            state.instance = None
            state.lifecycle = CapabilityLifecycleState.RELOADING
            return claim

    @staticmethod
    def _claim_stop(
        state: _CapabilityState,
    ) -> Union[_SettledTransition, _TransitionWait, _StopClaim]:
        """声明一次停止转换并原子接管尚未释放的资源。"""
        with state.lock:
            if state.inflight is not None:
                return _TransitionWait(state.inflight, state.inflight_operation)
            stop_owner = (
                state.instance
                if state.instance is not None
                else state.pending_stop
            )
            pending_cleanup = state.pending_cleanup
            if stop_owner is None and pending_cleanup is None:
                if state.lifecycle is not CapabilityLifecycleState.FAILED:
                    state.lifecycle = CapabilityLifecycleState.STOPPED
                return _SettledTransition()
            state.generation += 1
            future: Future[Any] = Future()
            state.inflight = future
            state.inflight_operation = "stop"
            claim = _StopClaim(
                generation=state.generation,
                future=future,
                stop_owner=stop_owner,
                pending_cleanup=pending_cleanup,
                pending_cleanup_error=state.pending_cleanup_error,
            )
            state.instance = None
            state.pending_stop = None
            state.pending_cleanup = None
            state.pending_cleanup_error = None
            state.lifecycle = CapabilityLifecycleState.STOPPING
            return claim

    @classmethod
    def _publish_materialization(
        cls,
        state: _CapabilityState,
        claim: _MaterializationClaim,
        implementation: Any,
    ) -> None:
        """提交物化成功状态并唤醒全部等待者。"""
        with state.lock:
            state.implementation = implementation
            state.materialization = CapabilityMaterializationState.RESOLVED
            state.last_error = None
        cls._finish_transition(state, claim.future, result=implementation)

    @classmethod
    def _fail_materialization(
        cls,
        state: _CapabilityState,
        claim: _MaterializationClaim,
        error: BaseException,
    ) -> BaseException:
        """提交物化失败状态并返回稳定的公开异常。"""
        with state.lock:
            state.materialization = CapabilityMaterializationState.FAILED
            state.last_error = error
        operation_error = cls._wrap_error(state.spec.id, "materialize", error)
        cls._finish_transition(state, claim.future, error=operation_error)
        return operation_error

    @classmethod
    def _publish_activation(
        cls,
        state: _CapabilityState,
        claim: _ActivationClaim,
        *,
        implementation: Any,
        candidate: Any,
    ) -> None:
        """仅在候选启动成功后提交实现与可见实例。"""
        with state.lock:
            state.implementation = implementation
            state.materialization = CapabilityMaterializationState.RESOLVED
            state.instance = candidate
            state.lifecycle = CapabilityLifecycleState.RUNNING
            state.last_error = None
        cls._finish_transition(state, claim.future, result=candidate)

    @classmethod
    def _fail_activation(
        cls,
        state: _CapabilityState,
        claim: _ActivationClaim,
        *,
        operation: str,
        implementation: Any,
        pending_stop: Any,
        error: BaseException,
        lifecycle_error: BaseException,
    ) -> BaseException:
        """提交启动失败状态，并保留尚未释放的旧 owner。"""
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
            state.lifecycle = cls._failure_lifecycle(lifecycle_error)
            state.last_error = error
        operation_error = cls._wrap_error(state.spec.id, operation, error)
        cls._finish_transition(state, claim.future, error=operation_error)
        return operation_error

    @classmethod
    def _publish_reload(
        cls,
        state: _CapabilityState,
        claim: _ReloadClaim,
        candidate: Any,
    ) -> None:
        """提交重载后的新实例并恢复对外可见性。"""
        with state.lock:
            state.instance = candidate
            state.lifecycle = CapabilityLifecycleState.RUNNING
            state.last_error = None
        cls._finish_transition(state, claim.future, result=candidate)

    @classmethod
    def _fail_reload(
        cls,
        state: _CapabilityState,
        claim: _ReloadClaim,
        *,
        previous_released: bool,
        error: BaseException,
        lifecycle_error: BaseException,
    ) -> BaseException:
        """提交重载失败状态，必要时保留未释放的旧实例。"""
        with state.lock:
            state.instance = None
            if not previous_released:
                state.pending_stop = claim.previous
            state.lifecycle = cls._failure_lifecycle(lifecycle_error)
            state.last_error = error
        operation_error = cls._wrap_error(state.spec.id, "reload", error)
        cls._finish_transition(state, claim.future, error=operation_error)
        return operation_error

    @classmethod
    def _publish_stop(
        cls,
        state: _CapabilityState,
        claim: _StopClaim,
    ) -> None:
        """提交资源真实收敛后的停止状态。"""
        with state.lock:
            state.lifecycle = CapabilityLifecycleState.STOPPED
            state.last_error = None
        cls._finish_transition(state, claim.future, result=None)

    @classmethod
    def _fail_stop(
        cls,
        state: _CapabilityState,
        claim: _StopClaim,
        *,
        stop_owner: Any,
        pending_cleanup: Any,
        error: BaseException,
    ) -> BaseException:
        """提交停止失败状态并归还仍未释放的资源 owner。"""
        with state.lock:
            state.lifecycle = CapabilityLifecycleState.FAILED
            state.last_error = error
            if stop_owner is not None:
                state.pending_stop = stop_owner
            if pending_cleanup is not None:
                state.pending_cleanup = pending_cleanup
                state.pending_cleanup_error = error
        operation_error = cls._wrap_error(state.spec.id, "stop", error)
        cls._finish_transition(state, claim.future, error=operation_error)
        return operation_error

    @staticmethod
    def _publish_pending_stop_released(state: _CapabilityState) -> None:
        """在遗留停止成功后把启动转换推进到 STARTING。"""
        with state.lock:
            state.lifecycle = CapabilityLifecycleState.STARTING

    @staticmethod
    def _restore_pending_cleanup(
        state: _CapabilityState,
        candidate: Any,
        error: Optional[BaseException],
    ) -> None:
        """清理重试失败时归还 Runtime 对候选资源的所有权。"""
        with state.lock:
            state.pending_cleanup = candidate
            state.pending_cleanup_error = error

    @staticmethod
    def _failure_lifecycle(error: BaseException) -> CapabilityLifecycleState:
        """把关闭竞态与普通失败映射到统一生命周期结果。"""
        if isinstance(error, CapabilityRuntimeClosedError):
            return CapabilityLifecycleState.STOPPED
        return CapabilityLifecycleState.FAILED

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
                decision = self._claim_materialization(state, retry=retry)
            if isinstance(decision, _PublishedTransition):
                return decision.result
            if isinstance(decision, _TransitionWait):
                result = self._wait_sync(decision.future)
                if decision.operation == "materialize":
                    return result
                continue
            claim = decision
            break

        started_at = time.monotonic()
        self._emit(
            state,
            generation=claim.generation,
            operation="materialize",
            outcome="started",
            reason=reason,
            started_at=started_at,
        )
        try:
            implementation = self._sync_callback(adapter, "materialize", state.spec)
            implementation = self._canonical_implementation(state.spec, implementation)
            self._ensure_open()
            self._publish_materialization(state, claim, implementation)
            self._emit(
                state,
                generation=claim.generation,
                operation="materialize",
                outcome="succeeded",
                reason=reason,
                started_at=started_at,
            )
            return implementation
        except BaseException as error:
            operation_error = self._fail_materialization(state, claim, error)
            self._emit(
                state,
                generation=claim.generation,
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
                decision = self._claim_materialization(state, retry=retry)
            if isinstance(decision, _PublishedTransition):
                return decision.result
            if isinstance(decision, _TransitionWait):
                result = await self._wait_async(decision.future)
                if decision.operation == "materialize":
                    return result
                continue
            claim = decision
            break

        started_at = time.monotonic()
        self._emit(
            state,
            generation=claim.generation,
            operation="materialize",
            outcome="started",
            reason=reason,
            started_at=started_at,
        )
        try:
            implementation = await self._async_callback(adapter, "materialize", state.spec)
            implementation = self._canonical_implementation(state.spec, implementation)
            self._ensure_open()
            self._publish_materialization(state, claim, implementation)
            self._emit(
                state,
                generation=claim.generation,
                operation="materialize",
                outcome="succeeded",
                reason=reason,
                started_at=started_at,
            )
            return implementation
        except BaseException as error:
            operation_error = self._fail_materialization(state, claim, error)
            self._emit(
                state,
                generation=claim.generation,
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
                decision = self._claim_activation(
                    state,
                    operation=operation,
                    retry=retry,
                )
            if isinstance(decision, _PublishedTransition):
                return decision.result
            if isinstance(decision, _TransitionWait):
                result = self._wait_sync(decision.future)
                if decision.operation == operation:
                    return result
                continue
            claim = decision
            break

        implementation = claim.implementation
        pending_stop = claim.pending_stop
        started_at = time.monotonic()
        self._emit(
            state,
            generation=claim.generation,
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
                    claim.generation,
                )
                pending_stop = None
                self._publish_pending_stop_released(state)
            if claim.pending_cleanup is not None:
                try:
                    self._sync_callback(
                        adapter,
                        "cleanup",
                        state.spec,
                        claim.pending_cleanup,
                        claim.generation,
                        claim.pending_cleanup_error or RuntimeError("pending cleanup"),
                    )
                except BaseException:
                    self._restore_pending_cleanup(
                        state,
                        claim.pending_cleanup,
                        claim.pending_cleanup_error,
                    )
                    raise
            if implementation is None:
                materialized = self._sync_callback(adapter, "materialize", state.spec)
                implementation = self._canonical_implementation(state.spec, materialized)
            candidate = self._sync_callback(
                adapter,
                "create",
                state.spec,
                implementation,
                claim.generation,
                previous,
            )
            if candidate is None:
                raise CapabilityAdapterContractError(
                    f"adapter 没有返回 {state.spec.id} 的 candidate"
                )
            self._sync_callback(
                adapter,
                "start",
                state.spec,
                candidate,
                claim.generation,
            )
            self._ensure_open()
            self._publish_activation(
                state,
                claim,
                implementation=implementation,
                candidate=candidate,
            )
            self._emit(
                state,
                generation=claim.generation,
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
                claim.generation,
                error,
            )
            final_error = cleanup_error or error
            operation_error = self._fail_activation(
                state,
                claim,
                operation=operation,
                implementation=implementation,
                pending_stop=pending_stop,
                error=final_error,
                lifecycle_error=error,
            )
            self._emit(
                state,
                generation=claim.generation,
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
                decision = self._claim_activation(
                    state,
                    operation=operation,
                    retry=retry,
                )
            if isinstance(decision, _PublishedTransition):
                return decision.result
            if isinstance(decision, _TransitionWait):
                result = await self._wait_async(decision.future)
                if decision.operation == operation:
                    return result
                continue
            claim = decision
            break

        implementation = claim.implementation
        pending_stop = claim.pending_stop
        started_at = time.monotonic()
        self._emit(
            state,
            generation=claim.generation,
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
                    claim.generation,
                )
                pending_stop = None
                self._publish_pending_stop_released(state)
            if claim.pending_cleanup is not None:
                try:
                    await self._async_callback(
                        adapter,
                        "cleanup",
                        state.spec,
                        claim.pending_cleanup,
                        claim.generation,
                        claim.pending_cleanup_error or RuntimeError("pending cleanup"),
                    )
                except BaseException:
                    self._restore_pending_cleanup(
                        state,
                        claim.pending_cleanup,
                        claim.pending_cleanup_error,
                    )
                    raise
            if implementation is None:
                materialized = await self._async_callback(adapter, "materialize", state.spec)
                implementation = self._canonical_implementation(state.spec, materialized)
            candidate = await self._async_callback(
                adapter,
                "create",
                state.spec,
                implementation,
                claim.generation,
                previous,
            )
            if candidate is None:
                raise CapabilityAdapterContractError(
                    f"adapter 没有返回 {state.spec.id} 的 candidate"
                )
            await self._async_callback(
                adapter,
                "start",
                state.spec,
                candidate,
                claim.generation,
            )
            self._ensure_open()
            self._publish_activation(
                state,
                claim,
                implementation=implementation,
                candidate=candidate,
            )
            self._emit(
                state,
                generation=claim.generation,
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
                claim.generation,
                error,
            )
            final_error = cleanup_error or error
            operation_error = self._fail_activation(
                state,
                claim,
                operation=operation,
                implementation=implementation,
                pending_stop=pending_stop,
                error=final_error,
                lifecycle_error=error,
            )
            self._emit(
                state,
                generation=claim.generation,
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
            self._restore_pending_cleanup(state, candidate, cleanup_error)
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
            self._restore_pending_cleanup(state, candidate, cleanup_error)
            return RuntimeError(f"{error}; cleanup failed: {cleanup_error}")

    def reload(self, capability_id: str, *, reason: str) -> Any:
        """撤销当前实例后，通过同步 adapter 完成新 generation 的资源切换。"""
        state = self._state(capability_id)
        adapter = self._adapter(state, AdapterExecutionMode.SYNC)
        while True:
            with self._shutdown_lock:
                self._ensure_open()
                decision = self._claim_reload(state)
            if isinstance(decision, _TransitionWait):
                result = self._wait_sync(decision.future)
                if decision.operation == "reload":
                    return result
                continue
            claim = decision
            break
        started_at = time.monotonic()
        self._emit(
            state,
            generation=claim.generation,
            operation="reload",
            outcome="started",
            reason=reason,
            started_at=started_at,
        )
        candidate = None
        previous_released = False
        try:
            self._sync_callback(
                adapter,
                "stop",
                state.spec,
                claim.previous,
                claim.generation,
            )
            previous_released = True
            candidate = self._sync_callback(
                adapter,
                "create",
                state.spec,
                claim.implementation,
                claim.generation,
                claim.previous,
            )
            if candidate is None:
                raise CapabilityAdapterContractError(
                    f"adapter 没有返回 {state.spec.id} 的 candidate"
                )
            self._sync_callback(
                adapter,
                "start",
                state.spec,
                candidate,
                claim.generation,
            )
            self._ensure_open()
            self._publish_reload(state, claim, candidate)
            self._emit(
                state,
                generation=claim.generation,
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
                claim.generation,
                error,
            )
            final_error = cleanup_error or error
            operation_error = self._fail_reload(
                state,
                claim,
                previous_released=previous_released,
                error=final_error,
                lifecycle_error=error,
            )
            self._emit(
                state,
                generation=claim.generation,
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
                decision = self._claim_reload(state)
            if isinstance(decision, _TransitionWait):
                result = await self._wait_async(decision.future)
                if decision.operation == "reload":
                    return result
                continue
            claim = decision
            break
        started_at = time.monotonic()
        self._emit(
            state,
            generation=claim.generation,
            operation="reload",
            outcome="started",
            reason=reason,
            started_at=started_at,
        )
        candidate = None
        previous_released = False
        try:
            await self._async_callback(
                adapter,
                "stop",
                state.spec,
                claim.previous,
                claim.generation,
            )
            previous_released = True
            candidate = await self._async_callback(
                adapter,
                "create",
                state.spec,
                claim.implementation,
                claim.generation,
                claim.previous,
            )
            if candidate is None:
                raise CapabilityAdapterContractError(
                    f"adapter 没有返回 {state.spec.id} 的 candidate"
                )
            await self._async_callback(
                adapter,
                "start",
                state.spec,
                candidate,
                claim.generation,
            )
            self._ensure_open()
            self._publish_reload(state, claim, candidate)
            self._emit(
                state,
                generation=claim.generation,
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
                claim.generation,
                error,
            )
            final_error = cleanup_error or error
            operation_error = self._fail_reload(
                state,
                claim,
                previous_released=previous_released,
                error=final_error,
                lifecycle_error=error,
            )
            self._emit(
                state,
                generation=claim.generation,
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

    def _stop_sync(self, capability_id: str, *, reason: str, shutdown: bool) -> bool:
        """停止同步能力并返回资源 owner 是否已经真实收敛。"""
        state = self._state(capability_id)
        adapter = self._adapter(state, AdapterExecutionMode.SYNC)
        while True:
            decision = self._claim_stop(state)
            if isinstance(decision, _SettledTransition):
                return True
            if isinstance(decision, _TransitionWait):
                try:
                    self._wait_sync(decision.future)
                except BaseException:
                    if not shutdown:
                        raise
                    if decision.operation == "stop":
                        return False
                if decision.operation == "stop":
                    return True
                continue
            claim = decision
            break

        stop_owner = claim.stop_owner
        pending_cleanup = claim.pending_cleanup
        started_at = time.monotonic()
        self._emit(
            state,
            generation=claim.generation,
            operation="stop",
            outcome="started",
            reason=reason,
            started_at=started_at,
        )
        try:
            if stop_owner is not None:
                self._sync_callback(
                    adapter,
                    "stop",
                    state.spec,
                    stop_owner,
                    claim.generation,
                )
                stop_owner = None
            if pending_cleanup is not None:
                self._sync_callback(
                    adapter,
                    "cleanup",
                    state.spec,
                    pending_cleanup,
                    claim.generation,
                    claim.pending_cleanup_error or RuntimeError("pending cleanup"),
                )
                pending_cleanup = None
            self._publish_stop(state, claim)
            self._emit(
                state,
                generation=claim.generation,
                operation="stop",
                outcome="succeeded",
                reason=reason,
                started_at=started_at,
            )
            return True
        except BaseException as error:
            operation_error = self._fail_stop(
                state,
                claim,
                stop_owner=stop_owner,
                pending_cleanup=pending_cleanup,
                error=error,
            )
            self._emit(
                state,
                generation=claim.generation,
                operation="stop",
                outcome="failed",
                reason=reason,
                started_at=started_at,
                error=error,
            )
            if not shutdown:
                raise operation_error from error
            return False

    async def stop_async(self, capability_id: str, *, reason: str) -> None:
        """异步撤销并停止能力资源。"""
        await self._stop_async(capability_id, reason=reason, shutdown=False)

    async def _stop_async(
        self,
        capability_id: str,
        *,
        reason: str,
        shutdown: bool,
    ) -> bool:
        """停止异步能力并返回资源 owner 是否已经真实收敛。"""
        state = self._state(capability_id)
        adapter = self._adapter(state, AdapterExecutionMode.ASYNC)
        while True:
            decision = self._claim_stop(state)
            if isinstance(decision, _SettledTransition):
                return True
            if isinstance(decision, _TransitionWait):
                try:
                    await self._wait_async(decision.future)
                except BaseException:
                    if not shutdown:
                        raise
                    if decision.operation == "stop":
                        return False
                if decision.operation == "stop":
                    return True
                continue
            claim = decision
            break

        stop_owner = claim.stop_owner
        pending_cleanup = claim.pending_cleanup
        started_at = time.monotonic()
        self._emit(
            state,
            generation=claim.generation,
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
                    claim.generation,
                )
                stop_owner = None
            if pending_cleanup is not None:
                await self._async_callback(
                    adapter,
                    "cleanup",
                    state.spec,
                    pending_cleanup,
                    claim.generation,
                    claim.pending_cleanup_error or RuntimeError("pending cleanup"),
                )
                pending_cleanup = None
            self._publish_stop(state, claim)
            self._emit(
                state,
                generation=claim.generation,
                operation="stop",
                outcome="succeeded",
                reason=reason,
                started_at=started_at,
            )
            return True
        except BaseException as error:
            operation_error = self._fail_stop(
                state,
                claim,
                stop_owner=stop_owner,
                pending_cleanup=pending_cleanup,
                error=error,
            )
            self._emit(
                state,
                generation=claim.generation,
                operation="stop",
                outcome="failed",
                reason=reason,
                started_at=started_at,
                error=error,
            )
            if not shutdown:
                raise operation_error from error
            return False

    def shutdown(self, *, reason: str) -> bool:
        """不可逆关闭同步 Runtime，并返回全部 owner 是否收敛。"""
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
        converged = True
        for spec in self._registry.list_specs():
            if not self._stop_sync(spec.id, reason=reason, shutdown=True):
                converged = False
        return converged

    async def shutdown_async(self, *, reason: str) -> bool:
        """不可逆关闭混合 Runtime，并返回全部 owner 是否收敛。"""
        with self._shutdown_lock:
            self._shutdown = True
        converged = True
        for spec in self._registry.list_specs():
            adapter = self._adapters[spec.kind]
            if getattr(adapter, "execution_mode", None) is AdapterExecutionMode.ASYNC:
                stopped = await self._stop_async(
                    spec.id,
                    reason=reason,
                    shutdown=True,
                )
            else:
                stopped = await asyncio.to_thread(
                    self._stop_sync,
                    spec.id,
                    reason=reason,
                    shutdown=True,
                )
            if not stopped:
                converged = False
        return converged
