from typing import Any, List, Literal, Optional

from fastapi import Depends

from app.adapters.external.server import MoviePilotServerHelper
from app.api.dependencies.auth import (
    get_current_active_manage_user,
    get_current_active_manage_user_async,
)
from app.api.dependencies.workflow import (
    get_workflow_definition_command,
    get_workflow_mutation_command,
    get_workflow_query_service,
)
from app.api.response import ResponseAPIRouter
from app.application.plugin.runtime import get_plugin_manager
from app.application.workflow import (
    WorkflowDefinitionCommand,
    WorkflowMutationCommand,
    WorkflowQueryService,
    get_workflow_manager,
)
from app.chain.workflow import WorkflowChain
from app.schemas.common import JsonObject as _SchemaJsonObject
from app.schemas.response import Response as _SchemaResponse
from app.schemas.types import EVENT_TYPE_NAMES, EventType
from app.schemas.workflow import NameValueOption as _SchemaNameValueOption
from app.schemas.workflow import PluginWorkflowActionGroup as _SchemaPluginWorkflowActionGroup
from app.schemas.workflow import Workflow as _SchemaWorkflow
from app.schemas.workflow import WorkflowActionDefinition as _SchemaWorkflowActionDefinition
from app.schemas.workflow import WorkflowShare as _SchemaWorkflowShare

router = ResponseAPIRouter()

@router.get("/", summary="所有工作流", response_model=List[_SchemaWorkflow])
async def list_workflows(
    query: WorkflowQueryService = Depends(get_workflow_query_service),
    _: Any = Depends(get_current_active_manage_user_async),
) -> Any:
    """
    获取工作流列表
    """
    return await query.list()


@router.get(
    "/agent",
    summary="查询 Agent 可用工作流",
    response_model=List[_SchemaJsonObject],
)
async def list_agent_workflows(
    state: Literal["W", "R", "P", "S", "F", "all"] = "all",
    name: Optional[str] = None,
    trigger_type: Literal["timer", "event", "manual", "all"] = "all",
    query: WorkflowQueryService = Depends(get_workflow_query_service),
    _: Any = Depends(get_current_active_manage_user_async),
) -> List[dict[str, Any]]:
    """按旧 Agent 过滤与字段投影返回工作流列表，避免输出完整动作上下文。"""
    workflows = await query.list()
    results = []
    for workflow in workflows:
        if state != "all" and workflow.state != state:
            continue
        normalized_trigger = workflow.trigger_type or "timer"
        if trigger_type != "all" and normalized_trigger != trigger_type:
            continue
        if name and name.lower() not in (workflow.name or "").lower():
            continue
        results.append(
            {
                "id": workflow.id,
                "name": workflow.name,
                "description": workflow.description,
                "trigger_type": normalized_trigger,
                "state": workflow.state,
                "run_count": workflow.run_count,
                "timer": workflow.timer,
                "event_type": workflow.event_type,
                "add_time": workflow.add_time,
                "last_time": workflow.last_time,
                "current_action": workflow.current_action,
            }
        )
    return results


@router.post("/", summary="创建工作流", response_model=_SchemaResponse[None])
async def create_workflow(
    workflow: _SchemaWorkflow,
    command: WorkflowDefinitionCommand = Depends(get_workflow_definition_command),
    _: Any = Depends(get_current_active_manage_user_async),
) -> Any:
    """
    创建工作流
    """
    result = await command.create(workflow.model_dump(exclude={"id"}))
    return _SchemaResponse(success=result.success, message=result.message)


@router.get(
    "/plugin/actions",
    summary="查询插件动作",
    response_model=List[_SchemaPluginWorkflowActionGroup],
)
def list_plugin_actions(
    plugin_id: str = None, _: Any = Depends(get_current_active_manage_user)
) -> Any:
    """
    获取所有动作
    """
    return get_plugin_manager().get_plugin_actions(plugin_id)


@router.get(
    "/actions",
    summary="所有动作",
    response_model=List[_SchemaWorkflowActionDefinition],
)
async def list_actions(_: Any = Depends(get_current_active_manage_user_async)) -> Any:
    """
    获取所有动作
    """
    return get_workflow_manager().list_actions()


@router.get(
    "/event_types",
    summary="获取所有事件类型",
    response_model=List[_SchemaNameValueOption],
)
async def get_event_types(_: Any = Depends(get_current_active_manage_user_async)) -> Any:
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


@router.post("/share", summary="分享工作流", response_model=_SchemaResponse[None])
async def workflow_share(
    workflow: _SchemaWorkflowShare, _: Any = Depends(get_current_active_manage_user_async)
) -> Any:
    """
    分享工作流
    """
    if not workflow.id or not workflow.share_title or not workflow.share_user:
        return _SchemaResponse(
            success=False, message="请填写工作流ID、分享标题和分享人"
        )

    state, errmsg = await MoviePilotServerHelper.async_workflow_share_by_id(
        workflow_id=workflow.id,
        share_title=workflow.share_title or "",
        share_comment=workflow.share_comment or "",
        share_user=workflow.share_user or "",
    )
    return _SchemaResponse(success=state, message=errmsg)


