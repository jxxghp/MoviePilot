from typing import List, Mapping, Tuple, Optional, Any

from sqlalchemy import delete as sqlalchemy_delete

from app.db.base import DbOper
from app.db.models.workflow import Workflow


class WorkflowOper(DbOper):
    """
    工作流管理
    """

    def add(self, **kwargs) -> Tuple[bool, str]:
        """
        新增工作流
        """
        wf = Workflow(**kwargs)
        if not wf.get_by_name(self._db, kwargs.get("name")):
            wf.create(self._db)
            return True, "新增工作流成功"
        return False, "工作流已存在"

    def get(self, wid: int) -> Optional[Workflow]:
        """
        查询单个工作流
        """
        return Workflow.get(self._db, wid)

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
        return await Workflow.async_get(self._db, wid)

    def list(self) -> List[Workflow]:
        """
        获取所有工作流列表
        """
        return Workflow.list(self._db)

    async def async_list(self) -> List[Workflow]:
        """
        异步获取所有工作流列表
        """
        return await Workflow.async_list(self._db)

    def list_enabled(self) -> List[Workflow]:
        """
        获取启用的工作流列表
        """
        return Workflow.get_enabled_workflows(self._db)

    def get_timer_triggered_workflows(self) -> List[Workflow]:
        """
        获取定时触发的工作流列表
        """
        return Workflow.get_timer_triggered_workflows(self._db)

    def get_event_triggered_workflows(self) -> List[Workflow]:
        """
        获取事件触发的工作流列表
        """
        return Workflow.get_event_triggered_workflows(self._db)

    def get_by_name(self, name: str) -> Workflow:
        """
        按名称获取工作流
        """
        return Workflow.get_by_name(self._db, name)

    async def async_get_by_name(self, name: str) -> Optional[Workflow]:
        """
        异步按名称获取工作流
        """
        return await Workflow.async_get_by_name(self._db, name)

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
        return Workflow.start(self._db, wid)

    def success(self, wid: int, result: Optional[str] = None) -> bool:
        """
        成功
        """
        return Workflow.success(self._db, wid, result)

    def fail(self, wid: int, result: str) -> bool:
        """
        失败
        """
        return Workflow.fail(self._db, wid, result)

    def step(self, wid: int, action_id: str, context: dict, execution_state: Optional[dict] = None) -> bool:
        """
        步进
        """
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
        return Workflow.reset(self._db, wid, reset_count=reset_count)
