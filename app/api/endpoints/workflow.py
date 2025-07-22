import json
from datetime import datetime
from typing import List, Any, Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import schemas
from app.chain.workflow import WorkflowChain
from app.core.config import global_vars
from app.core.plugin import PluginManager
from app.core.workflow import WorkFlowManager
from app.db import get_db
from app.db.models import Workflow
from app.db.systemconfig_oper import SystemConfigOper
from app.db.user_oper import get_current_active_user
from app.db.workflow_oper import WorkflowOper
from app.helper.workflow import WorkflowHelper
from app.scheduler import Scheduler
from app.schemas.types import EventType

router = APIRouter()


@router.get("/", summary="所有工作流", response_model=List[schemas.Workflow])
def list_workflows(db: Session = Depends(get_db),
                   _: schemas.TokenPayload = Depends(get_current_active_user)) -> Any:
    """
    获取工作流列表
    """
    from app.db.workflow_oper import WorkflowOper
    return WorkflowOper(db).list()


@router.post("/", summary="创建工作流", response_model=schemas.Response)
def create_workflow(workflow: schemas.Workflow,
                    db: Session = Depends(get_db),
                    _: schemas.TokenPayload = Depends(get_current_active_user)) -> Any:
    """
    创建工作流
    """
    from app.db.workflow_oper import WorkflowOper
    if workflow.name and WorkflowOper(db).get_by_name(workflow.name):
        return schemas.Response(success=False, message="已存在相同名称的工作流")
    if not workflow.add_time:
        workflow.add_time = datetime.strftime(datetime.now(), "%Y-%m-%d %H:%M:%S")
    if not workflow.state:
        workflow.state = "P"
    if not workflow.trigger_type:
        workflow.trigger_type = "timer"
    Workflow(**workflow.dict()).create(db)
    return schemas.Response(success=True, message="创建工作流成功")


@router.get("/plugin/actions", summary="查询插件动作", response_model=List[dict])
def list_plugin_actions(plugin_id: str = None, _: schemas.TokenPayload = Depends(get_current_active_user)) -> Any:
    """
    获取所有动作
    """
    return PluginManager().get_plugin_actions(plugin_id)


@router.get("/actions", summary="所有动作", response_model=List[dict])
def list_actions(_: schemas.TokenPayload = Depends(get_current_active_user)) -> Any:
    """
    获取所有动作
    """
    return WorkFlowManager().list_actions()


@router.get("/event_types", summary="获取所有事件类型", response_model=List[dict])
def get_event_types(_: schemas.TokenPayload = Depends(get_current_active_user)) -> Any:
    """
    获取所有事件类型
    """
    return [{
        "title": event_type.name,
        "value": event_type.value
    } for event_type in EventType]


@router.post("/share", summary="分享工作流", response_model=schemas.Response)
def workflow_share(
        workflow: schemas.WorkflowShare,
        _: schemas.TokenPayload = Depends(get_current_active_user)) -> Any:
    """
    分享工作流
    """
    if not workflow.id or not workflow.share_title or not workflow.share_user:
        return schemas.Response(success=False, message="请填写工作流ID、分享标题和分享人")

    state, errmsg = WorkflowHelper().workflow_share(workflow_id=workflow.id,
                                                    share_title=workflow.share_title or "",
                                                    share_comment=workflow.share_comment or "",
                                                    share_user=workflow.share_user or "")
    return schemas.Response(success=state, message=errmsg)


@router.delete("/share/{share_id}", summary="删除分享", response_model=schemas.Response)
def workflow_share_delete(
        share_id: int,
        _: schemas.TokenPayload = Depends(get_current_active_user)) -> Any:
    """
    删除分享
    """
    state, errmsg = WorkflowHelper().share_delete(share_id=share_id)
    return schemas.Response(success=state, message=errmsg)


@router.post("/fork", summary="复用工作流", response_model=schemas.Response)
def workflow_fork(
        workflow: schemas.WorkflowShare,
        db: Session = Depends(get_db),
        _: schemas.User = Depends(get_current_active_user)) -> Any:
    """
    复用工作流
    """
    if not workflow.name:
        return schemas.Response(success=False, message="工作流名称不能为空")

    # 解析JSON数据，添加错误处理
    try:
        actions = json.loads(workflow.actions or "[]")
    except json.JSONDecodeError:
        return schemas.Response(success=False, message="actions字段JSON格式错误")

    try:
        flows = json.loads(workflow.flows or "[]")
    except json.JSONDecodeError:
        return schemas.Response(success=False, message="flows字段JSON格式错误")

    try:
        context = json.loads(workflow.context or "{}")
    except json.JSONDecodeError:
        return schemas.Response(success=False, message="context字段JSON格式错误")

    # 创建工作流
    workflow_dict = {
        "name": workflow.name,
        "description": workflow.description,
        "timer": workflow.timer,
        "trigger_type": workflow.trigger_type or "timer",
        "event_type": workflow.event_type,
        "event_conditions": json.loads(workflow.event_conditions or "{}") if workflow.event_conditions else {},
        "actions": actions,
        "flows": flows,
        "context": context,
        "state": "P"  # 默认暂停状态
    }

    # 检查名称是否重复
    if Workflow.get_by_name(db, workflow_dict["name"]):
        return schemas.Response(success=False, message="已存在相同名称的工作流")

    # 创建新工作流
    workflow = Workflow(**workflow_dict)
    workflow.create(db)

    # 更新复用次数
    if workflow.id:
        WorkflowHelper().workflow_fork(share_id=workflow.id)

    return schemas.Response(success=True, message="复用成功")


