"""
工作流状态与定义写操作应用用例。
"""

import json
from collections import deque
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, List, Mapping, Optional, Protocol, TypeVar

from app.schemas.common import JsonData

WORKFLOW_TRIGGER_TIMER = "timer"
WORKFLOW_TRIGGER_EVENT = "event"
WORKFLOW_TRIGGER_MANUAL = "manual"
SUPPORTED_WORKFLOW_TRIGGERS = {
    WORKFLOW_TRIGGER_TIMER,
    WORKFLOW_TRIGGER_EVENT,
    WORKFLOW_TRIGGER_MANUAL,
}


def _workflow_graph_entries(value: object, kind: str) -> tuple[Mapping[str, object], ...]:
    """
    规范化动作或流程列表，并拒绝共享 JSON 中格式错误的条目。
    """
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"工作流{kind}必须为列表")
    entries: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError(f"工作流{kind}定义格式错误")
        entries.append(item)
    return tuple(entries)


def _workflow_action_ids(actions: tuple[Mapping[str, object], ...]) -> set[str]:
    """
    提取非空且唯一的动作 ID，避免执行器字典静默覆盖节点。
    """
    action_ids: set[str] = set()
    for action in actions:
        action_id = action.get("id")
        if not isinstance(action_id, str) or not action_id.strip():
            raise ValueError("工作流动作缺少 ID")
        if action_id in action_ids:
            raise ValueError(f"工作流动作 ID 重复：{action_id}")
        action_ids.add(action_id)
    return action_ids


