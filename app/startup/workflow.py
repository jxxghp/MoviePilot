"""工作流执行状态事务适配器。"""

from collections.abc import Callable
from typing import Any, TypeVar

from sqlalchemy.orm import Session

from app.application.workflow import WorkflowExecutionCommand
from app.db.oper.workflow import WorkflowOper
from app.db.uow import SqlAlchemyUnitOfWork


_Result = TypeVar("_Result")


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
