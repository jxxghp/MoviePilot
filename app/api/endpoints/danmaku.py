"""
提供自定义弹弹play 兼容弹幕代理、自动匹配、手动绑定与管理员配置接口。
"""

from __future__ import annotations

import gzip
from typing import List, Optional

from fastapi import Depends, HTTPException, Query, Request, Response

from app.adapters.external.danmaku import RequestUtilsDanmakuTransport
from app.adapters.web.security.access import verify_token
from app.api.dependencies.auth import get_current_active_superuser
from app.api.principal import ApiPrincipal
from app.api.response import ResponseAPIRouter
from app.application.danmaku import DanmakuError, DanmakuService, is_danmaku_enabled
from app.schemas.danmaku import (
    DanmakuAnimeSearchItem,
    DanmakuBangumiResponse,
    DanmakuBindingRequest,
    DanmakuCacheClearResult,
    DanmakuConfigResponse,
    DanmakuConfigTestRequest,
    DanmakuConfigTestResult,
    DanmakuConfigUpdateRequest,
    DanmakuEpisodeCandidate,
    DanmakuMatchRequest,
    DanmakuMatchResponse,
    DanmakuStatusResponse,
)
from app.schemas.response import Response as _SchemaResponse
from app.schemas.token import TokenPayload as _SchemaTokenPayload

router = ResponseAPIRouter()

_default_danmaku_service: Optional[DanmakuService] = None


def get_danmaku_service() -> DanmakuService:
    """
    获取进程共享的弹幕代理应用服务实例（可在测试中通过 FastAPI dependency override 替换）。

    :return: 弹幕代理服务单例
    """
    global _default_danmaku_service
    if _default_danmaku_service is None:
        _default_danmaku_service = DanmakuService(transport=RequestUtilsDanmakuTransport())
    return _default_danmaku_service


def _raise_danmaku_http_error(error: DanmakuError) -> None:
    """
    将弹幕应用层异常映射为标准 HTTPException。

    :param error: 弹幕业务异常
    :raises HTTPException: 始终抛出带对应状态码的 HTTP 异常
    """
    raise HTTPException(status_code=error.status_code, detail=str(error)) from error


def _if_none_match_matches(header_value: Optional[str], etag: str) -> bool:
    """
    判断请求头 ``If-None-Match`` 是否命中当前强 ETag（兼容弱标记与逗号分隔列表）。

    :param header_value: 客户端携带的 If-None-Match 原始请求头
    :param etag: 服务端当前 ETag
    :return: 命中时返回 True
    """
    if not header_value:
        return False
    target = etag.strip().removeprefix("W/").strip()
    for part in header_value.split(","):
        token = part.strip()
        if token == "*":
            return True
        if token.removeprefix("W/").strip() == target:
            return True
    return False


def _client_accepts_gzip(request: Request) -> bool:
    """
    判断客户端请求头 ``Accept-Encoding`` 是否允许直接返回预压缩 gzip 载荷。

    :param request: 当前 HTTP 请求对象
    :return: 允许 gzip 传输编码时返回 True
    """
    accept_encoding = request.headers.get("accept-encoding") or ""
    for item in accept_encoding.split(","):
        encoding = item.split(";", 1)[0].strip().lower()
        if encoding == "gzip":
            return True
    return False


@router.get(
    "/status",
    summary="查询弹幕代理服务启用状态",
    response_model=_SchemaResponse[DanmakuStatusResponse],
)
def get_danmaku_status(
    _: _SchemaTokenPayload = Depends(verify_token),
) -> _SchemaResponse[DanmakuStatusResponse]:
    """
    返回当前实例是否已启用弹幕代理服务，绝不暴露上游地址或密钥信息。

    :param _: 已验证的登录用户访问令牌
    :return: 仅包含 enabled 布尔值的安全状态
    """
    return _SchemaResponse(success=True, data=DanmakuStatusResponse(enabled=is_danmaku_enabled()))


@router.get(
    "/config",
    summary="获取弹幕服务配置（仅管理员）",
    response_model=_SchemaResponse[DanmakuConfigResponse],
)
def get_danmaku_config(
    _: ApiPrincipal = Depends(get_current_active_superuser),
    service: DanmakuService = Depends(get_danmaku_service),
) -> _SchemaResponse[DanmakuConfigResponse]:
    """
    获取自定义弹弹play 兼容 API 配置快照（密钥仅返回布尔配置状态）及缓存统计。

    :param _: 已验证的超级管理员主体
    :param service: 弹幕代理应用服务
    :return: 脱敏后的管理员配置响应
    """
    return _SchemaResponse(success=True, data=service.get_admin_config())


