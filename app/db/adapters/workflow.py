"""工作流查询与执行状态事务适配器。"""

from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager
from copy import deepcopy
from typing import Any, Optional, TypeVar, cast

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.application.workflow import WorkflowExecutionCommand, WorkflowSnapshot
from app.db.oper.workflow import WorkflowOper
from app.db.uow import SqlAlchemyUnitOfWork
from app.schemas.common import JsonData

_Result = TypeVar("_Result")


def _copy_json_mapping(value: object, field_name: str) -> dict[str, JsonData]:
    """复制 ORM JSON 对象，拒绝把损坏结构带出 Session。"""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"工作流 {field_name} 必须是 JSON 对象")
    return cast(dict[str, JsonData], deepcopy(value))


def _copy_json_sequence(
    value: object,
    field_name: str,
) -> tuple[dict[str, JsonData], ...]:
    """复制 ORM JSON 对象序列，确保快照不共享可变容器。"""
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"工作流 {field_name} 必须是 JSON 对象数组")
    return tuple(cast(dict[str, JsonData], deepcopy(item)) for item in value)


def _project_workflow(record: object) -> WorkflowSnapshot:
    """在持有数据库会话时把 ORM 记录投影为稳定快照。"""
    workflow_id = getattr(record, "id", None)
    name = getattr(record, "name", None)
    state = getattr(record, "state", None)
    if not isinstance(workflow_id, int) or not isinstance(name, str) or not isinstance(state, str):
        raise ValueError("工作流记录缺少稳定身份或状态")
    return WorkflowSnapshot(
        id=workflow_id,
        name=name,
        description=getattr(record, "description", None),
        timer=getattr(record, "timer", None),
        trigger_type=getattr(record, "trigger_type", None),
        event_type=getattr(record, "event_type", None),
        event_conditions=_copy_json_mapping(
            getattr(record, "event_conditions", None),
            "event_conditions",
        ),
        state=state,
        current_action=getattr(record, "current_action", None),
        result=getattr(record, "result", None),
        run_count=getattr(record, "run_count", None),
        actions=_copy_json_sequence(getattr(record, "actions", None), "actions"),
        flows=_copy_json_sequence(getattr(record, "flows", None), "flows"),
        context=_copy_json_mapping(getattr(record, "context", None), "context"),
        execution_config=_copy_json_mapping(
            getattr(record, "execution_config", None),
            "execution_config",
        ),
        execution_state=_copy_json_mapping(
            getattr(record, "execution_state", None),
            "execution_state",
        ),
        add_time=getattr(record, "add_time", None),
        last_time=getattr(record, "last_time", None),
    )


class TransactionalWorkflowQueryRepository:
    """在自有短 Session 内查询并投影工作流快照。"""

    def __init__(
        self,
        sync_session: Callable[[], Session],
        async_session: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        """保存同步 Session 工厂与异步 Session 作用域。"""
        self._sync_session = sync_session
        self._async_session = async_session

    def get(self, workflow_id: int) -> Optional[WorkflowSnapshot]:
        """在同步短 Session 内读取并投影单条工作流。"""
        session = self._sync_session()
        try:
            record = WorkflowOper(session).get(workflow_id)
            return _project_workflow(record) if record else None
        finally:
            session.close()

    def list_enabled(self) -> list[WorkflowSnapshot]:
        """在同步短 Session 内投影全部启用工作流。"""
        return self._list_sync(lambda repository: repository.list_enabled())

    def list_timer_enabled(self) -> list[WorkflowSnapshot]:
        """在同步短 Session 内投影启用的定时工作流。"""
        return self._list_sync(
            lambda repository: repository.get_timer_triggered_workflows()
        )

    def list_event_enabled(self) -> list[WorkflowSnapshot]:
        """在同步短 Session 内投影启用的事件工作流。"""
        return self._list_sync(
            lambda repository: repository.get_event_triggered_workflows()
        )

    async def async_list(
        self,
        *,
        state: Optional[str] = None,
        name: Optional[str] = None,
        trigger_type: Optional[str] = None,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> list[WorkflowSnapshot]:
        """在异步短 Session 内按筛选和分页窗口投影工作流。"""
        async with self._async_session() as session:
            records = await WorkflowOper(session).async_list(
                state=state,
                name=name,
                trigger_type=trigger_type,
                page=page,
                count=count,
            )
            return [_project_workflow(record) for record in records]

    async def async_count(
        self,
        *,
        state: Optional[str] = None,
        name: Optional[str] = None,
        trigger_type: Optional[str] = None,
    ) -> int:
        """在异步短 Session 内按列表筛选统计工作流数量。"""
        async with self._async_session() as session:
            return int(
                await WorkflowOper(session).async_count(
                    state=state,
                    name=name,
                    trigger_type=trigger_type,
                )
            )

    async def async_get(self, workflow_id: int) -> Optional[WorkflowSnapshot]:
        """在异步短 Session 内读取并投影单条工作流。"""
        async with self._async_session() as session:
            record = await WorkflowOper(session).async_get(workflow_id)
            return _project_workflow(record) if record else None

    def _list_sync(
        self,
        operation: Callable[[WorkflowOper], Iterable[object]],
    ) -> list[WorkflowSnapshot]:
        """在同步短 Session 内执行列表查询并完成投影。"""
        session = self._sync_session()
        try:
            return [
                _project_workflow(record)
                for record in operation(WorkflowOper(session))
            ]
        finally:
            session.close()


class TransactionalWorkflowExecutionService:
    """为每次工作流执行状态写入创建独立短会话和 UnitOfWork。"""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        """保存由启动组合根提供的同步 Session 工厂。"""
        self._session_factory = session_factory

    def start(self, workflow_id: int) -> bool:
        """以独立事务提交运行中状态。"""
        return self._run(lambda command: command.start(workflow_id))

    def success(self, workflow_id: int, result: str | None = None) -> bool:
        """以独立事务提交成功状态。"""
        return self._run(lambda command: command.success(workflow_id, result))

    def fail(self, workflow_id: int, result: str) -> bool:
        """以独立事务提交失败状态。"""
        return self._run(lambda command: command.fail(workflow_id, result))

    def step(
            self,
            workflow_id: int,
            action_id: str,
            context: dict[str, Any],
            execution_state: dict[str, Any] | None = None,
    ) -> bool:
        """以独立事务提交动作进度。"""
        return self._run(
            lambda command: command.step(
                workflow_id,
                action_id,
                context,
                execution_state,
            )
        )

    def reset(self, workflow_id: int, reset_count: bool = False) -> bool:
        """以独立事务提交执行状态重置。"""
        return self._run(
            lambda command: command.reset(workflow_id, reset_count)
        )

    def _run(
            self,
            operation: Callable[[WorkflowExecutionCommand], _Result],
    ) -> _Result:
        """创建短会话并把提交/回滚交给 Application command。"""
        session = self._session_factory()
        try:
            command = WorkflowExecutionCommand(
                repository=WorkflowOper(db=session),
                unit_of_work=SqlAlchemyUnitOfWork(session),
            )
            return operation(command)
        finally:
            session.close()
