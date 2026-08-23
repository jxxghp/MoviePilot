from typing import List, Mapping, Tuple, Optional, Any, Protocol

from sqlalchemy import delete as sqlalchemy_delete
from sqlalchemy.orm import Session

from app.db.base import DbOper
from app.db.models.workflow import Workflow


class WorkflowLegacyWriter(Protocol):
    """无显式 Session 的旧 Oper 写入口所需事务服务。"""

    def start(self, workflow_id: int) -> bool:
        """提交工作流运行中状态。"""
        ...

    def success(
            self,
            workflow_id: int,
            result: Optional[str] = None,
    ) -> bool:
        """提交工作流成功状态。"""
        ...

    def fail(self, workflow_id: int, result: str) -> bool:
        """提交工作流失败状态。"""
        ...

    def step(
            self,
            workflow_id: int,
            action_id: str,
            context: dict[str, Any],
            execution_state: Optional[dict[str, Any]] = None,
    ) -> bool:
        """提交工作流动作进度。"""
        ...

    def reset(self, workflow_id: int, reset_count: bool = False) -> bool:
        """提交工作流执行状态重置。"""
        ...


_legacy_writer: Optional[WorkflowLegacyWriter] = None


def configure_workflow_legacy_writer(writer: WorkflowLegacyWriter) -> None:
    """由启动组合根为旧的无 Session Oper 写入口注入事务服务。"""
    global _legacy_writer
    _legacy_writer = writer


def _get_workflow_legacy_writer() -> WorkflowLegacyWriter:
    """返回已装配的兼容事务服务，避免 Oper 自行创建会话。"""
    if _legacy_writer is None:
        raise RuntimeError("工作流兼容写服务尚未配置")
    return _legacy_writer


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

    async def async_list(self) -> List[Workflow]:
        """
        异步获取所有工作流列表
        """
        return await self._execute_async_query(
            lambda session: Workflow.async_list(session)
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

    def start(self, wid: int) -> bool:
        """
        启动
        """
        if self._db is None:
            return _get_workflow_legacy_writer().start(wid)
        return self.stage_start(wid)

    def stage_start(self, wid: int) -> bool:
        """在调用方持有的会话中暂存运行中状态。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("工作流暂存写入需要调用方提供同步 Session")
        return Workflow.start(self._db, wid)

    def success(self, wid: int, result: Optional[str] = None) -> bool:
        """
        成功
        """
        if self._db is None:
            return _get_workflow_legacy_writer().success(wid, result)
        return self.stage_success(wid, result)

    def stage_success(self, wid: int, result: Optional[str] = None) -> bool:
        """在调用方持有的会话中暂存成功状态。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("工作流暂存写入需要调用方提供同步 Session")
        return Workflow.success(self._db, wid, result)

    def fail(self, wid: int, result: str) -> bool:
        """
        失败
        """
        if self._db is None:
            return _get_workflow_legacy_writer().fail(wid, result)
        return self.stage_fail(wid, result)

    def stage_fail(self, wid: int, result: str) -> bool:
        """在调用方持有的会话中暂存失败状态。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("工作流暂存写入需要调用方提供同步 Session")
        return Workflow.fail(self._db, wid, result)

    def step(
            self,
            wid: int,
            action_id: str,
            context: dict[str, Any],
            execution_state: Optional[dict[str, Any]] = None,
    ) -> bool:
        """
        步进
        """
        if self._db is None:
            return _get_workflow_legacy_writer().step(
                wid,
                action_id,
                context,
                execution_state,
            )
        return self.stage_step(wid, action_id, context, execution_state)

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

    def reset(self, wid: int, reset_count: bool = False) -> bool:
        """
        重置
        """
        if self._db is None:
            return _get_workflow_legacy_writer().reset(wid, reset_count)
        return self.stage_execution_reset(wid, reset_count)

    def stage_execution_reset(
            self,
            wid: int,
            reset_count: bool = False,
    ) -> bool:
        """在调用方持有的会话中暂存执行状态重置。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("工作流暂存写入需要调用方提供同步 Session")
        return Workflow.reset(self._db, wid, reset_count=reset_count)