@router.delete("/share/{share_id}", summary="删除分享", response_model=_SchemaResponse[None])
async def workflow_share_delete(
    share_id: int, _: Any = Depends(get_current_active_manage_user_async)
) -> Any:
    """
    删除分享
    """
    state, errmsg = await MoviePilotServerHelper.async_workflow_share_delete_by_id(share_id=share_id)
    return _SchemaResponse(success=state, message=errmsg)


@router.post("/fork", summary="复用工作流", response_model=_SchemaResponse[None])
async def workflow_fork(
    workflow: _SchemaWorkflowShare,
    command: WorkflowDefinitionCommand = Depends(get_workflow_definition_command),
    _: Any = Depends(get_current_active_manage_user_async),
) -> Any:
    """
    复用工作流
    """
    result = await command.fork(workflow.model_dump(), share_id=workflow.id)
    return _SchemaResponse(success=result.success, message=result.message)


@router.get(
    "/shares", summary="查询分享的工作流", response_model=List[_SchemaWorkflowShare]
)
async def workflow_shares(
    name: Optional[str] = None,
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    _: Any = Depends(get_current_active_manage_user_async),
) -> Any:
    """
    查询分享的工作流
    """
    return await MoviePilotServerHelper.async_get_workflow_shares(name=name, page=page, count=count)


@router.post(
    "/{workflow_id}/run", summary="执行工作流", response_model=_SchemaResponse[None]
)
def run_workflow(
    workflow_id: int,
    from_begin: Optional[bool] = True,
    _: Any = Depends(get_current_active_manage_user),
) -> Any:
    """
    执行工作流
    """
    state, errmsg = WorkflowChain().process(workflow_id, from_begin=from_begin)
    if not state:
        return _SchemaResponse(success=False, message=errmsg)
    return _SchemaResponse(success=True)


@router.post(
    "/{workflow_id}/start", summary="启用工作流", response_model=_SchemaResponse[None]
)
def start_workflow(
    workflow_id: int,
    command: WorkflowMutationCommand = Depends(get_workflow_mutation_command),
    _: Any = Depends(get_current_active_manage_user),
) -> Any:
    """
    启用工作流
    """
    result = command.start(workflow_id)
    return _SchemaResponse(success=result.success, message=result.message)


@router.post(
    "/{workflow_id}/pause", summary="停用工作流", response_model=_SchemaResponse[None]
)
def pause_workflow(
    workflow_id: int,
    command: WorkflowMutationCommand = Depends(get_workflow_mutation_command),
    _: Any = Depends(get_current_active_manage_user),
) -> Any:
    """
    停用工作流
    """
    result = command.pause(workflow_id)
    return _SchemaResponse(success=result.success, message=result.message)


@router.post(
    "/{workflow_id}/reset", summary="重置工作流", response_model=_SchemaResponse[None]
)
async def reset_workflow(
    workflow_id: int,
    command: WorkflowDefinitionCommand = Depends(get_workflow_definition_command),
    _: Any = Depends(get_current_active_manage_user_async),
) -> Any:
    """
    重置工作流
    """
    result = await command.reset(workflow_id)
    return _SchemaResponse(success=result.success, message=result.message)


@router.get("/{workflow_id}", summary="工作流详情", response_model=_SchemaWorkflow)
async def get_workflow(
    workflow_id: int,
    query: WorkflowQueryService = Depends(get_workflow_query_service),
    _: Any = Depends(get_current_active_manage_user_async),
) -> Any:
    """
    获取工作流详情
    """
    return await query.get(workflow_id)


@router.put("/{workflow_id}", summary="更新工作流", response_model=_SchemaResponse[None])
def update_workflow(
    workflow_id: int,
    workflow: _SchemaWorkflow,
    command: WorkflowMutationCommand = Depends(get_workflow_mutation_command),
    _: Any = Depends(get_current_active_manage_user),
) -> Any:
    """
    更新工作流，路径 ID 是本次更新的唯一目标。
    """
    workflow_data = workflow.model_dump()
    workflow_data["id"] = workflow_id
    result = command.update(workflow_data)
    return _SchemaResponse(success=result.success, message=result.message)


@router.delete("/{workflow_id}", summary="删除工作流", response_model=_SchemaResponse[None])
def delete_workflow(
    workflow_id: int,
    command: WorkflowMutationCommand = Depends(get_workflow_mutation_command),
    _: Any = Depends(get_current_active_manage_user),
) -> Any:
    """
    删除工作流
    """
    result = command.delete(workflow_id)
    return _SchemaResponse(success=result.success, message=result.message)
