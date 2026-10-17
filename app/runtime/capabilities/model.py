"""
能力声明、生命周期状态、观测快照和领域适配器契约。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol


class ActivationPolicy(str, Enum):
    """
    能力的启动触发策略。
    """

    # 在启动阶段主动激活
    BOOTSTRAP = "bootstrap"
    # 配置选择器满足启用条件时激活
    WHEN_CONFIGURED = "when_configured"
    # 首次请求使用时延迟激活
    ON_FIRST_USE = "on_first_use"


class AdapterExecutionMode(str, Enum):
    """
    领域适配器执行回调所使用的并发模型。
    """

    # 通过同步回调执行生命周期操作
    SYNC = "sync"
    # 通过异步回调执行生命周期操作
    ASYNC = "async"


class CapabilityMaterializationState(str, Enum):
    """
    Python 实现对象的解析状态。
    """

    # 尚未解析能力的实现入口
    UNRESOLVED = "unresolved"
    # 正在解析实现入口
    RESOLVING = "resolving"
    # 实现入口已解析为可用对象
    RESOLVED = "resolved"
    # 实现解析失败，需要显式重试
    FAILED = "failed"


class CapabilityLifecycleState(str, Enum):
    """
    能力所拥有外部资源的生命周期状态。
    """

    # 能力已登记，尚未启动资源
    DISCOVERED = "discovered"
    # 正在创建和启动候选资源
    STARTING = "starting"
    # 资源已成功启动并对外可见
    RUNNING = "running"
    # 正在替换运行资源，旧实例已撤销可见性
    RELOADING = "reloading"
    # 正在停止资源或清理遗留候选
    STOPPING = "stopping"
    # 资源停止已经实际完成
    STOPPED = "stopped"
    # 生命周期操作失败，可能仍需清理遗留资源
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SelectorSchema:
    """
    声明一个 selector 可接受的精确参数集合。

    :param required_fields: 配置选择器必须提供的字段名集合
    :param optional_fields: 配置选择器可以额外提供的字段名集合
    :param validator: 对已通过字段集合检查的配置执行额外校验；为 None 时不执行
    """

    required_fields: frozenset[str] = frozenset()
    optional_fields: frozenset[str] = frozenset()
    validator: Optional[Callable[[Mapping[str, Any]], None]] = None


@dataclass(frozen=True, slots=True)
class SelectorSpec:
    """
    由领域适配器解释的配置选择器。

    :param kind: 由领域适配器识别的选择器类型
    :param config: 传给选择器的配置参数映射
    """

    kind: str
    config: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    """
    从 data-only manifest 构建的不可变能力声明。

    :param schema_version: 能力清单的结构版本
    :param id: 在能力目录中唯一的标识
    :param kind: 用于选择领域适配器的能力类型
    :param entrypoint: 实现对象的规范导入入口
    :param activation: 决定何时激活资源的触发策略
    :param metadata: 领域适配器解释的附加元数据
    :param selector: 启用条件选择器；为 None 时不附加选择条件
    :param watch: 触发重新协调的配置项名称集合
    :param depends_on: 激活前需要满足的能力依赖标识
    :param source: 提供此声明的清单文件路径
    """

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
    """
    能力状态的只读快照。

    :param capability_id: 被查询的能力标识
    :param materialization: 实现对象当前解析状态
    :param lifecycle: 外部资源当前生命周期状态
    :param generation: 生成快照时的状态转换代次
    :param visible: 是否存在可供普通查询使用的已发布实例
    :param error: 最近一次异常的文本；没有时为 None
    """

    capability_id: str
    materialization: CapabilityMaterializationState
    lifecycle: CapabilityLifecycleState
    generation: int
    visible: bool
    error: Optional[str]


@dataclass(frozen=True, slots=True)
class CapabilityObservation:
    """
    单次状态转换的可复现观测记录。

    :param capability_id: 发生状态转换的能力标识
    :param generation: 本次转换的代次
    :param operation: 执行的生命周期操作名称
    :param outcome: 操作结果标识
    :param reason: 触发操作的业务原因
    :param materialization: 记录观测时的实现解析状态
    :param lifecycle: 记录观测时的资源生命周期状态
    :param duration_ms: 操作耗时，单位为毫秒
    :param error: 失败原因文本；成功时通常为 None
    """

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
    """
    同步领域适配器合同。

    :ivar execution_mode: 声明同步执行模式，运行时据此选择同步调用路径
    """

    execution_mode: AdapterExecutionMode

    def materialize(self, spec: CapabilitySpec) -> Any:
        """
        解析 manifest entrypoint 对应的 canonical 实现对象。

        :param spec: 本次资源所属的能力声明
        :return: 清单入口对应的实现对象，尚未创建外部资源实例
        """

    def create(
        self,
        spec: CapabilitySpec,
        implementation: Any,
        generation: int,
        previous: Any = None,
    ) -> Any:
        """
        创建尚未对外发布的候选资源。

        :param spec: 本次资源所属的能力声明
        :param implementation: 由 materialize 解析的实现对象
        :param generation: 本次状态转换的代次，供实现方关联资源和诊断
        :param previous: 重载前的旧实例；首次创建时为 None
        :return: 尚未对外可见的候选资源，由运行时继续启动并发布
        """

    def start(self, spec: CapabilitySpec, candidate: Any, generation: int) -> None:
        """
        启动候选资源；返回前候选不会对普通查询可见。

        :param spec: 本次资源所属的能力声明
        :param candidate: 已创建但尚未发布的候选资源
        :param generation: 本次状态转换的代次，供实现方关联资源和诊断
        """

    def stop(self, spec: CapabilitySpec, instance: Any, generation: int) -> None:
        """
        停止已经撤销运行态可见性的资源。

        :param spec: 本次资源所属的能力声明
        :param instance: 已经撤销对外可见性、需要停止的资源
        :param generation: 本次状态转换的代次，供实现方关联资源和诊断
        """

    def cleanup(
        self,
        spec: CapabilitySpec,
        candidate: Any,
        generation: int,
        error: BaseException,
    ) -> None:
        """
        清理由失败启动留下的候选资源。

        :param spec: 本次资源所属的能力声明
        :param candidate: 已创建但尚未发布的候选资源
        :param generation: 本次状态转换的代次，供实现方关联资源和诊断
        :param error: 候选资源启动失败的原始异常
        """


class AsyncCapabilityAdapter(Protocol):
    """
    异步领域适配器合同。

    :ivar execution_mode: 声明异步执行模式，运行时据此等待协程调用
    """

    execution_mode: AdapterExecutionMode

    def materialize(self, spec: CapabilitySpec) -> Awaitable[Any]:
        """
        异步解析 manifest entrypoint 对应的 canonical 实现对象。

        :param spec: 本次资源所属的能力声明
        :return: 等待后取得清单入口对应的实现对象，尚未创建外部资源实例
        """

    def create(
        self,
        spec: CapabilitySpec,
        implementation: Any,
        generation: int,
        previous: Any = None,
    ) -> Awaitable[Any]:
        """
        异步创建尚未对外发布的候选资源。

        :param spec: 本次资源所属的能力声明
        :param implementation: 由 materialize 解析的实现对象
        :param generation: 本次状态转换的代次，供实现方关联资源和诊断
        :param previous: 重载前的旧实例；首次创建时为 None
        :return: 等待后取得尚未对外可见的候选资源，由运行时继续启动并发布
        """

    def start(
        self,
        spec: CapabilitySpec,
        candidate: Any,
        generation: int,
    ) -> Awaitable[None]:
        """
        异步启动候选资源。

        :param spec: 本次资源所属的能力声明
        :param candidate: 已创建但尚未发布的候选资源
        :param generation: 本次状态转换的代次，供实现方关联资源和诊断
        :return: 等待完成即表示候选资源已启动，随后由运行时决定是否发布，不返回业务数据
        """

    def stop(
        self,
        spec: CapabilitySpec,
        instance: Any,
        generation: int,
    ) -> Awaitable[None]:
        """
        异步停止已经撤销运行态可见性的资源。

        :param spec: 本次资源所属的能力声明
        :param instance: 已经撤销对外可见性、需要停止的资源
        :param generation: 本次状态转换的代次，供实现方关联资源和诊断
        :return: 等待完成即表示该代资源的停止操作完成，不返回业务数据
        """

    def cleanup(
        self,
        spec: CapabilitySpec,
        candidate: Any,
        generation: int,
        error: BaseException,
    ) -> Awaitable[None]:
        """
        异步清理由失败启动留下的候选资源。

        :param spec: 本次资源所属的能力声明
        :param candidate: 已创建但尚未发布的候选资源
        :param generation: 本次状态转换的代次，供实现方关联资源和诊断
        :param error: 候选资源启动失败的原始异常
        :return: 等待完成即表示失败候选的清理操作完成，不返回业务数据
        """


EMPTY_MAPPING: Mapping[str, Any] = MappingProxyType({})
