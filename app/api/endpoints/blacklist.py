"""
管理员永久种子黑名单接口，不修改下载任务、文件或历史记录。
"""

from typing import Annotated

from fastapi import Depends, HTTPException, Path, Query

from app.api.dependencies.auth import get_current_active_superuser_async
from app.api.dependencies.history import get_history_query_service
from app.api.response import ResponseAPIRouter
from app.application.download.blacklist import TorrentBlacklistService
from app.application.history import HistoryQueryService
from app.domain.blacklist import BlacklistConfigurationError, history_blacklist_identity
from app.schemas.blacklist import BlacklistAddResult, BlacklistPage, BlacklistRemoveResult, BlacklistRule
from app.schemas.response import Response

router = ResponseAPIRouter()


@router.get("", summary="分页查询永久种子黑名单", response_model=Response[BlacklistPage])
async def list_blacklist(
    _: object = Depends(get_current_active_superuser_async),
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始")] = 1,
    count: Annotated[int, Query(ge=1, le=100, description="每页条数，最多 100")] = 30,
) -> Response[BlacklistPage]:
    """
    在单个不可变快照上分页查询，不向普通用户开放全局规则。
    """
    try:
        return Response(success=True, data=TorrentBlacklistService().list(page=page, count=count))
    except BlacklistConfigurationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("", summary="添加永久种子黑名单规则", response_model=Response[BlacklistAddResult])
async def add_blacklist_rule(
    body: BlacklistRule,
    _: object = Depends(get_current_active_superuser_async),
) -> Response[BlacklistAddResult]:
    """
    原子添加字面关键词、站点或发布者规则，重复添加返回原条目。
    """
    try:
        return Response(success=True, data=await TorrentBlacklistService().async_add_rule(body))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/history/{history_id}", summary="从下载历史屏蔽具体资源", response_model=Response[BlacklistAddResult])
async def add_history_blacklist(
    history_id: Annotated[int, Path(ge=1, description="服务端下载历史 ID")],
    _: object = Depends(get_current_active_superuser_async),
    query: HistoryQueryService = Depends(get_history_query_service),
) -> Response[BlacklistAddResult]:
    """
    读取服务端历史安全身份；旧历史没有有效哈希时明确要求改用手动规则。
    """
    history = await query.get_download(history_id)
    if history is None:
        raise HTTPException(status_code=404, detail="下载历史不存在")
    try:
        identity = history_blacklist_identity(history)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        return Response(success=True, data=await TorrentBlacklistService().async_add_resource(identity))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/{entry_id}", summary="移除永久种子黑名单条目", response_model=Response[BlacklistRemoveResult])
async def remove_blacklist(
    entry_id: Annotated[str, Path(pattern=r"^[a-f0-9]{32}$", description="黑名单条目 ID")],
    _: object = Depends(get_current_active_superuser_async),
) -> Response[BlacklistRemoveResult]:
    """
    原子幂等移除条目，条目不存在仍然成功，不恢复或删除任何下载任务。
    """
    try:
        removed = await TorrentBlacklistService().async_remove(entry_id)
        return Response(success=True, data=BlacklistRemoveResult(removed=removed))
    except BlacklistConfigurationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
