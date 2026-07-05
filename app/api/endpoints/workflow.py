import json
from datetime import datetime
from typing import List, Any, Optional

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app import schemas
from app.chain.workflow import WorkflowChain
from app.core.config import global_vars
from app.core.plugin import PluginManager
from app.workflow import WorkFlowManager
from app.db import get_async_db, get_db
from app.db.models import Workflow, User
from app.db.systemconfig_oper import SystemConfigOper
from app.db.user_oper import (
    get_current_active_manage_user,
    get_current_active_manage_user_async,
)
from app.db.workflow_oper import WorkflowOper
from app.helper.server import MoviePilotServerHelper
from app.scheduler import Scheduler
from app.schemas.types import EventType, EVENT_TYPE_NAMES

router = APIRouter()

WORKFLOW_TRIGGER_TIMER = "timer"
WORKFLOW_TRIGGER_EVENT = "event"
WORKFLOW_TRIGGER_MANUAL = "manual"


@router.get("/", summary="所有工作流", response_model=List[schemas.Workflow])
async def list_workflows(
    db: AsyncSession = Depends(get_async_db),
    _: User = Depends(get_current_active_manage_user_async),
) -> Any:
    """
    获取工作流列表
    """
    return await WorkflowOper(db).async_list()


@router.post("/", summary="创建工作流", response_model=schemas.Response)
async def create_workflow(
    workflow: schemas.Workflow,
    db: AsyncSession = Depends(get_async_db),
    _: User = Depends(get_current_active_manage_user_async),
) -> Any:
    """
    创建工作流
    """
    if workflow.name and await WorkflowOper(db).async_get_by_name(workflow.name):
        return schemas.Response(success=False, message="已存在相同名称的工作流")
    if not workflow.add_time:
        workflow.add_time = datetime.strftime(datetime.now(), "%Y-%m-%d %H:%M:%S")
    if not workflow.state:
        workflow.state = "P"
    if not workflow.trigger_type:
        workflow.trigger_type = "timer"
    workflow_obj = Workflow(**workflow.model_dump())
    await workflow_obj.async_create(db)
    return schemas.Response(success=True, message="创建工作流成功")


@router.get("/plugin/actions", summary="查询插件动作", response_model=List[dict])
def list_plugin_actions(
    plugin_id: str = None, _: User = Depends(get_current_active_manage_user)
) -> Any:
    """
    获取所有动作
    """
    return PluginManager().get_plugin_actions(plugin_id)


@router.get("/actions", summary="所有动作", response_model=List[dict])
async def list_actions(_: User = Depends(get_current_active_manage_user_async)) -> Any:
    """
    获取所有动作
    """
    return WorkFlowManager().list_actions()


@router.get("/event_types", summary="获取所有事件类型", response_model=List[dict])
async def get_event_types(_: User = Depends(get_current_active_manage_user_async)) -> Any:
    """
    获取所有事件类型
    """
    return [
        {
            "title": EVENT_TYPE_NAMES.get(event_type, event_type.name),
            "value": event_type.value,
        }
        for event_type in EventType
    ]


@router.post("/share", summary="分享工作流", response_model=schemas.Response)
async def workflow_share(
    workflow: schemas.WorkflowShare, _: User = Depends(get_current_active_manage_user_async)
) -> Any:
    """
    分享工作流
    """
    if not workflow.id or not workflow.share_title or not workflow.share_user:
        return schemas.Response(
            success=False, message="请填写工作流ID、分享标题和分享人"
        )

    state, errmsg = await MoviePilotServerHelper.async_workflow_share_by_id(
        workflow_id=workflow.id,
        share_title=workflow.share_title or "",
        share_comment=workflow.share_comment or "",
        share_user=workflow.share_user or "",
    )
    return schemas.Response(success=state, message=errmsg)


@router.delete("/share/{share_id}", summary="删除分享", response_model=schemas.Response)
async def workflow_share_delete(
    share_id: int, _: User = Depends(get_current_active_manage_user_async)
) -> Any:
    """
    删除分享
    """
    state, errmsg = await MoviePilotServerHelper.async_workflow_share_delete_by_id(share_id=share_id)
    return schemas.Response(success=state, message=errmsg)


