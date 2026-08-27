"""保留旧工作流 Oper 的无 Session 执行状态写入契约。"""

from typing import Any, Optional

from app.application.workflow import get_configured_workflow_execution
from app.db.oper.workflow import WorkflowOper as CanonicalWorkflowOper


class WorkflowOper(CanonicalWorkflowOper):
    """继承显式 Session 查询，并保留旧执行状态写入方法。"""

    def start(self, wid: int) -> bool:
        """按旧签名提交工作流运行中状态。"""
        if self._db is None:
            return get_configured_workflow_execution().start(wid)
        return self.stage_start(wid)

    def success(self, wid: int, result: Optional[str] = None) -> bool:
        """按旧签名提交工作流成功状态。"""
        if self._db is None:
            return get_configured_workflow_execution().success(wid, result)
        return self.stage_success(wid, result)

    def fail(self, wid: int, result: str) -> bool:
        """按旧签名提交工作流失败状态。"""
        if self._db is None:
            return get_configured_workflow_execution().fail(wid, result)
        return self.stage_fail(wid, result)

    def step(
            self,
            wid: int,
            action_id: str,
            context: dict[str, Any],
            execution_state: Optional[dict[str, Any]] = None,
    ) -> bool:
        """按旧签名提交工作流动作进度。"""
        if self._db is None:
            return get_configured_workflow_execution().step(
                wid,
                action_id,
                context,
                execution_state,
            )
        return self.stage_step(wid, action_id, context, execution_state)

    def reset(self, wid: int, reset_count: bool = False) -> bool:
        """按旧签名提交工作流执行状态重置。"""
        if self._db is None:
            return get_configured_workflow_execution().reset(wid, reset_count)
        return self.stage_execution_reset(wid, reset_count)


__all__ = ["WorkflowOper"]
