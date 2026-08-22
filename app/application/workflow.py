"""工作流状态与定义写操作应用用例。"""

from dataclasses import dataclass
import json
from collections.abc import Awaitable
from datetime import datetime
from typing import Any, Callable, Mapping, Optional, Protocol, TypeVar


WORKFLOW_TRIGGER_TIMER = "timer"
WORKFLOW_TRIGGER_EVENT = "event"
WORKFLOW_TRIGGER_MANUAL = "manual"
SUPPORTED_WORKFLOW_TRIGGERS = {
    WORKFLOW_TRIGGER_TIMER,
    WORKFLOW_TRIGGER_EVENT,
    WORKFLOW_TRIGGER_MANUAL,
}


class AsyncWorkflowQueryRepository(Protocol):
    """工作流查询用例需要的异步读取端口。"""

    async def async_list(self) -> list[Any]:
        """读取全部工作流。"""
        ...

    async def async_get(self, workflow_id: int) -> Optional[Any]:
        """按 ID 读取工作流。"""
        ...


class WorkflowQueryService:
    """提供工作流列表和详情查询，隔离 API 与数据库会话。"""

    def __init__(self, repository: AsyncWorkflowQueryRepository) -> None:
        """保存请求级异步查询端口。"""
        self._repository = repository

    async def list(self) -> list[Any]:
        """返回全部工作流。"""
        return await self._repository.async_list()

    async def get(self, workflow_id: int) -> Optional[Any]:
        """返回指定工作流。"""
        return await self._repository.async_get(workflow_id)


_configured_workflow_query: WorkflowQueryService | None = None


def configure_workflow_query(service: WorkflowQueryService) -> None:
    """由启动组合根登记工作流查询服务。"""
    global _configured_workflow_query
    _configured_workflow_query = service


def get_configured_workflow_query() -> WorkflowQueryService:
    """返回启动阶段登记的工作流查询服务。"""
    if _configured_workflow_query is None:
        raise RuntimeError("工作流查询服务尚未配置")
    return _configured_workflow_query


@dataclass(frozen=True, slots=True)
class WorkflowMutationResult:
    """描述工作流写操作是否成功及兼容提示信息。"""

    success: bool
    message: str = ""


class WorkflowMutationRepository(Protocol):
    """工作流写用例需要的最小持久化端口。"""

    def get(self, workflow_id: int) -> Optional[Any]:
        """读取工作流。"""
        ...

    def stage_state(self, workflow_id: int, state: str) -> bool:
        """暂存工作流状态变更。"""
        ...

    def stage_update(self, workflow_id: int, payload: Mapping[str, Any]) -> Optional[Any]:
        """暂存工作流定义更新并返回更新后的对象。"""
        ...

    def stage_delete(self, workflow_id: int) -> None:
        """暂存工作流删除。"""
        ...


class UnitOfWork(Protocol):
    """同步工作流写用例使用的事务端口。"""

    def commit(self) -> None:
        """提交当前事务。"""
        ...

    def rollback(self) -> None:
        """回滚当前事务。"""
        ...


class WorkflowExecutionRepository(Protocol):
    """工作流执行状态写入所需的最小暂存端口。"""

    def stage_start(self, workflow_id: int) -> bool:
        """暂存运行中状态。"""
        ...

    def stage_success(
            self,
            workflow_id: int,
            result: Optional[str] = None,
    ) -> bool:
        """暂存成功状态和执行次数。"""
        ...

    def stage_fail(self, workflow_id: int, result: str) -> bool:
        """暂存失败状态和错误信息。"""
        ...

    def stage_step(
            self,
            workflow_id: int,
            action_id: str,
            context: dict[str, Any],
            execution_state: Optional[dict[str, Any]] = None,
    ) -> bool:
        """暂存动作进度和执行上下文。"""
        ...

    def stage_execution_reset(
            self,
            workflow_id: int,
            reset_count: bool = False,
    ) -> bool:
        """暂存执行状态重置。"""
        ...


_ExecutionResult = TypeVar("_ExecutionResult")


class WorkflowExecutionCommand:
    """在一个显式 UnitOfWork 中提交单次工作流执行状态变更。"""

    def __init__(
            self,
            *,
            repository: WorkflowExecutionRepository,
            unit_of_work: UnitOfWork,
    ) -> None:
        """保存工作流执行仓储和事务端口。"""
        self._repository = repository
        self._unit_of_work = unit_of_work

    def start(self, workflow_id: int) -> bool:
        """提交工作流运行中状态。"""
        return self._commit(lambda: self._repository.stage_start(workflow_id))

    def success(
            self,
            workflow_id: int,
            result: Optional[str] = None,
    ) -> bool:
        """提交工作流成功状态。"""
        return self._commit(
            lambda: self._repository.stage_success(workflow_id, result)
        )

    def fail(self, workflow_id: int, result: str) -> bool:
        """提交工作流失败状态。"""
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
        """提交工作流动作进度。"""
        return self._commit(
            lambda: self._repository.stage_step(
                workflow_id,
                action_id,
                context,
                execution_state,
            )
        )

    def reset(self, workflow_id: int, reset_count: bool = False) -> bool:
        """提交工作流执行状态重置。"""
        return self._commit(
            lambda: self._repository.stage_execution_reset(
                workflow_id,
                reset_count,
            )
        )

    def _commit(self, operation: Callable[[], _ExecutionResult]) -> _ExecutionResult:
        """提交暂存操作；失败时回滚并原样传播异常。"""
        try:
            result = operation()
            self._unit_of_work.commit()
            return result
        except Exception:
            self._unit_of_work.rollback()
            raise


