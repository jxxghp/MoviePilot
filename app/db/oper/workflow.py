from typing import Any, List, Mapping, Optional, Tuple

from sqlalchemy import delete as sqlalchemy_delete
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.base import DbOper
from app.db.models.workflow import Workflow
from app.db.oper.query import literal_contains


def _workflow_conditions(
    *,
    state: Optional[str] = None,
    name: Optional[str] = None,
    trigger_type: Optional[str] = None,
) -> list[Any]:
    """构造工作流列表与计数共享的数据库筛选条件。"""
    conditions: list[Any] = []
    if state:
        conditions.append(Workflow.state == state)
    if name:
        conditions.append(literal_contains(Workflow.name, name))
    if trigger_type == "timer":
        conditions.append(
            or_(Workflow.trigger_type == "timer", Workflow.trigger_type.is_(None))
        )
    elif trigger_type:
        conditions.append(Workflow.trigger_type == trigger_type)
    return conditions


async def _async_workflow_rows(session: Any, statement: Any) -> List[Workflow]:
    """执行工作流列表语句并返回 ORM 行。"""
    result = await session.execute(statement)
    return list(result.scalars().all())


async def _async_scalar(session: Any, statement: Any) -> int:
    """执行工作流计数语句并返回整数。"""
    result = await session.execute(statement)
    return int(result.scalar_one() or 0)


class WorkflowOper(DbOper):
    """
    工作流管理
    """

    def add(self, **kwargs) -> Tuple[bool, str]:
        """
        新增工作流
        """
        wf = Workflow(**kwargs)
        if not self.get_by_name(kwargs.get("name")):
            self._stage_create(wf)
            return True, "新增工作流成功"
        return False, "工作流已存在"

    def get(self, wid: int) -> Optional[Workflow]:
        """
        查询单个工作流
        """
        return self._execute_sync_query(lambda session: Workflow.get(session, wid))

    def stage_state(self, workflow_id: int, state: str) -> bool:
        """暂存工作流状态变更，不由模型方法自行提交。"""
        workflow = self.get(workflow_id)
        if not workflow:
            return False
        workflow.state = state
        return True

    def stage_update(
            self,
            workflow_id: int,
            payload: Mapping[str, Any],
    ) -> Optional[Workflow]:
        """暂存工作流字段更新并返回同一会话中的对象。"""
        workflow = self.get(workflow_id)
        if not workflow:
            return None
        for key, value in payload.items():
            if key != "id":
                setattr(workflow, key, value)
        return workflow

    def stage_delete(self, workflow_id: int) -> None:
        """暂存工作流删除，由请求级 UnitOfWork 统一提交。"""
        self._db.execute(
            sqlalchemy_delete(Workflow).where(Workflow.id == workflow_id)
        )

    async def async_get(self, wid: int) -> Optional[Workflow]:
        """
        异步查询单个工作流
        """
        return await self._execute_async_query(
            lambda session: Workflow.async_get(session, wid)
        )

    def list(self) -> List[Workflow]:
        """
        获取所有工作流列表
        """
        return self._execute_sync_query(lambda session: Workflow.list(session))

    async def async_list(
        self,
        *,
        state: Optional[str] = None,
        name: Optional[str] = None,
        trigger_type: Optional[str] = None,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> List[Workflow]:
        """按筛选条件和可选分页窗口异步获取工作流列表。"""
        statement = select(Workflow).where(
            *_workflow_conditions(
                state=state,
                name=name,
                trigger_type=trigger_type,
            )
        ).order_by(Workflow.id)
        if page is not None and count is not None:
            statement = statement.offset((page - 1) * count).limit(count)
        return await self._execute_async_query(
            lambda session: _async_workflow_rows(session, statement)
        )

    async def async_count(
        self,
        *,
        state: Optional[str] = None,
        name: Optional[str] = None,
        trigger_type: Optional[str] = None,
    ) -> int:
        """按与列表一致的筛选条件异步统计工作流数量。"""
        statement = select(func.count()).select_from(Workflow).where(
            *_workflow_conditions(
                state=state,
                name=name,
                trigger_type=trigger_type,
            )
        )
        return await self._execute_async_query(
            lambda session: _async_scalar(session, statement)
        )

    def list_enabled(self) -> List[Workflow]:
        """
        获取启用的工作流列表
        """
        return self._execute_sync_query(
            lambda session: Workflow.get_enabled_workflows(session)
        )

    def get_timer_triggered_workflows(self) -> List[Workflow]:
        """
        获取定时触发的工作流列表
        """
        return self._execute_sync_query(
            lambda session: Workflow.get_timer_triggered_workflows(session)
        )

    def get_event_triggered_workflows(self) -> List[Workflow]:
        """
        获取事件触发的工作流列表
        """
        return self._execute_sync_query(
            lambda session: Workflow.get_event_triggered_workflows(session)
        )

    def get_by_name(self, name: str) -> Workflow:
        """
        按名称获取工作流
        """
        return self._execute_sync_query(
            lambda session: Workflow.get_by_name(session, name)
        )

    async def async_get_by_name(self, name: str) -> Optional[Workflow]:
        """
        异步按名称获取工作流
        """
        return await self._execute_async_query(
            lambda session: Workflow.async_get_by_name(session, name)
        )

    async def stage_create(self, payload: Mapping[str, Any]) -> Workflow:
        """暂存新工作流，不在操作器内提交事务。"""
        workflow = Workflow(**dict(payload))
        self._db.add(workflow)
        await self._db.flush()
        return workflow

    async def stage_reset(
            self,
            workflow_id: int,
            reset_count: bool = False,
    ) -> Optional[Workflow]:
        """暂存工作流重置字段，不触发模型装饰器的隐式提交。"""
        workflow = await self.async_get(workflow_id)
        if not workflow:
            return None
        workflow.state = "W"
        workflow.result = None
        workflow.current_action = None
        workflow.context = {}
        workflow.execution_state = {}
        if reset_count:
            workflow.run_count = 0
        return workflow

    def stage_start(self, wid: int) -> bool:
        """在调用方持有的会话中暂存运行中状态。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("工作流暂存写入需要调用方提供同步 Session")
        return Workflow.start(self._db, wid)

    def stage_success(self, wid: int, result: Optional[str] = None) -> bool:
        """在调用方持有的会话中暂存成功状态。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("工作流暂存写入需要调用方提供同步 Session")
        return Workflow.success(self._db, wid, result)

    def stage_fail(self, wid: int, result: str) -> bool:
        """在调用方持有的会话中暂存失败状态。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("工作流暂存写入需要调用方提供同步 Session")
        return Workflow.fail(self._db, wid, result)

    def stage_step(
            self,
            wid: int,
            action_id: str,
            context: dict[str, Any],
            execution_state: Optional[dict[str, Any]] = None,
    ) -> bool:
        """在调用方持有的会话中暂存动作进度。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("工作流暂存写入需要调用方提供同步 Session")
        return Workflow.update_current_action(
            self._db,
            wid,
            action_id,
            context,
            execution_state
        )

    def stage_execution_reset(
            self,
            wid: int,
            reset_count: bool = False,
    ) -> bool:
        """在调用方持有的会话中暂存执行状态重置。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("工作流暂存写入需要调用方提供同步 Session")
        return Workflow.reset(self._db, wid, reset_count=reset_count)
