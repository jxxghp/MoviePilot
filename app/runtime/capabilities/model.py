from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol


class ActivationPolicy(str, Enum):
    """能力的启动触发策略。"""

    BOOTSTRAP = "bootstrap"
    WHEN_CONFIGURED = "when_configured"
    ON_FIRST_USE = "on_first_use"


class AdapterExecutionMode(str, Enum):
    """领域适配器执行回调所使用的并发模型。"""

    SYNC = "sync"
    ASYNC = "async"


class CapabilityMaterializationState(str, Enum):
    """Python 实现对象的解析状态。"""

    UNRESOLVED = "unresolved"
    RESOLVING = "resolving"
    RESOLVED = "resolved"
    FAILED = "failed"


class CapabilityLifecycleState(str, Enum):
    """能力所拥有外部资源的生命周期状态。"""

    DISCOVERED = "discovered"
    STARTING = "starting"
    RUNNING = "running"
    RELOADING = "reloading"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SelectorSchema:
    """声明一个 selector 可接受的精确参数集合。"""

    required_fields: frozenset[str] = frozenset()
    optional_fields: frozenset[str] = frozenset()
    validator: Optional[Callable[[Mapping[str, Any]], None]] = None


@dataclass(frozen=True, slots=True)
class SelectorSpec:
    """由领域适配器解释的配置选择器。"""

    kind: str
    config: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    """从 data-only manifest 构建的不可变能力声明。"""

    schema_version: int
    id: str
    kind: str
    entrypoint: str
    activation: ActivationPolicy
    metadata: Mapping[str, Any]
    selector: Optional[SelectorSpec]
    watch: tuple[str, ...]
    depends_on: tuple[str, ...]
    source: Path


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    """能力状态的只读快照。"""

    capability_id: str
    materialization: CapabilityMaterializationState
    lifecycle: CapabilityLifecycleState
    generation: int
    visible: bool
    error: Optional[str]


@dataclass(frozen=True, slots=True)
class CapabilityObservation:
    """单次状态转换的可复现观测记录。"""

    capability_id: str
    generation: int
    operation: str
    outcome: str
    reason: str
    materialization: CapabilityMaterializationState
    lifecycle: CapabilityLifecycleState
    duration_ms: float
    error: Optional[str]


class SyncCapabilityAdapter(Protocol):
    """同步领域适配器合同。"""

    execution_mode: AdapterExecutionMode

    def materialize(self, spec: CapabilitySpec) -> Any:
        """解析 manifest entrypoint 对应的 canonical 实现对象。"""

    def create(
        self,
        spec: CapabilitySpec,
        implementation: Any,
        generation: int,
        previous: Any = None,
    ) -> Any:
        """创建尚未对外发布的候选资源。"""

    def start(self, spec: CapabilitySpec, candidate: Any, generation: int) -> None:
        """启动候选资源；返回前候选不会对普通查询可见。"""

    def stop(self, spec: CapabilitySpec, instance: Any, generation: int) -> None:
        """停止已经撤销运行态可见性的资源。"""

    def cleanup(
        self,
        spec: CapabilitySpec,
        candidate: Any,
        generation: int,
        error: BaseException,
    ) -> None:
        """清理由失败启动留下的候选资源。"""


class AsyncCapabilityAdapter(Protocol):
    """异步领域适配器合同。"""

    execution_mode: AdapterExecutionMode

    def materialize(self, spec: CapabilitySpec) -> Awaitable[Any]:
        """异步解析 manifest entrypoint 对应的 canonical 实现对象。"""

    def create(
        self,
        spec: CapabilitySpec,
        implementation: Any,
        generation: int,
        previous: Any = None,
    ) -> Awaitable[Any]:
        """异步创建尚未对外发布的候选资源。"""

    def start(
        self,
        spec: CapabilitySpec,
        candidate: Any,
        generation: int,
    ) -> Awaitable[None]:
        """异步启动候选资源。"""

    def stop(
        self,
        spec: CapabilitySpec,
        instance: Any,
        generation: int,
    ) -> Awaitable[None]:
        """异步停止已经撤销运行态可见性的资源。"""

    def cleanup(
        self,
        spec: CapabilitySpec,
        candidate: Any,
        generation: int,
        error: BaseException,
    ) -> Awaitable[None]:
        """异步清理由失败启动留下的候选资源。"""


EMPTY_MAPPING: Mapping[str, Any] = MappingProxyType({})
