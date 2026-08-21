"""工作流领域的请求级 command/query 依赖。"""

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.adapters.external.server import MoviePilotServerHelper
from app.api.data import get_async_db, get_db
from app.api.dependencies.data import repository, standalone_repository, transaction
from app.application.scheduling import Scheduler
from app.application.workflow import (
    WorkflowDefinitionCommand,
    WorkflowMutationCommand,
    WorkflowQueryService,
)
from app.runtime.config import global_vars
from app.workflow import WorkFlowManager


def get_workflow_mutation_command(
    db: Session = Depends(get_db),
) -> WorkflowMutationCommand:
    """组装请求级工作流写用例和提交后的调度副作用。"""
    scheduler = Scheduler()
    workflow_manager = WorkFlowManager()
    return WorkflowMutationCommand(
        repository=repository("workflow", db),
        unit_of_work=transaction("sync", db),
        add_timer=scheduler.update_workflow_job,
        remove_timer=scheduler.remove_workflow_job,
        load_event=workflow_manager.load_workflow_events,
        remove_event=workflow_manager.remove_workflow_event,
        refresh_event=workflow_manager.update_workflow_event,
        stop_running=global_vars.stop_workflow,
        delete_cache=lambda workflow_id: standalone_repository(
            "system_config"
        ).delete(f"WorkflowCache-{workflow_id}"),
    )


def get_workflow_definition_command(
    db: AsyncSession = Depends(get_async_db),
) -> WorkflowDefinitionCommand:
    """组装工作流创建、复用和重置的异步写用例。"""
    return WorkflowDefinitionCommand(
        repository=repository("workflow", db),
        unit_of_work=transaction("async", db),
        stop_running=global_vars.stop_workflow,
        delete_cache=lambda workflow_id: standalone_repository(
            "system_config"
        ).delete(f"WorkflowCache-{workflow_id}"),
        report_fork=MoviePilotServerHelper.async_workflow_fork_by_id,
    )


def get_workflow_query_service(
    db: AsyncSession = Depends(get_async_db),
) -> WorkflowQueryService:
    """组装工作流只读查询用例，避免端点直接持有数据库操作器。"""
    return WorkflowQueryService(repository=repository("workflow", db))
