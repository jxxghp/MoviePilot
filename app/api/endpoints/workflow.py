from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import schemas
from app.db import get_db
from app.db.models.workflow import Workflow
from app.db.user_oper import get_current_active_superuser, get_current_active_user
from chain.workflow import WorkflowChain

router = APIRouter()


@router.get("/", summary="所有工作流", response_model=List[schemas.Workflow])
def list_workflows(db: Session = Depends(get_db),
                   _: schemas.TokenPayload = Depends(get_current_active_user)) -> List[dict]:
    """
    获取工作流列表
    """
    return Workflow.list(db)


@router.post("/", summary="创建工作流", response_model=schemas.Workflow)
def create_workflow(workflow: schemas.Workflow,
                    db: Session = Depends(get_db),
                    _: schemas.TokenPayload = Depends(get_current_active_superuser)) -> dict:
    """
    创建工作流
    """
    return Workflow.create(db, workflow)


@router.get("/{workflow_id}", summary="工作流详情", response_model=schemas.Workflow)
def get_workflow(workflow_id: int,
                 db: Session = Depends(get_db),
                 _: schemas.TokenPayload = Depends(get_current_active_user)) -> dict:
    """
    获取工作流详情
    """
    return Workflow.get(db, workflow_id)


@router.put("/{workflow_id}", summary="更新工作流", response_model=schemas.Workflow)
def update_workflow(workflow: schemas.Workflow,
                    db: Session = Depends(get_db),
                    _: schemas.TokenPayload = Depends(get_current_active_superuser)) -> dict:
    """
    更新工作流
    """
    return Workflow.update(db, workflow)


@router.delete("/{workflow_id}", summary="删除工作流", response_model=schemas.Workflow)
def delete_workflow(workflow_id: int,
                    db: Session = Depends(get_db),
                    _: schemas.TokenPayload = Depends(get_current_active_superuser)) -> dict:
    """
    删除工作流
    """
    return Workflow.delete(db, workflow_id)


@router.get("/run/{workfow_id}", summary="执行工作流", response_model=schemas.Workflow)
def run_workflow(workfow_id: int,
                 from_begin: bool = True,
                 _: schemas.TokenPayload = Depends(get_current_active_user)) -> dict:
    """
    执行工作流
    """
    return WorkflowChain().process(workfow_id, from_begin=from_begin)