@router.put(
    "/config",
    summary="更新弹幕服务配置（仅管理员）",
    response_model=_SchemaResponse[DanmakuConfigResponse],
)
def update_danmaku_config(
    payload: DanmakuConfigUpdateRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser),
    service: DanmakuService = Depends(get_danmaku_service),
) -> _SchemaResponse[DanmakuConfigResponse]:
    """
    加密持久化弹幕服务配置；变更上游来源或聚合参数时自动隔离旧缓存。

    :param payload: 管理员提交的配置更新请求
    :param _: 已验证的超级管理员主体
    :param service: 弹幕代理应用服务
    :return: 更新后的管理员配置快照
    """
    try:
        updated = service.update_config(payload)
    except DanmakuError as err:
        _raise_danmaku_http_error(err)
    return _SchemaResponse(success=True, data=updated)


@router.post(
    "/config/test",
    summary="测试弹幕上游连通性与鉴权（仅管理员）",
    response_model=_SchemaResponse[DanmakuConfigTestResult],
)
def test_danmaku_config(
    payload: Optional[DanmakuConfigTestRequest] = None,
    _: ApiPrincipal = Depends(get_current_active_superuser),
    service: DanmakuService = Depends(get_danmaku_service),
) -> _SchemaResponse[DanmakuConfigTestResult]:
    """
    使用表单覆盖项或已保存配置向上游发起轻量连通性探测。

    :param payload: 可选的未保存表单参数
    :param _: 已验证的超级管理员主体
    :param service: 弹幕代理应用服务
    :return: 探测耗时与鉴权校验结果
    """
    result = service.test_config(payload)
    return _SchemaResponse(success=True, data=result)


@router.post(
    "/match",
    summary="匹配当前播放媒体对应的弹幕分集",
    response_model=_SchemaResponse[DanmakuMatchResponse],
)
def match_danmaku(
    payload: DanmakuMatchRequest,
    _: _SchemaTokenPayload = Depends(verify_token),
    service: DanmakuService = Depends(get_danmaku_service),
) -> _SchemaResponse[DanmakuMatchResponse]:
    """
    按手动绑定、L1 缓存、上游自动匹配及剧名兜底搜索顺序解析弹幕分集。

    :param payload: 当前播放媒体的身份与文件名元数据
    :param _: 已验证的登录用户访问令牌
    :param service: 弹幕代理应用服务
    :return: 匹配到的目标弹幕分集与候选列表
    """
    try:
        matched = service.match(payload)
    except DanmakuError as err:
        _raise_danmaku_http_error(err)
    return _SchemaResponse(success=True, data=matched)


@router.get(
    "/search",
    summary="搜索弹幕作品与分集（供播放页手动纠偏）",
    response_model=_SchemaResponse[List[DanmakuAnimeSearchItem]],
)
def search_danmaku(
    keyword: str = Query(min_length=1, max_length=120),
    episode: Optional[str] = Query(default=None, max_length=32),
    anime_type: Optional[str] = Query(default=None, alias="type", max_length=32),
    _: _SchemaTokenPayload = Depends(verify_token),
    service: DanmakuService = Depends(get_danmaku_service),
) -> _SchemaResponse[List[DanmakuAnimeSearchItem]]:
    """
    调用上游搜索接口按剧名查找候选作品及分集列表。

    :param keyword: 搜索关键词
    :param episode: 可选集号过滤
    :param anime_type: 可选作品分类过滤
    :param _: 已验证的登录用户访问令牌
    :param service: 弹幕代理应用服务
    :return: 作品搜索结果列表
    """
    try:
        items = service.search(keyword, episode=episode, anime_type=anime_type)
    except DanmakuError as err:
        _raise_danmaku_http_error(err)
    return _SchemaResponse(success=True, data=items)


@router.get(
    "/bangumi/{anime_id}",
    summary="获取指定弹幕作品的完整分集目录",
    response_model=_SchemaResponse[DanmakuBangumiResponse],
)
def get_danmaku_bangumi(
    anime_id: int,
    _: _SchemaTokenPayload = Depends(verify_token),
    service: DanmakuService = Depends(get_danmaku_service),
) -> _SchemaResponse[DanmakuBangumiResponse]:
    """
    返回指定作品下的全部弹幕分集列表，用于播放页手动换集。

    :param anime_id: 上游作品唯一标识
    :param _: 已验证的登录用户访问令牌
    :param service: 弹幕代理应用服务
    :return: 作品及分集列表
    """
    try:
        bangumi = service.get_bangumi(anime_id)
    except DanmakuError as err:
        _raise_danmaku_http_error(err)
    return _SchemaResponse(success=True, data=bangumi)