class WorkflowMutationCommand:
    """协调工作流状态、定义、调度和事件注册变更。"""

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
        """保存工作流事务和提交后运行时副作用端口。"""
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
        """启用工作流，并在提交后登记定时器或事件触发器。"""
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
        """停用工作流，并在提交后移除运行时触发器和执行状态。"""
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
        """更新工作流定义，并在提交后刷新调度器和事件注册。"""
        values = dict(payload)
        workflow_id = values.get("id")
        if not workflow_id:
            return WorkflowMutationResult(False, "工作流ID不能为空")
        current = self._repository.get(workflow_id)
        if not current:
            return WorkflowMutationResult(False, "工作流不存在")
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
        ) and updated.timer:
            self._add_timer(updated)
        self._refresh_event(updated)
        return WorkflowMutationResult(True, "更新成功")

    def delete(self, workflow_id: int) -> WorkflowMutationResult:
        """删除工作流，并在提交后清除缓存和运行时触发器。"""
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
        """提交工作流事务，失败时回滚且不执行后续运行时副作用。"""
        try:
            self._unit_of_work.commit()
        except Exception:
            self._unit_of_work.rollback()
            raise


class AsyncWorkflowDefinitionRepository(Protocol):
    """工作流创建、复用和重置需要的异步持久化端口。"""

    async def async_get_by_name(self, name: str) -> Optional[Any]:
        """按名称读取工作流。"""
        ...

    async def stage_create(self, payload: Mapping[str, Any]) -> Any:
        """暂存新工作流。"""
        ...

    async def stage_reset(self, workflow_id: int, reset_count: bool = False) -> Optional[Any]:
        """暂存工作流重置。"""
        ...

    async def async_get(self, workflow_id: int) -> Optional[Any]:
        """读取指定工作流。"""
        ...


class AsyncUnitOfWork(Protocol):
    """异步工作流定义用例使用的事务端口。"""

    async def commit(self) -> None:
        """提交当前事务。"""
        ...

    async def rollback(self) -> None:
        """回滚当前事务。"""
        ...


class WorkflowDefinitionCommand:
    """协调工作流创建、分享复用和重置的异步写用例。"""

    def __init__(
        self,
        *,
        repository: AsyncWorkflowDefinitionRepository,
        unit_of_work: AsyncUnitOfWork,
        stop_running: Callable[[int], None],
        delete_cache: Callable[[int], None],
        report_fork: Optional[Callable[[int], Awaitable[object]]] = None,
    ) -> None:
        """保存异步事务和提交后运行时副作用端口。"""
        self._repository = repository
        self._unit_of_work = unit_of_work
        self._stop_running = stop_running
        self._delete_cache = delete_cache
        self._report_fork = report_fork

    async def create(self, payload: Mapping[str, Any]) -> WorkflowMutationResult:
        """校验名称并暂存新工作流，提交失败时不产生运行时副作用。"""
        values = dict(payload)
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
        """解析共享工作流内容并在提交后更新远程复用次数。"""
        values = dict(payload)
        if not values.get("name"):
            return WorkflowMutationResult(False, "工作流名称不能为空")
        parsed = {}
        for field, default, error_message in (
            ("actions", "[]", "actions字段JSON格式错误"),
            ("flows", "[]", "flows字段JSON格式错误"),
            ("context", "{}", "context字段JSON格式错误"),
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
            "context": parsed["context"],
            "state": "P",
        }
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
        """重置工作流并在提交后停止运行态、清除缓存。"""
        workflow = await self._repository.async_get(workflow_id)
        if not workflow:
            return WorkflowMutationResult(False, "工作流不存在")
        await self._repository.stage_reset(workflow_id, reset_count=True)
        await self._commit()
        self._stop_running(workflow_id)
        self._delete_cache(workflow_id)
        return WorkflowMutationResult(True)

    async def _commit(self) -> None:
        """提交异步事务，失败时回滚且不继续执行运行时副作用。"""
        try:
            await self._unit_of_work.commit()
        except Exception:
            await self._unit_of_work.rollback()
            raise