@router.post("/fork", summary="复用工作流", response_model=schemas.Response)
async def workflow_fork(
    workflow: schemas.WorkflowShare,
    db: AsyncSession = Depends(get_async_db),
    _: User = Depends(get_current_active_manage_user_async),
) -> Any:
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

    try:
        event_conditions = json.loads(workflow.event_conditions or "{}") if workflow.event_conditions else {}
    except json.JSONDecodeError:
        return schemas.Response(success=False, message="event_conditions字段JSON格式错误")

    share_id = workflow.id
    # 创建工作流
    workflow_dict = {
        "name": workflow.name,
        "description": workflow.description,
        "timer": workflow.timer,
        "trigger_type": workflow.trigger_type or WORKFLOW_TRIGGER_TIMER,
        "event_type": workflow.event_type,
        "event_conditions": event_conditions,
        "actions": actions,
        "flows": flows,
        "context": context,
        "state": "P",  # 默认暂停状态
    }

    # 检查名称是否重复
    workflow_oper = WorkflowOper(db)
    if await workflow_oper.async_get_by_name(workflow_dict["name"]):
        return schemas.Response(success=False, message="已存在相同名称的工作流")

    # 创建新工作流
    workflow_obj = await Workflow(**workflow_dict).async_create(db)

    # 更新复用次数
    if workflow_obj and share_id:
        await MoviePilotServerHelper.async_workflow_fork_by_id(share_id=share_id)

    return schemas.Response(success=True, message="复用成功")


@router.get(
    "/shares", summary="查询分享的工作流", response_model=List[schemas.WorkflowShare]
)
async def workflow_shares(
    name: Optional[str] = None,
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    _: User = Depends(get_current_active_manage_user_async),
) -> Any:
    """
    查询分享的工作流
    """
    return await MoviePilotServerHelper.async_get_workflow_shares(name=name, page=page, count=count)


@router.post(
    "/{workflow_id}/run", summary="执行工作流", response_model=schemas.Response
)
def run_workflow(
    workflow_id: int,
    from_begin: Optional[bool] = True,
    _: User = Depends(get_current_active_manage_user),
) -> Any:
    """
    执行工作流
    """
    state, errmsg = WorkflowChain().process(workflow_id, from_begin=from_begin)
    if not state:
        return schemas.Response(success=False, message=errmsg)
    return schemas.Response(success=True)


@router.post(
    "/{workflow_id}/start", summary="启用工作流", response_model=schemas.Response
)
def start_workflow(
    workflow_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_active_manage_user),
) -> Any:
    """
    启用工作流
    """
    workflow = WorkflowOper(db).get(workflow_id)
    if not workflow:
        return schemas.Response(success=False, message="工作流不存在")
    trigger_type = workflow.trigger_type or WORKFLOW_TRIGGER_TIMER
    if trigger_type == WORKFLOW_TRIGGER_TIMER and not workflow.timer:
        return schemas.Response(success=False, message="定时工作流缺少定时器配置")
    if trigger_type not in {
        WORKFLOW_TRIGGER_TIMER,
        WORKFLOW_TRIGGER_EVENT,
        WORKFLOW_TRIGGER_MANUAL,
    }:
        return schemas.Response(success=False, message="工作流触发类型不支持")
    # 先更新状态，事件触发注册会重新读取工作流并跳过暂停状态。
    workflow.update_state(db, workflow_id, "W")
    if trigger_type == WORKFLOW_TRIGGER_TIMER:
        # 添加定时任务
        Scheduler().update_workflow_job(workflow)
    elif trigger_type == WORKFLOW_TRIGGER_EVENT:
        # 事件触发：添加到事件触发器
        WorkFlowManager().load_workflow_events(workflow_id)
    return schemas.Response(success=True)