@router.get(
    "/comments/{episode_id}",
    summary="获取归一化紧凑单集弹幕（支持 ETag 304 与预压缩 gzip）",
    response_class=Response,
    response_model=None,
)
def get_danmaku_comments(
    episode_id: int,
    request: Request,
    ch_convert: Optional[int] = Query(default=None, ge=0, le=2),
    force_refresh: bool = Query(default=False),
    _: _SchemaTokenPayload = Depends(verify_token),
    service: DanmakuService = Depends(get_danmaku_service),
) -> Response:
    """
    返回单集归一化紧凑弹幕；支持 ``If-None-Match`` 304 协商与零二次压缩 ``gzip`` 直出。

    :param episode_id: 上游弹幕分集标识
    :param request: 当前 HTTP 请求对象
    :param ch_convert: 可选简繁转换覆盖参数
    :param force_refresh: 为 True 时强制忽略 L2 磁盘缓存刷新上游
    :param _: 已验证的登录用户访问令牌
    :param service: 弹幕代理应用服务
    :return: 304 空响应、预压缩 gzip JSON 响应或解压后的 JSON 响应
    """
    try:
        result = service.get_comments(
            episode_id,
            ch_convert=ch_convert,
            force_refresh=force_refresh,
        )
    except DanmakuError as err:
        _raise_danmaku_http_error(err)

    common_headers = {
        "ETag": result.etag,
        "Cache-Control": "private, max-age=300",
        "Vary": "Accept-Encoding",
    }
    if not force_refresh and _if_none_match_matches(request.headers.get("if-none-match"), result.etag):
        return Response(status_code=304, headers=common_headers)

    if _client_accepts_gzip(request):
        return Response(
            content=result.gz_bytes,
            media_type="application/json",
            headers={**common_headers, "Content-Encoding": "gzip"},
        )
    return Response(
        content=gzip.decompress(result.gz_bytes),
        media_type="application/json",
        headers=common_headers,
    )


@router.put(
    "/bindings",
    summary="保存或覆盖媒体条目的弹幕手动绑定与时间偏移",
    response_model=_SchemaResponse[DanmakuEpisodeCandidate],
)
def put_danmaku_binding(
    payload: DanmakuBindingRequest,
    userinfo: _SchemaTokenPayload = Depends(verify_token),
    service: DanmakuService = Depends(get_danmaku_service),
) -> _SchemaResponse[DanmakuEpisodeCandidate]:
    """
    持久化媒体条目与弹幕分集的手动纠偏关系及时间偏移，一端纠偏全端共享。

    :param payload: 手动绑定请求参数
    :param userinfo: 当前已验证登录用户令牌载荷
    :param service: 弹幕代理应用服务
    :return: 已保存的弹幕分集信息
    """
    try:
        saved = service.save_binding(payload, updated_by=userinfo.username or str(userinfo.sub or ""))
    except DanmakuError as err:
        _raise_danmaku_http_error(err)
    return _SchemaResponse(success=True, data=saved)


@router.delete(
    "/bindings",
    summary="删除弹幕手动绑定以恢复自动匹配",
    response_model=_SchemaResponse[bool],
)
def delete_danmaku_binding(
    server: Optional[str] = Query(default=None, max_length=128),
    item_id: Optional[str] = Query(default=None, max_length=256),
    tmdb_id: Optional[str] = Query(default=None, max_length=64),
    season_number: Optional[int] = Query(default=None, ge=0, le=999),
    episode_number: Optional[int] = Query(default=None, ge=0, le=99999),
    _: _SchemaTokenPayload = Depends(verify_token),
    service: DanmakuService = Depends(get_danmaku_service),
) -> _SchemaResponse[bool]:
    """
    移除指定媒体条目的手动绑定记录，使后续播放恢复自动匹配流程。

    :param server: 媒体服务器配置名称
    :param item_id: 媒体服务器条目标识
    :param tmdb_id: 可选 TMDB 标识
    :param season_number: 可选季号
    :param episode_number: 可选集号
    :param _: 已验证的登录用户访问令牌
    :param service: 弹幕代理应用服务
    :return: 是否命中并移除了已有绑定
    """
    try:
        removed = service.delete_binding(
            server=server,
            item_id=item_id,
            tmdb_id=tmdb_id,
            season_number=season_number,
            episode_number=episode_number,
        )
    except DanmakuError as err:
        _raise_danmaku_http_error(err)
    return _SchemaResponse(success=True, data=removed)


@router.delete(
    "/cache",
    summary="清空所有弹幕元数据与磁盘缓存（仅管理员）",
    response_model=_SchemaResponse[DanmakuCacheClearResult],
)
def clear_danmaku_cache(
    _: ApiPrincipal = Depends(get_current_active_superuser),
    service: DanmakuService = Depends(get_danmaku_service),
) -> _SchemaResponse[DanmakuCacheClearResult]:
    """
    清空所有 L1 匹配元数据缓存与 L2 磁盘压缩弹幕文件。

    :param _: 已验证的超级管理员主体
    :param service: 弹幕代理应用服务
    :return: 清理文件数与释放空间统计
    """
    return _SchemaResponse(success=True, data=service.clear_cache())
