"""
订阅执行批次状态与取消端点。
"""

import time
from collections.abc import AsyncIterator
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from fastapi import Depends, HTTPException, Path, Query, Request
from starlette.responses import Response, StreamingResponse

from app.adapters.web.security.access import verify_resource_token
from app.api.context import get_host_runtime
from app.api.dependencies.auth import get_current_active_user_async
from app.api.dependencies.subscription import (
    get_subscription_execution_status_service,
    get_subscription_query_service,
    subscription_log_query_scope,
)
from app.api.endpoints.system import _build_log_zip_response
from app.api.principal import ApiPrincipal
from app.api.response import ResponseAPIRouter
from app.application.subscription.query import SubscriptionQueryService
from app.application.subscription.status import SubscriptionExecutionStatusService
from app.application.system import LogNotFoundError
from app.schemas.response import Response as _SchemaResponse
from app.schemas.subscribe import SubscriptionActivityEvent as _SchemaSubscriptionActivityEvent
from app.schemas.subscribe import SubscriptionActivityPage as _SchemaSubscriptionActivityPage
from app.schemas.subscribe import SubscriptionBatchStatus as _SchemaSubscriptionBatchStatus
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.startup.composition.context import HostRuntime

router = ResponseAPIRouter()


@router.get(
    "/{subscription_id}/activity",
    summary="查看订阅业务活动",
    response_model=_SchemaSubscriptionActivityPage,
)
async def list_subscription_activity(
    subscription_id: int = Path(..., ge=1),
    history: bool = False,
    kind: Optional[str] = Query(None, pattern="^(created|updated|status|completed|refresh|search)$"),
    days: Optional[int] = Query(None, ge=1, le=365),
    before: Optional[str] = Query(None, pattern="^[a-zA-Z0-9_-]{1,64}$"),
    limit: int = Query(30, ge=1, le=100),
    current_user: ApiPrincipal = Depends(get_current_active_user_async),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> _SchemaSubscriptionActivityPage:
    """
    按当前 owner 校验活动或完成历史，分页仅返回轻量概要。
    """
    repository = runtime.subscription.activity_repository
    if repository is None:
        raise HTTPException(status_code=503, detail="订阅活动暂时不可用")
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="milliseconds") if days else None
    result = await repository.list_accessible(
        subscription_id, current_user.name, current_user.is_superuser,
        history=history, kind=kind, since=since, before=before, limit=limit,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="订阅不存在或无权查看")
    events, next_cursor = result
    return _SchemaSubscriptionActivityPage.model_validate({
        "items": [
            {**{key: value for key, value in asdict(event).items() if key not in {"activity_key", "tracking", "details"}},
             "details_loaded": False,
             "details": {key: value for key, value in event.details.items() if key in {"reason", "retry_at", "phase", "changes"}}}
            for event in events
        ],
        "next_cursor": next_cursor,
    })


