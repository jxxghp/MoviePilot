from typing import List, Tuple

from app.db import DbOper
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

    def get(self, wid: int) -> Workflow:
        """
        查询单个工作流
        """
        return Workflow.get(self._db, wid)

    def list_enabled(self) -> List[Workflow]:
        """
        获取启用的工作流列表
        """
        return Workflow.get_enabled_workflows(self._db)

    def get_by_name(self, name: str) -> Workflow:
        """
        按名称获取工作流
        """
        return Workflow.get_by_name(self._db, name)

    def start(self, wid: int) -> bool:
        """
        启动
        """
        return Workflow.start(self._db, wid)

    def success(self, wid: int, result: str = None) -> bool:
        """
        成功
        """
        return Workflow.success(self._db, wid, result)

    def fail(self, wid: int, result: str) -> bool:
        """
        失败
        """
        return Workflow.fail(self._db, wid, result)

    def step(self, wid: int, action_id: str, context: dict) -> bool:
        """
        步进
        """
        return Workflow.update_current_action(self._db, wid, action_id, context)

    def reset(self, wid: int) -> bool:
        """
        重置
        """
        return Workflow.reset(self._db, wid)