@router.get("/shares", summary="查询分享的工作流", response_model=List[schemas.WorkflowShare])
def workflow_shares(
        name: Optional[str] = None,
        page: Optional[int] = 1,
        count: Optional[int] = 30,
        _: schemas.TokenPayload = Depends(get_current_active_user)) -> Any:
    """
    查询分享的工作流
    """
    return WorkflowHelper().get_shares(name=name, page=page, count=count)


@router.post("/{workflow_id}/run", summary="执行工作流", response_model=schemas.Response)
def run_workflow(workflow_id: int,
                 from_begin: Optional[bool] = True,
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
    from app.db.workflow_oper import WorkflowOper
    workflow = WorkflowOper(db).get(workflow_id)
    if not workflow:
        return schemas.Response(success=False, message="工作流不存在")
    if not workflow.trigger_type or workflow.trigger_type == "timer":
        # 添加定时任务
        Scheduler().update_workflow_job(workflow)
    else:
        # 事件触发：添加到事件触发器
        WorkFlowManager().load_workflow_events(workflow_id)
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
    from app.db.workflow_oper import WorkflowOper
    workflow = WorkflowOper(db).get(workflow_id)
    if not workflow:
        return schemas.Response(success=False, message="工作流不存在")
    # 根据触发类型进行不同处理
    if workflow.trigger_type == "timer":
        # 定时触发：移除定时任务
        Scheduler().remove_workflow_job(workflow)
    elif workflow.trigger_type == "event":
        # 事件触发：从事件触发器中移除
        WorkFlowManager().remove_workflow_event(workflow_id, workflow.event_type)
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
    from app.db.workflow_oper import WorkflowOper
    workflow = WorkflowOper(db).get(workflow_id)
    if not workflow:
        return schemas.Response(success=False, message="工作流不存在")
    # 停止工作流
    global_vars.stop_workflow(workflow_id)
    # 重置工作流
    workflow.reset(db, workflow_id, reset_count=True)
    # 删除缓存
    SystemConfigOper().delete(f"WorkflowCache-{workflow_id}")
    return schemas.Response(success=True)


@router.get("/{workflow_id}", summary="工作流详情", response_model=schemas.Workflow)
def get_workflow(workflow_id: int,
                 db: Session = Depends(get_db),
                 _: schemas.TokenPayload = Depends(get_current_active_user)) -> Any:
    """
    获取工作流详情
    """
    from app.db.workflow_oper import WorkflowOper
    return WorkflowOper(db).get(workflow_id)


@router.put("/{workflow_id}", summary="更新工作流", response_model=schemas.Response)
def update_workflow(workflow: schemas.Workflow,
                    db: Session = Depends(get_db),
                    _: schemas.TokenPayload = Depends(get_current_active_user)) -> Any:
    """
    更新工作流
    """
    if not workflow.id:
        return schemas.Response(success=False, message="工作流ID不能为空")
    wf = WorkflowOper(db).get(workflow.id)
    if not wf:
        return schemas.Response(success=False, message="工作流不存在")
    if not wf.trigger_type:
        workflow.trigger_type = "timer"
    wf.update(db, workflow.dict())
    return schemas.Response(success=True, message="更新成功")


@router.delete("/{workflow_id}", summary="删除工作流", response_model=schemas.Response)
def delete_workflow(workflow_id: int,
                    db: Session = Depends(get_db),
                    _: schemas.TokenPayload = Depends(get_current_active_user)) -> Any:
    """
    删除工作流
    """
    workflow = WorkflowOper(db).get(workflow_id)
    if not workflow:
        return schemas.Response(success=False, message="工作流不存在")
    if not workflow.trigger_type or workflow.trigger_type == "timer":
        # 定时触发：删除定时任务
        Scheduler().remove_workflow_job(workflow)
    else:
        # 事件触发：从事件触发器中移除
        WorkFlowManager().register_workflow_event(workflow_id, workflow.event_type)
    # 删除工作流
    Workflow.delete(db, workflow_id)
    # 删除缓存
    SystemConfigOper().delete(f"WorkflowCache-{workflow_id}")
    return schemas.Response(success=True, message="删除成功")
