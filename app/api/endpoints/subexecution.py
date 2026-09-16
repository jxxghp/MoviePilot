"""订阅执行批次状态与取消端点。"""

from typing import Any, List, Optional

from fastapi import Depends, HTTPException

from app.api.dependencies.auth import get_current_active_user_async
from app.api.dependencies.subscription import (
    get_subscription_execution_status_service,
    get_subscription_query_service,
)
from app.api.principal import ApiPrincipal
from app.api.response import ResponseAPIRouter
from app.application.subscription.query import SubscriptionQueryService
from app.application.subscription.status import SubscriptionExecutionStatusService
from app.schemas.response import Response as _SchemaResponse
from app.schemas.subscribe import SubscriptionBatchStatus as _SchemaSubscriptionBatchStatus

router = ResponseAPIRouter()


async def _accessible_subscription_ids(
    query: SubscriptionQueryService,
    current_user: ApiPrincipal,
) -> Optional[set[int]]:
    """返回普通用户可访问订阅 ID；超级用户以 None 表示不限制。"""
    if current_user.is_superuser:
        return None
    subscribes = await query.list_public(current_user.name)
    return {item.id for item in subscribes if item.id is not None}


@router.get(  # type: ignore[misc]
    "/execution/batches",
    summary="查看订阅搜索进度",
    response_model=List[_SchemaSubscriptionBatchStatus],
)
async def list_subscription_execution_batches(
    limit: int = 10,
    status_service: SubscriptionExecutionStatusService = Depends(get_subscription_execution_status_service),
    query: SubscriptionQueryService = Depends(get_subscription_query_service),
    current_user: ApiPrincipal = Depends(get_current_active_user_async),
) -> Any:
    """返回当前用户完整可见的最近搜索批次。"""
    accessible_ids = await _accessible_subscription_ids(query, current_user)
    batches = await status_service.list_batches(
        accessible_subscription_ids=accessible_ids,
        limit=limit,
    )
    return [_SchemaSubscriptionBatchStatus.model_validate(batch) for batch in batches]


@router.get(  # type: ignore[misc]
    "/execution/batches/{batch_id}",
    summary="查看一次订阅搜索",
    response_model=_SchemaSubscriptionBatchStatus,
)
async def get_subscription_execution_batch(
    batch_id: str,
    status_service: SubscriptionExecutionStatusService = Depends(get_subscription_execution_status_service),
    query: SubscriptionQueryService = Depends(get_subscription_query_service),
    current_user: ApiPrincipal = Depends(get_current_active_user_async),
) -> Any:
    """按稳定 ID 返回当前用户可访问的搜索批次。"""
    accessible_ids = await _accessible_subscription_ids(query, current_user)
    batch = await status_service.get_batch(
        batch_id,
        accessible_subscription_ids=accessible_ids,
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="没有找到这次搜索，请刷新后重试")
    return _SchemaSubscriptionBatchStatus.model_validate(batch)


@router.put(  # type: ignore[misc]
    "/execution/batches/{batch_id}/cancel",
    summary="停止一次订阅搜索",
    response_model=_SchemaResponse[None],
)
async def cancel_subscription_execution_batch(
    batch_id: str,
    status_service: SubscriptionExecutionStatusService = Depends(get_subscription_execution_status_service),
    query: SubscriptionQueryService = Depends(get_subscription_query_service),
    current_user: ApiPrincipal = Depends(get_current_active_user_async),
) -> Any:
    """在权限校验后请求取消尚未越过下载副作用边界的任务。"""
    accessible_ids = await _accessible_subscription_ids(query, current_user)
    batch = await status_service.get_batch(
        batch_id,
        accessible_subscription_ids=accessible_ids,
    )
    if batch is None:
        return _SchemaResponse(success=False, message="没有找到这次搜索，请刷新后重试")
    cancelled = await status_service.request_cancel(batch_id)
    return _SchemaResponse(
        success=bool(cancelled),
        message="" if cancelled else "这次搜索已经结束，暂时无法停止",
    )