def _workflow_flow_endpoint(value: object, action_ids: set[str]) -> str:
    """
    校验每个流程端点都指向图中已有的动作。
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("工作流流程端点不能为空")
    if value not in action_ids:
        raise ValueError(f"工作流流程端点不存在：{value}")
    return value


def _workflow_graph_edges(
    flows: tuple[Mapping[str, object], ...], action_ids: set[str],
) -> tuple[dict[str, list[str]], dict[str, int]]:
    """
    校验流程 ID 和端点，并构造拓扑校验需要的出边与入度表。
    """
    outgoing: dict[str, list[str]] = {action_id: [] for action_id in action_ids}
    incoming = dict.fromkeys(action_ids, 0)
    flow_ids: set[str] = set()
    for flow in flows:
        flow_id = flow.get("id")
        if flow_id is not None:
            if not isinstance(flow_id, str):
                raise ValueError("工作流流程 ID 格式错误")
            if flow_id and flow_id in flow_ids:
                raise ValueError(f"工作流流程 ID 重复：{flow_id}")
            if flow_id:
                flow_ids.add(flow_id)
        source = _workflow_flow_endpoint(flow.get("source"), action_ids)
        target = _workflow_flow_endpoint(flow.get("target"), action_ids)
        outgoing[source].append(target)
        incoming[target] += 1
    return outgoing, incoming


def validate_workflow_graph(actions: object, flows: object) -> None:
    """
    校验工作流有向无环图；允许无动作草稿，但拒绝重复 ID 和无效流程端点。
    """
    action_ids = _workflow_action_ids(_workflow_graph_entries(actions, "动作"))
    outgoing, incoming = _workflow_graph_edges(_workflow_graph_entries(flows, "流程"), action_ids)
    ready = deque(action_id for action_id, degree in incoming.items() if degree == 0)
    visited = 0
    while ready:
        source = ready.popleft()
        visited += 1
        for target in outgoing[source]:
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
    if visited != len(action_ids):
        raise ValueError("工作流不能包含循环")


@dataclass(frozen=True, slots=True)
class WorkflowSnapshot:
    """
    工作流查询返回的脱离数据库会话的冻结快照。

    :param id: 工作流持久化主键
    :param name: 工作流展示名称
    :param description: 工作流用途说明，未填写时为 None
    :param timer: 定时触发使用的计划表达式，非定时工作流可为 None
    :param trigger_type: 触发类型：timer、event 或 manual，旧记录可能未填写
    :param event_type: 事件触发的事件类型名称，非事件工作流可为 None
    :param event_conditions: 事件触发时需要匹配的条件映射
    :param state: 持久化状态：W 等待、R 运行中、P 暂停、S 成功、F 失败
    :param current_action: 已执行动作标识组成的逗号分隔列表，尚未执行时可为 None
    :param result: 最近执行结果摘要，尚未产生时为 None
    :param run_count: 累计运行次数，旧记录可能未填写
    :param actions: 按存储顺序冻结的动作节点定义
    :param flows: 连接动作节点的有向流程边定义
    :param context: 工作流持久化的业务上下文
    :param execution_config: 动作执行时采用的运行配置
    :param execution_state: 节点执行和恢复所需的持久化状态
    :param add_time: 工作流创建时间字符串，未记录时为 None
    :param last_time: 最近运行时间字符串，未运行时为 None
    """

    id: int
    name: str
    description: Optional[str]
    timer: Optional[str]
    trigger_type: Optional[str]
    event_type: Optional[str]
    event_conditions: Mapping[str, JsonData]
    state: str
    current_action: Optional[str]
    result: Optional[str]
    run_count: Optional[int]
    actions: tuple[Mapping[str, JsonData], ...]
    flows: tuple[Mapping[str, JsonData], ...]
    context: Mapping[str, JsonData]
    execution_config: Mapping[str, JsonData]
    execution_state: Mapping[str, JsonData]
    add_time: Optional[str]
    last_time: Optional[str]


class WorkflowRuntime(Protocol):
    """
    声明宿主入口与 Chain 消费的工作流运行时能力。
    """

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        """
        执行单个工作流动作，参数与具体管理器保持一致。

        :param args: 位置参数依次为工作流标识、动作定义及可选执行上下文
        :param kwargs: 可选动作输入 inputs、运行参数 runtime、取消令牌 cancel_token 等命名参数
        :return: 包含动作成功状态、结果信息和后续上下文的执行结果
        """
        ...

    def list_actions(self) -> list[dict[str, Any]]:
        """
        返回当前运行时登记的工作流动作定义。

        :return: 已注册动作的类型、名称、参数等定义列表
        """
        ...

    def load_workflow_events(self, workflow_id: Optional[int] = None) -> None:
        """
        加载全部或指定工作流的事件触发器。

        :param workflow_id: 需要重新加载的工作流主键，None 表示加载全部事件工作流
        """
        ...

    def remove_workflow_event(
            self,
            workflow_id: Optional[int] = None,
            event_type_str: Optional[str] = None,
    ) -> None:
        """
        移除全部或指定工作流的事件触发器。

        :param workflow_id: 需要移除监听的工作流主键，None 表示移除范围内全部工作流
        :param event_type_str: 限定移除的事件类型，None 表示遍历全部已登记事件类型
        """
        ...

    def update_workflow_event(self, workflow: WorkflowSnapshot) -> None:
        """
        按最新定义刷新工作流事件触发器。

        :param workflow: 已持久化的工作流最新定义快照
        """
        ...

    def register_execution(self, owner: "WorkflowExecutionOwner") -> bool:
        """
        原子登记活动执行，同 ID 已在运行或停机封口后返回 False。

        :param owner: 当前执行实例的停止、等待和工作流标识能力
        :return: 成功登记时为 True，同工作流已有执行或宿主停止接收时为 False
        """
        ...

    def unregister_execution(self, owner: "WorkflowExecutionOwner") -> None:
        """
        在工作流执行真实终止后释放 owner。

        :param owner: 已真实结束的执行实例；只能释放该实例拥有的活动登记
        """
        ...


class WorkflowExecutionOwner(Protocol):
    """
    声明具体工作流管理器需要持有的执行生命周期能力。
    """

    @property
    def workflow_id(self) -> int:
        """
        返回同一工作流执行互斥使用的稳定 ID。

        :return: 用于阻止同一工作流并发重复运行的持久化主键
        """
        ...

    def request_stop(self) -> None:
        """
        请求停止继续调度，并通知支持取消的活动动作。
        """
        ...

    def wait_stopped(self, timeout: float) -> bool:
        """
        有限等待执行及其节点线程池真实终止。

        :param timeout: 最多等待的秒数
        :return: 执行及节点线程池已真实结束时为 True，超时仍未结束时为 False
        """
        ...


WorkflowRuntimeProvider = Callable[[], WorkflowRuntime]


def _unconfigured_workflow_runtime() -> WorkflowRuntime:
    """
    拒绝在启动组合根装配前隐式创建工作流管理器。
    """
    raise RuntimeError("工作流运行时尚未由启动组合根装配")


_workflow_runtime_provider: WorkflowRuntimeProvider = _unconfigured_workflow_runtime


def configure_workflow_runtime(
    provider: Optional[WorkflowRuntimeProvider],
) -> WorkflowRuntimeProvider:
    """
    登记工作流运行时 provider，并返回先前 provider 供失败回滚。
    """
    global _workflow_runtime_provider
    previous = _workflow_runtime_provider
    _workflow_runtime_provider = provider or _unconfigured_workflow_runtime
    return previous


def reset_workflow_runtime() -> None:
    """
    恢复未装配 provider，禁止重复 lifespan 复用旧运行时。
    """
    configure_workflow_runtime(None)


def get_workflow_manager() -> WorkflowRuntime:
    """
    返回组合根提供的工作流运行时，避免消费者直接定位 Singleton。
    """
    return _workflow_runtime_provider()


class WorkflowQueryRepository(Protocol):
    """
    工作流查询用例需要的同步与异步快照端口。
    """

    def get(self, workflow_id: int) -> Optional[WorkflowSnapshot]:
        """
        按 ID 读取工作流快照。

        :param workflow_id: 工作流持久化主键
        :return: 脱离数据库会话的工作流快照，不存在时为 None
        """
        ...

    def list_enabled(self) -> List[WorkflowSnapshot]:
        """
        读取全部启用的工作流快照。

        :return: 全部未暂停的工作流快照
        """
        ...

    def list_timer_enabled(self) -> List[WorkflowSnapshot]:
        """
        读取启用的定时工作流快照。

        :return: 全部未暂停的定时工作流，包含未声明触发类型的旧记录
        """
        ...

    def list_event_enabled(self) -> List[WorkflowSnapshot]:
        """
        读取启用的事件工作流快照。

        :return: 全部未暂停的事件工作流快照
        """
        ...

    async def async_list(
        self,
        *,
        state: Optional[str] = None,
        name: Optional[str] = None,
        trigger_type: Optional[str] = None,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> List[WorkflowSnapshot]:
        """
        按可选筛选与分页窗口异步读取工作流快照。

        :param state: 状态筛选：W 等待、R 运行中、P 暂停、S 成功、F 失败；None 表示不限制
        :param name: 名称的字面量包含匹配，None 或空字符串表示不限制
        :param trigger_type: timer、event 或 manual 触发类型；timer 兼容未声明类型的旧记录，None 不限制
        :param page: 页码，从 1 开始；必须与 count 同时提供才启用分页
        :param count: 每页条数；None 或未提供 page 时读取全部匹配记录
        :return: 按工作流主键排序的匹配快照列表
        """
        ...

    async def async_count(
        self,
        *,
        state: Optional[str] = None,
        name: Optional[str] = None,
        trigger_type: Optional[str] = None,
    ) -> int:
        """
        按与列表一致的筛选条件统计工作流数量。

        :param state: 状态筛选：W 等待、R 运行中、P 暂停、S 成功、F 失败；None 表示不限制
        :param name: 名称的字面量包含匹配，None 或空字符串表示不限制
        :param trigger_type: timer、event 或 manual 触发类型；timer 兼容未声明类型的旧记录，None 不限制
        :return: 符合相同筛选条件的记录总数，不受分页窗口影响
        """
        ...

    async def async_get(self, workflow_id: int) -> Optional[WorkflowSnapshot]:
        """
        异步按 ID 读取工作流快照。

        :param workflow_id: 工作流持久化主键
        :return: 脱离数据库会话的工作流快照，不存在时为 None
        """
        ...


class WorkflowCachePort(Protocol):
    """
    工作流重置所需的同步与异步配置缓存端口。
    """

    def delete(self, key: Any) -> Any:
        """
        删除配置缓存。

        :param key: 要删除的工作流执行配置缓存键
        :return: 底层缓存删除结果，调用方不据此判断工作流是否存在
        """
        ...

    async def async_delete(self, key: Any) -> Any:
        """
        通过异步数据库执行端口删除配置缓存。

        :param key: 要删除的工作流执行配置缓存键
        :return: 底层缓存删除结果，调用方不据此判断工作流是否存在
        """
        ...


class WorkflowQueryService:
    """
    提供工作流列表和详情查询，隔离 API 与数据库会话。
    """

    def __init__(self, repository: WorkflowQueryRepository) -> None:
        """
        保存可返回脱离会话快照的查询端口。
        """
        self._repository = repository

    async def list(
        self,
        *,
        state: Optional[str] = None,
        name: Optional[str] = None,
        trigger_type: Optional[str] = None,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> List[WorkflowSnapshot]:
        """
        按可选筛选与分页窗口返回工作流快照。
        """
        if all(
            value is None
            for value in (state, name, trigger_type, page, count)
        ):
            return await self._repository.async_list()
        return await self._repository.async_list(
            state=state,
            name=name,
            trigger_type=trigger_type,
            page=page,
            count=count,
        )

    async def count(
        self,
        *,
        state: Optional[str] = None,
        name: Optional[str] = None,
        trigger_type: Optional[str] = None,
    ) -> int:
        """
        按与列表一致的筛选条件返回工作流总数。
        """
        return await self._repository.async_count(
            state=state,
            name=name,
            trigger_type=trigger_type,
        )

    async def get(self, workflow_id: int) -> Optional[WorkflowSnapshot]:
        """
        返回指定工作流快照。
        """
        return await self._repository.async_get(workflow_id)

    def get_sync(self, workflow_id: int) -> Optional[WorkflowSnapshot]:
        """
        同步返回指定工作流快照。
        """
        return self._repository.get(workflow_id)

    def list_enabled(self) -> List[WorkflowSnapshot]:
        """
        同步返回全部启用的工作流快照。
        """
        return self._repository.list_enabled()

    def list_timer_enabled(self) -> List[WorkflowSnapshot]:
        """
        同步返回启用的定时工作流快照。
        """
        return self._repository.list_timer_enabled()

    def list_event_enabled(self) -> List[WorkflowSnapshot]:
        """
        同步返回启用的事件工作流快照。
        """
        return self._repository.list_event_enabled()


_configured_workflow_query: WorkflowQueryService | None = None


def configure_workflow_query(service: WorkflowQueryService) -> None:
    """
    由启动组合根登记工作流查询服务。
    """
    global _configured_workflow_query
    _configured_workflow_query = service


def reset_workflow_query() -> None:
    """
    清除当前 lifespan 的工作流查询服务。
    """
    global _configured_workflow_query
    _configured_workflow_query = None


def get_configured_workflow_query() -> WorkflowQueryService:
    """
    返回启动阶段登记的工作流查询服务。
    """
    if _configured_workflow_query is None:
        raise RuntimeError("工作流查询服务尚未配置")
    return _configured_workflow_query


@dataclass(frozen=True, slots=True)
class WorkflowMutationResult:
    """
    描述工作流写操作是否成功及兼容提示信息。

    :param success: 工作流修改是否完成
    :param message: 可向用户展示的修改结果或拒绝原因
    """

    success: bool
    message: str = ""


class WorkflowMutationRepository(Protocol):
    """
    工作流写用例需要的最小持久化端口。
    """

    def get(self, workflow_id: int) -> Optional[Any]:
        """
        读取工作流。

        :param workflow_id: 工作流持久化主键
        :return: 当前事务内的工作流对象，不存在时为 None
        """
        ...

    def stage_state(self, workflow_id: int, state: str) -> bool:
        """
        暂存工作流状态变更。

        :param workflow_id: 工作流持久化主键
        :param state: 待写入的工作流状态，如 W 启用等待、P 暂停
        :return: 找到记录并暂存状态时为 True，不存在时为 False；事务由调用方提交
        """
        ...

    def stage_update(self, workflow_id: int, payload: Mapping[str, Any]) -> Optional[Any]:
        """
        暂存工作流定义更新并返回更新后的对象。

        :param workflow_id: 工作流持久化主键
        :param payload: 已通过用例校验的工作流定义字段，主键不参与修改
        :return: 当前事务中更新后的对象，不存在时为 None；本方法不提交事务
        """
        ...

    def stage_delete(self, workflow_id: int) -> None:
        """
        暂存工作流删除。

        :param workflow_id: 工作流持久化主键
        """
        ...


class UnitOfWork(Protocol):
    """
    同步工作流写用例使用的事务端口。
    """

    def commit(self) -> None:
        """
        提交当前事务。
        """
        ...

    def rollback(self) -> None:
        """
        回滚当前事务。
        """
        ...


class WorkflowExecutionPort(Protocol):
    """
    工作流 Chain 提交执行状态所需的类型化事务端口。
    """

    def start(self, workflow_id: int) -> bool:
        """
        提交工作流运行中状态。

        :param workflow_id: 工作流持久化主键
        :return: 状态写入操作已完成时为 True；不表示数据库中一定存在匹配记录，返回前已提交事务
        """
        ...

    def success(
            self,
            workflow_id: int,
            result: Optional[str] = None,
    ) -> bool:
        """
        提交工作流成功状态。

        :param workflow_id: 工作流持久化主键
        :param result: 执行成功的结果摘要，None 表示没有补充结果
        :return: 状态写入操作已完成时为 True；不表示数据库中一定存在匹配记录，返回前已提交事务
        """
        ...

    def fail(self, workflow_id: int, result: str) -> bool:
        """
        提交工作流失败状态。

        :param workflow_id: 工作流持久化主键
        :param result: 执行失败原因摘要
        :return: 状态写入操作已完成时为 True；不表示数据库中一定存在匹配记录，返回前已提交事务
        """
        ...

    def step(
            self,
            workflow_id: int,
            action_id: str,
            context: dict[str, Any],
            execution_state: Optional[dict[str, Any]] = None,
    ) -> bool:
        """
        提交工作流动作进度。

        :param workflow_id: 工作流持久化主键
        :param action_id: 本次已执行的动作标识，追加到去重后的已执行动作列表
        :param context: 本次动作完成后的业务上下文快照
        :param execution_state: 结构化节点执行状态，None 表示保留已有状态
        :return: 状态写入操作已完成时为 True；不表示数据库中一定存在匹配记录，返回前已提交事务
        """
        ...

    def reset(self, workflow_id: int, reset_count: bool = False) -> bool:
        """
        提交工作流执行状态重置。

        :param workflow_id: 工作流持久化主键
        :param reset_count: 是否同时将累计运行次数归零，默认保留次数
        :return: 状态写入操作已完成时为 True；不表示数据库中一定存在匹配记录，返回前已提交事务
        """
        ...


_configured_workflow_execution: Optional[WorkflowExecutionPort] = None


def configure_workflow_execution(service: WorkflowExecutionPort) -> None:
    """
    由启动组合根登记唯一工作流执行状态事务服务。
    """
    global _configured_workflow_execution
    _configured_workflow_execution = service


def reset_workflow_execution() -> None:
    """
    清除当前 lifespan 的工作流执行状态事务服务。
    """
    global _configured_workflow_execution
    _configured_workflow_execution = None


def get_configured_workflow_execution() -> WorkflowExecutionPort:
    """
    返回启动阶段登记的工作流执行状态事务服务。
    """
    if _configured_workflow_execution is None:
        raise RuntimeError("工作流执行状态事务服务尚未配置")
    return _configured_workflow_execution


class WorkflowExecutionRepository(Protocol):
    """
    工作流执行状态写入所需的最小暂存端口。
    """

    def stage_start(self, workflow_id: int) -> bool:
        """
        暂存运行中状态。

        :param workflow_id: 工作流持久化主键
        :return: 状态写入操作已完成时为 True；不表示数据库中一定存在匹配记录，事务由调用方提交
        """
        ...

    def stage_success(
            self,
            workflow_id: int,
            result: Optional[str] = None,
    ) -> bool:
        """
        暂存成功状态和执行次数。

        :param workflow_id: 工作流持久化主键
        :param result: 执行成功的结果摘要，None 表示没有补充结果
        :return: 状态写入操作已完成时为 True；不表示数据库中一定存在匹配记录，事务由调用方提交
        """
        ...

    def stage_fail(self, workflow_id: int, result: str) -> bool:
        """
        暂存失败状态和错误信息。

        :param workflow_id: 工作流持久化主键
        :param result: 执行失败原因摘要
        :return: 状态写入操作已完成时为 True；不表示数据库中一定存在匹配记录，事务由调用方提交
        """
        ...

    def stage_step(
            self,
            workflow_id: int,
            action_id: str,
            context: dict[str, Any],
            execution_state: Optional[dict[str, Any]] = None,
    ) -> bool:
        """
        暂存动作进度和执行上下文。

        :param workflow_id: 工作流持久化主键
        :param action_id: 本次已执行的动作标识，追加到去重后的已执行动作列表
        :param context: 本次动作完成后的业务上下文快照
        :param execution_state: 结构化节点执行状态，None 表示保留已有状态
        :return: 状态写入操作已完成时为 True；不表示数据库中一定存在匹配记录，事务由调用方提交
        """
        ...

    def stage_execution_reset(
            self,
            workflow_id: int,
            reset_count: bool = False,
    ) -> bool:
        """
        暂存执行状态重置。

        :param workflow_id: 工作流持久化主键
        :param reset_count: 是否同时将累计运行次数归零，默认保留次数
        :return: 状态写入操作已完成时为 True；不表示数据库中一定存在匹配记录，事务由调用方提交
        """
        ...


_ExecutionResult = TypeVar("_ExecutionResult")


class WorkflowExecutionCommand:
    """
    在一个显式 UnitOfWork 中提交单次工作流执行状态变更。
    """

    def __init__(
            self,
            *,
            repository: WorkflowExecutionRepository,
            unit_of_work: UnitOfWork,
    ) -> None:
        """
        保存工作流执行仓储和事务端口。
        """
        self._repository = repository
        self._unit_of_work = unit_of_work

    def start(self, workflow_id: int) -> bool:
        """
        提交工作流运行中状态。
        """
        return self._commit(lambda: self._repository.stage_start(workflow_id))

    def success(
            self,
            workflow_id: int,
            result: Optional[str] = None,
    ) -> bool:
        """
        提交工作流成功状态。
        """
        return self._commit(
            lambda: self._repository.stage_success(workflow_id, result)
        )

    def fail(self, workflow_id: int, result: str) -> bool:
        """
        提交工作流失败状态。
        """
        return self._commit(
            lambda: self._repository.stage_fail(workflow_id, result)
        )

    def step(
            self,
            workflow_id: int,
            action_id: str,
            context: dict[str, Any],
            execution_state: Optional[dict[str, Any]] = None,
    ) -> bool:
        """
        提交工作流动作进度。
        """
        return self._commit(
            lambda: self._repository.stage_step(
                workflow_id,
                action_id,
                context,
                execution_state,
            )
        )

    def reset(self, workflow_id: int, reset_count: bool = False) -> bool:
        """
        提交工作流执行状态重置。
        """
        return self._commit(
            lambda: self._repository.stage_execution_reset(
                workflow_id,
                reset_count,
            )
        )

    def _commit(self, operation: Callable[[], _ExecutionResult]) -> _ExecutionResult:
        """
        提交暂存操作；失败时回滚并原样传播异常。
        """
        try:
            result = operation()
            self._unit_of_work.commit()
            return result
        except Exception:
            self._unit_of_work.rollback()
            raise


class WorkflowMutationCommand:
    """
    协调工作流状态、定义、调度和事件注册变更。
    """

    def __init__(
            self,
            *,
            repository: WorkflowMutationRepository,
            unit_of_work: UnitOfWork,
            add_timer: Callable[[Any], None],
            remove_timer: Callable[[Any], None],
            load_event: Callable[[int], None],
            remove_event: Callable[[int, Optional[str]], None],
            refresh_event: Callable[[Any], None],
            stop_running: Callable[[int], None],
            delete_cache: Callable[[int], None],
    ) -> None:
        """
        保存工作流事务和提交后运行时副作用端口。
        """
        self._repository = repository
        self._unit_of_work = unit_of_work
        self._add_timer = add_timer
        self._remove_timer = remove_timer
        self._load_event = load_event
        self._remove_event = remove_event
        self._refresh_event = refresh_event
        self._stop_running = stop_running
        self._delete_cache = delete_cache

    def start(self, workflow_id: int) -> WorkflowMutationResult:
        """
        启用工作流，并在提交后登记定时器或事件触发器。
        """
        workflow = self._repository.get(workflow_id)
        if not workflow:
            return WorkflowMutationResult(False, "工作流不存在")
        trigger_type = workflow.trigger_type or WORKFLOW_TRIGGER_TIMER
        if trigger_type == WORKFLOW_TRIGGER_TIMER and not workflow.timer:
            return WorkflowMutationResult(False, "定时工作流缺少定时器配置")
        if trigger_type not in SUPPORTED_WORKFLOW_TRIGGERS:
            return WorkflowMutationResult(False, "工作流触发类型不支持")

        self._repository.stage_state(workflow_id, "W")
        self._commit()
        if trigger_type == WORKFLOW_TRIGGER_TIMER:
            self._add_timer(workflow)
        elif trigger_type == WORKFLOW_TRIGGER_EVENT:
            self._load_event(workflow_id)
        return WorkflowMutationResult(True)

    def pause(self, workflow_id: int) -> WorkflowMutationResult:
        """
        停用工作流，并在提交后移除运行时触发器和执行状态。
        """
        workflow = self._repository.get(workflow_id)
        if not workflow:
            return WorkflowMutationResult(False, "工作流不存在")

        self._repository.stage_state(workflow_id, "P")
        self._commit()
        if workflow.trigger_type == WORKFLOW_TRIGGER_TIMER:
            self._remove_timer(workflow)
        elif workflow.trigger_type == WORKFLOW_TRIGGER_EVENT:
            self._remove_event(workflow_id, workflow.event_type)
        self._stop_running(workflow_id)
        return WorkflowMutationResult(True)

    def update(self, payload: Mapping[str, Any]) -> WorkflowMutationResult:
        """
        更新工作流定义，并在提交后刷新调度器和事件注册。
        """
        values = dict(payload)
        workflow_id = values.get("id")
        if not workflow_id:
            return WorkflowMutationResult(False, "工作流ID不能为空")
        current = self._repository.get(workflow_id)
        if not current:
            return WorkflowMutationResult(False, "工作流不存在")
        try:
            validate_workflow_graph(
                values.get("actions", getattr(current, "actions", None)),
                values.get("flows", getattr(current, "flows", None)),
            )
        except ValueError as err:
            return WorkflowMutationResult(False, str(err))
        if not current.trigger_type:
            values["trigger_type"] = WORKFLOW_TRIGGER_TIMER

        updated = self._repository.stage_update(workflow_id, values)
        if not updated:
            self._unit_of_work.rollback()
            return WorkflowMutationResult(False, "工作流不存在")
        self._commit()
        self._remove_timer(updated)
        if (
                not updated.trigger_type
                or updated.trigger_type == WORKFLOW_TRIGGER_TIMER
        ) and updated.timer and getattr(updated, "state", None) != "P":
            self._add_timer(updated)
        self._refresh_event(updated)
        return WorkflowMutationResult(True, "更新成功")

    def delete(self, workflow_id: int) -> WorkflowMutationResult:
        """
        删除工作流，并在提交后清除缓存和运行时触发器。
        """
        workflow = self._repository.get(workflow_id)
        if not workflow:
            return WorkflowMutationResult(False, "工作流不存在")

        self._repository.stage_delete(workflow_id)
        self._commit()
        self._delete_cache(workflow_id)
        if not workflow.trigger_type or workflow.trigger_type == WORKFLOW_TRIGGER_TIMER:
            self._remove_timer(workflow)
        elif workflow.trigger_type == WORKFLOW_TRIGGER_EVENT:
            self._remove_event(workflow_id, workflow.event_type)
        return WorkflowMutationResult(True, "删除成功")

    def _commit(self) -> None:
        """
        提交工作流事务，失败时回滚且不执行后续运行时副作用。
        """
        try:
            self._unit_of_work.commit()
        except Exception:
            self._unit_of_work.rollback()
            raise


class AsyncWorkflowDefinitionRepository(Protocol):
    """
    工作流创建、复用和重置需要的异步持久化端口。
    """

    async def async_get_by_name(self, name: str) -> Optional[Any]:
        """
        按名称读取工作流。

        :param name: 用于同名校验的完整工作流名称
        :return: 名称匹配的工作流对象，不存在时为 None
        """
        ...

    async def stage_create(self, payload: Mapping[str, Any]) -> Any:
        """
        暂存新工作流。

        :param payload: 已校验的工作流名称、图定义和触发配置等创建字段
        :return: 已暂存并取得主键的新工作流对象，事务由调用方提交
        """
        ...

    async def stage_reset(self, workflow_id: int, reset_count: bool = False) -> Optional[Any]:
        """
        暂存工作流重置。

        :param workflow_id: 工作流持久化主键
        :param reset_count: 是否同时将累计运行次数归零，默认保留次数
        :return: 清空执行进度、上下文并置为等待状态后的对象，不存在时为 None；不提交事务
        """
        ...

    async def async_get(self, workflow_id: int) -> Optional[Any]:
        """
        读取指定工作流。

        :param workflow_id: 工作流持久化主键
        :return: 当前事务中的工作流对象，不存在时为 None
        """
        ...


class AsyncUnitOfWork(Protocol):
    """
    异步工作流定义用例使用的事务端口。
    """

    async def commit(self) -> None:
        """
        提交当前事务。
        """
        ...

    async def rollback(self) -> None:
        """
        回滚当前事务。
        """
        ...


class WorkflowDefinitionCommand:
    """
    协调工作流创建、分享复用和重置的异步写用例。
    """

    def __init__(
        self,
        *,
        repository: AsyncWorkflowDefinitionRepository,
        unit_of_work: AsyncUnitOfWork,
        stop_running: Callable[[int], None],
        async_delete_cache: Callable[[int], Awaitable[Any]],
        report_fork: Optional[Callable[[int], Awaitable[object]]] = None,
    ) -> None:
        """
        保存异步事务和提交后运行时副作用端口。
        """
        self._repository = repository
        self._unit_of_work = unit_of_work
        self._stop_running = stop_running
        self._async_delete_cache = async_delete_cache
        self._report_fork = report_fork

    async def create(self, payload: Mapping[str, Any]) -> WorkflowMutationResult:
        """
        校验名称并暂存新工作流，提交失败时不产生运行时副作用。
        """
        values = dict(payload)
        try:
            validate_workflow_graph(values.get("actions"), values.get("flows"))
        except ValueError as err:
            return WorkflowMutationResult(False, str(err))
        name = values.get("name")
        if name and await self._repository.async_get_by_name(name):
            return WorkflowMutationResult(False, "已存在相同名称的工作流")
        if not values.get("add_time"):
            values["add_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not values.get("state"):
            values["state"] = "P"
        if not values.get("trigger_type"):
            values["trigger_type"] = WORKFLOW_TRIGGER_TIMER
        try:
            await self._repository.stage_create(values)
            await self._commit()
        except Exception:
            raise
        return WorkflowMutationResult(True, "创建工作流成功")

    async def fork(
        self,
        payload: Mapping[str, Any],
        share_id: Optional[int] = None,
    ) -> WorkflowMutationResult:
        """
        解析共享工作流内容并在提交后更新远程复用次数。

        只复用动作、流程和触发配置；执行上下文属于分享者本机的运行状态，对新工作流没有意义，
        且来自外部不可信，因此忽略载荷中的 ``context``，新工作流固定从空上下文开始。
        """
        values = dict(payload)
        if not values.get("name"):
            return WorkflowMutationResult(False, "工作流名称不能为空")
        parsed = {}
        for field, default, error_message in (
            ("actions", "[]", "actions字段JSON格式错误"),
            ("flows", "[]", "flows字段JSON格式错误"),
            ("event_conditions", "{}", "event_conditions字段JSON格式错误"),
        ):
            raw = values.get(field)
            try:
                parsed[field] = json.loads(raw or default)
            except json.JSONDecodeError:
                return WorkflowMutationResult(False, error_message)
        workflow_values = {
            "name": values["name"],
            "description": values.get("description"),
            "timer": values.get("timer"),
            "trigger_type": values.get("trigger_type") or WORKFLOW_TRIGGER_TIMER,
            "event_type": values.get("event_type"),
            "event_conditions": parsed["event_conditions"],
            "actions": parsed["actions"],
            "flows": parsed["flows"],
            "context": {},
            "state": "P",
        }
        try:
            validate_workflow_graph(workflow_values["actions"], workflow_values["flows"])
        except ValueError as err:
            return WorkflowMutationResult(False, str(err))
        if await self._repository.async_get_by_name(workflow_values["name"]):
            return WorkflowMutationResult(False, "已存在相同名称的工作流")
        try:
            created = await self._repository.stage_create(workflow_values)
            await self._commit()
        except Exception:
            raise
        if created and share_id and self._report_fork:
            try:
                await self._report_fork(share_id)
            except Exception:
                return WorkflowMutationResult(True, "复用成功；共享统计上报失败")
        return WorkflowMutationResult(True, "复用成功")

    async def reset(self, workflow_id: int) -> WorkflowMutationResult:
        """
        重置工作流并在提交后停止运行态、清除缓存。
        """
        workflow = await self._repository.async_get(workflow_id)
        if not workflow:
            return WorkflowMutationResult(False, "工作流不存在")
        await self._repository.stage_reset(workflow_id, reset_count=True)
        await self._commit()
        self._stop_running(workflow_id)
        await self._async_delete_cache(workflow_id)
        return WorkflowMutationResult(True)

    async def _commit(self) -> None:
        """
        提交异步事务，失败时回滚且不继续执行运行时副作用。
        """
        try:
            await self._unit_of_work.commit()
        except Exception:
            await self._unit_of_work.rollback()
            raise