@router.get(
    "/{subscription_id}/activity/{event_id}",
    summary="查看订阅活动详情",
    response_model=_SchemaSubscriptionActivityEvent,
)
async def get_subscription_activity(
    subscription_id: int = Path(..., ge=1),
    event_id: str = Path(..., pattern="^[a-zA-Z0-9_-]{1,64}$"),
    history: bool = False,
    current_user: ApiPrincipal = Depends(get_current_active_user_async),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> _SchemaSubscriptionActivityEvent:
    """
    按需读取冻结条件、资源判断和恢复记录，并再次检查订阅归属。
    """
    repository = runtime.subscription.activity_repository
    if repository is None:
        raise HTTPException(status_code=503, detail="订阅活动暂时不可用")
    event = await repository.get_accessible(
        subscription_id, event_id, current_user.name, current_user.is_superuser, history=history,
    )
    if event is None:
        raise HTTPException(status_code=404, detail="活动不存在或无权查看")
    return _SchemaSubscriptionActivityEvent.model_validate({
        key: value for key, value in asdict(event).items() if key not in {"activity_key", "tracking"}
    })


async def _accessible_subscription_ids(
    query: SubscriptionQueryService,
    current_user: ApiPrincipal,
) -> Optional[set[int]]:
    """
    返回普通用户可访问订阅 ID；超级用户以 None 表示不限制。
    """
    if current_user.is_superuser:
        return None
    subscribes = await query.list_public(current_user.name)
    return {item.id for item in subscribes if item.id is not None}


@router.get(
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
    """
    返回当前用户完整可见的最近搜索批次。
    """
    accessible_ids = await _accessible_subscription_ids(query, current_user)
    batches = await status_service.list_batches(
        accessible_subscription_ids=accessible_ids,
        limit=limit,
    )
    return [_SchemaSubscriptionBatchStatus.model_validate(batch) for batch in batches]


@router.get(
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
    """
    按稳定 ID 返回当前用户可访问的搜索批次。
    """
    accessible_ids = await _accessible_subscription_ids(query, current_user)
    batch = await status_service.get_batch(
        batch_id,
        accessible_subscription_ids=accessible_ids,
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="没有找到这次搜索，请刷新后重试")
    return _SchemaSubscriptionBatchStatus.model_validate(batch)


@router.put(
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
    """
    在权限校验后请求取消尚未越过下载副作用边界的任务。
    """
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


async def _require_subscription_log_access(
    subscription_id: int,
    query: SubscriptionQueryService,
    user: _SchemaTokenPayload,
) -> None:
    """
    日志仅对当前订阅 owner 或管理员开放，未知和越权 ID 均返回 404。
    """
    subscribe = await query.get_public(subscription_id)
    if subscribe is None or (not user.super_user and subscribe.username != user.username):
        raise HTTPException(status_code=404, detail="订阅不存在或无权查看")


async def _authorize_subscription_logs(subscription_id: int, user: _SchemaTokenPayload, runtime: HostRuntime) -> None:
    """
    在独立短会话中确认归属，返回后不持有数据库事务。
    """
    async with subscription_log_query_scope(runtime) as query:
        await _require_subscription_log_access(subscription_id, query, user)


@router.get(
    "/{subscription_id}/logs",
    summary="查看单个订阅的执行日志",
    response_model=None,
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}, "text/plain": {}}}},
)
async def get_subscription_logs(
    request: Request,
    subscription_id: int = Path(gt=0),
    length: int = Query(default=100, ge=-1, le=600),
    user: _SchemaTokenPayload = Depends(verify_resource_token),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> Response:
    """
    按订阅权限输出原始文本或实时流；首次执行前保持空流等待日志生成。
    """
    await _authorize_subscription_logs(subscription_id, user, runtime)
    logfile = f"subscriptions/{subscription_id}.log"
    if length == -1:
        try:
            content = await runtime.system.read_log(logfile)
        except LogNotFoundError:
            content = ""
        return Response(content, media_type="text/plain", headers={"Cache-Control": "no-store"})
    last_check = time.monotonic()

    async def disconnected() -> bool:
        """
        断开或归属变更时停止流；周期重读归属并验证资源令牌的有效性。
        """
        nonlocal last_check
        if await request.is_disconnected():
            return True
        if time.monotonic() - last_check >= 5:
            try:
                verify_resource_token(request.cookies.get(runtime.system.settings.get("PROJECT_NAME")))
                await _authorize_subscription_logs(subscription_id, user, runtime)
            except HTTPException:
                return True
            last_check = time.monotonic()
        return False

    source = await runtime.system.follow_log(logfile, max(1, length), disconnected)

    async def events() -> AsyncIterator[str]:
        """
        逐行编码 SSE，心跳不进入前端日志列表。
        """
        async for line in source:
            content = line
            yield "".join(f"data: {part}\n" for part in content.splitlines()) + "\n" if content else ": keepalive\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get(
    "/{subscription_id}/logs/download",
    summary="下载单个订阅的执行日志",
    response_model=None,
    response_class=Response,
    responses={200: {"content": {"application/zip": {}}}},
)
async def download_subscription_logs(
    subscription_id: int = Path(gt=0),
    user: _SchemaTokenPayload = Depends(verify_resource_token),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> Response:
    """
    在归属检查后下载当前订阅的日志及滚动备份，客户端不能指定路径。
    """
    await _authorize_subscription_logs(subscription_id, user, runtime)
    return await _build_log_zip_response(
        f"subscriptions/{subscription_id}", runtime,
        allow_empty=True,
    )
