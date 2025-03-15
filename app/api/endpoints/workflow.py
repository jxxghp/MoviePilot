from datetime import datetime
from typing import List, Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import schemas
from app.core.config import global_vars
from app.core.workflow import WorkFlowManager
from app.db import get_db
from app.db.models.workflow import Workflow
from app.db.systemconfig_oper import SystemConfigOper
from app.db.user_oper import get_current_active_user
from app.chain.workflow import WorkflowChain
from app.scheduler import Scheduler

router = APIRouter()


@router.get("/", summary="所有工作流", response_model=List[schemas.Workflow])
def list_workflows(db: Session = Depends(get_db),
                   _: schemas.TokenPayload = Depends(get_current_active_user)) -> Any:
    """
    获取工作流列表
    """
    return Workflow.list(db)


@router.post("/", summary="创建工作流", response_model=schemas.Response)
def create_workflow(workflow: schemas.Workflow,
                    db: Session = Depends(get_db),
                    _: schemas.TokenPayload = Depends(get_current_active_user)) -> Any:
    """
    创建工作流
    """
    if Workflow.get_by_name(db, workflow.name):
        return schemas.Response(success=False, message="已存在相同名称的工作流")
    if not workflow.add_time:
        workflow.add_time = datetime.strftime(datetime.now(), "%Y-%m-%d %H:%M:%S")
    if not workflow.state:
        workflow.state = "P"
    Workflow(**workflow.dict()).create(db)
    return schemas.Response(success=True, message="创建工作流成功")


@router.get("/actions", summary="所有动作", response_model=List[dict])
def list_actions(_: schemas.TokenPayload = Depends(get_current_active_user)) -> Any:
    """
    获取所有动作
    """
    return WorkFlowManager().list_actions()


@router.get("/{workflow_id}", summary="工作流详情", response_model=schemas.Workflow)
def get_workflow(workflow_id: int,
                 db: Session = Depends(get_db),
                 _: schemas.TokenPayload = Depends(get_current_active_user)) -> Any:
    """
    获取工作流详情
    """
    return Workflow.get(db, workflow_id)


@router.put("/{workflow_id}", summary="更新工作流", response_model=schemas.Response)
def update_workflow(workflow: schemas.Workflow,
                    db: Session = Depends(get_db),
                    _: schemas.TokenPayload = Depends(get_current_active_user)) -> Any:
    """
    更新工作流
    """
    wf = Workflow.get(db, workflow.id)
    if not wf:
        return schemas.Response(success=False, message="工作流不存在")
    wf.update(db, workflow.dict())
    return schemas.Response(success=True, message="更新成功")


@router.delete("/{workflow_id}", summary="删除工作流", response_model=schemas.Response)
def delete_workflow(workflow_id: int,
                    db: Session = Depends(get_db),
                    _: schemas.TokenPayload = Depends(get_current_active_user)) -> Any:
    """
    删除工作流
    """
    workflow = Workflow.get(db, workflow_id)
    if not workflow:
        return schemas.Response(success=False, message="工作流不存在")
    # 删除定时任务
    Scheduler().remove_workflow_job(workflow)
    # 删除工作流
    Workflow.delete(db, workflow_id)
    # 删除缓存
    SystemConfigOper().delete(f"WorkflowCache-{workflow_id}")
    return schemas.Response(success=True, message="删除成功")


@router.post("/{workflow_id}/run", summary="执行工作流", response_model=schemas.Response)
def run_workflow(workflow_id: int,
                 from_begin: bool = True,
                 _: schemas.TokenPayload = Depends(get_current_active_user)) -> Any:
    """
    执行工作流
    """
    state, errmsg = WorkflowChain().process(workflow_id, from_begin=from_begin)
    if not state:
        return schemas.Response(success=False, message=errmsg)
    return schemas.Response(success=True)


@router.post("/{workflow_id}/start", summary="启用工作流", response_model=schemas.Response)
def start_workflow(workflow_id: int,
                   db: Session = Depends(get_db),
                   _: schemas.TokenPayload = Depends(get_current_active_user)) -> Any:
    """
    启用工作流
    """
    workflow = Workflow.get(db, workflow_id)
    if not workflow:
        return schemas.Response(success=False, message="工作流不存在")
    # 添加定时任务
    Scheduler().update_workflow_job(workflow)
    # 更新状态
    workflow.update_state(db, workflow_id, "W")
    return schemas.Response(success=True)


@router.post("/{workflow_id}/pause", summary="停用工作流", response_model=schemas.Response)
def pause_workflow(workflow_id: int,
                   db: Session = Depends(get_db),
                   _: schemas.TokenPayload = Depends(get_current_active_user)) -> Any:
    """
    停用工作流
    """
    workflow = Workflow.get(db, workflow_id)
    if not workflow:
        return schemas.Response(success=False, message="工作流不存在")
    # 删除定时任务
    Scheduler().remove_workflow_job(workflow)
    # 停止工作流
    global_vars.stop_workflow(workflow_id)
    # 更新状态
    workflow.update_state(db, workflow_id, "P")
    return schemas.Response(success=True)


@router.post("/{workflow_id}/reset", summary="重置工作流", response_model=schemas.Response)
def reset_workflow(workflow_id: int,
                   db: Session = Depends(get_db),
                   _: schemas.TokenPayload = Depends(get_current_active_user)) -> Any:
    """
    重置工作流
    """
    workflow = Workflow.get(db, workflow_id)
    if not workflow:
        return schemas.Response(success=False, message="工作流不存在")
    # 停止工作流
    global_vars.stop_workflow(workflow_id)
    # 重置工作流
    workflow.reset(db, workflow_id, reset_count=True)
    # 删除缓存
    SystemConfigOper().delete(f"WorkflowCache-{workflow_id}")
    return schemas.Response(success=True)