@router.post(
    "/{workflow_id}/pause", summary="停用工作流", response_model=schemas.Response
)
def pause_workflow(
    workflow_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_active_manage_user),
) -> Any:
    """
    停用工作流
    """
    workflow = WorkflowOper(db).get(workflow_id)
    if not workflow:
        return schemas.Response(success=False, message="工作流不存在")
    # 根据触发类型进行不同处理
    if workflow.trigger_type == WORKFLOW_TRIGGER_TIMER:
        # 定时触发：移除定时任务
        Scheduler().remove_workflow_job(workflow)
    elif workflow.trigger_type == WORKFLOW_TRIGGER_EVENT:
        # 事件触发：从事件触发器中移除
        WorkFlowManager().remove_workflow_event(workflow_id, workflow.event_type)
    # 停止工作流
    global_vars.stop_workflow(workflow_id)
    # 更新状态
    workflow.update_state(db, workflow_id, "P")
    return schemas.Response(success=True)


@router.post(
    "/{workflow_id}/reset", summary="重置工作流", response_model=schemas.Response
)
async def reset_workflow(
    workflow_id: int,
    db: AsyncSession = Depends(get_async_db),
    _: User = Depends(get_current_active_manage_user_async),
) -> Any:
    """
    重置工作流
    """
    workflow = await WorkflowOper(db).async_get(workflow_id)
    if not workflow:
        return schemas.Response(success=False, message="工作流不存在")
    # 停止工作流
    global_vars.stop_workflow(workflow_id)
    # 重置工作流
    await Workflow.async_reset(db, workflow_id, reset_count=True)
    # 删除缓存
    SystemConfigOper().delete(f"WorkflowCache-{workflow_id}")
    return schemas.Response(success=True)


@router.get("/{workflow_id}", summary="工作流详情", response_model=schemas.Workflow)
async def get_workflow(
    workflow_id: int,
    db: AsyncSession = Depends(get_async_db),
    _: User = Depends(get_current_active_manage_user_async),
) -> Any:
    """
    获取工作流详情
    """
    return await WorkflowOper(db).async_get(workflow_id)


@router.put("/{workflow_id}", summary="更新工作流", response_model=schemas.Response)
def update_workflow(
    workflow: schemas.Workflow,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_active_manage_user),
) -> Any:
    """
    更新工作流
    """
    if not workflow.id:
        return schemas.Response(success=False, message="工作流ID不能为空")
    workflow_oper = WorkflowOper(db)
    wf = workflow_oper.get(workflow.id)
    if not wf:
        return schemas.Response(success=False, message="工作流不存在")
    if not wf.trigger_type:
        workflow.trigger_type = "timer"
    wf.update(db, workflow.model_dump())
    # 更新后的工作流对象
    updated_workflow = workflow_oper.get(workflow.id)
    scheduler = Scheduler()
    scheduler.remove_workflow_job(updated_workflow)
    if not updated_workflow.trigger_type or updated_workflow.trigger_type == WORKFLOW_TRIGGER_TIMER:
        if updated_workflow.timer:
            scheduler.update_workflow_job(updated_workflow)
    # 更新事件注册
    WorkFlowManager().update_workflow_event(updated_workflow)
    return schemas.Response(success=True, message="更新成功")


@router.delete("/{workflow_id}", summary="删除工作流", response_model=schemas.Response)
def delete_workflow(
    workflow_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_active_manage_user),
) -> Any:
    """
    删除工作流
    """
    workflow = WorkflowOper(db).get(workflow_id)
    if not workflow:
        return schemas.Response(success=False, message="工作流不存在")
    if not workflow.trigger_type or workflow.trigger_type == WORKFLOW_TRIGGER_TIMER:
        # 定时触发：删除定时任务
        Scheduler().remove_workflow_job(workflow)
    elif workflow.trigger_type == WORKFLOW_TRIGGER_EVENT:
        # 事件触发：从事件触发器中移除
        WorkFlowManager().remove_workflow_event(workflow_id, workflow.event_type)
    # 删除工作流
    Workflow.delete(db, workflow_id)
    # 删除缓存
    SystemConfigOper().delete(f"WorkflowCache-{workflow_id}")
    return schemas.Response(success=True, message="删除成功")
