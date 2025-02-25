from datetime import datetime
from typing import List, Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import schemas
from app.db import get_db
from app.db.models.workflow import Workflow
from app.db.user_oper import get_current_active_user
from app.chain.workflow import WorkflowChain

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
    Workflow.update(db, workflow)
    return schemas.Response(success=True, message="更新成功")


@router.delete("/{workflow_id}", summary="删除工作流", response_model=schemas.Response)
def delete_workflow(workflow_id: int,
                    db: Session = Depends(get_db),
                    _: schemas.TokenPayload = Depends(get_current_active_user)) -> Any:
    """
    删除工作流
    """
    Workflow.delete(db, workflow_id)
    return schemas.Response(success=True, message="删除成功")


@router.get("/run/{workfow_id}", summary="执行工作流", response_model=schemas.Response)
def run_workflow(workfow_id: int,
                 from_begin: bool = True,
                 _: schemas.TokenPayload = Depends(get_current_active_user)) -> Any:
    """
    执行工作流
    """
    if WorkflowChain().process(workfow_id, from_begin=from_begin):
        return schemas.Response(success=True, message="执行成功")
    return schemas.Response(success=False, message="执行失败")
