"""
提供媒体服务器配置对应的媒体库、条目与播放信息查询接口。
"""

from typing import Any, List, Optional

from fastapi import Depends, HTTPException, status

from app.adapters.web.security.access import verify_token
from app.api.dependencies.history import get_mediaserver_query_service
from app.api.response import (
    COLLECTION_PAGINATION_OPENAPI_KEY,
    CompatibleCountParam,
    CompatiblePageParam,
    ResponseAPIRouter,
)
from app.application.configuration import get_configured_system_config
from app.application.mediaserver import (
    MEDIA_SERVER_EXISTENCE_QUERY,
    MediaServerQueryService,
    get_mediaserver_configs,
)
from app.chain.download import DownloadChain
from app.chain.mediaserver import MediaServerChain
from app.domain.context import MediaInfo
from app.domain.metainfo import MetaInfo
from app.schemas.common import ServiceClientInfo as _SchemaServiceClientInfo
from app.schemas.context import MediaInfo as _SchemaMediaInfo
from app.schemas.media import build_media_key, resolve_media_identity
from app.schemas.mediaserver import MediaServerExistingEpisodes as _SchemaMediaServerExistingEpisodes
from app.schemas.mediaserver import MediaServerExistsData as _SchemaMediaServerExistsData
from app.schemas.mediaserver import MediaServerField, MediaServerQueryOptions, NotExistMediaInfo
from app.schemas.mediaserver import MediaServerLibrary as _SchemaMediaServerLibrary
from app.schemas.mediaserver import MediaServerPlayData as _SchemaMediaServerPlayData
from app.schemas.mediaserver import MediaServerPlayItem as _SchemaMediaServerPlayItem
from app.schemas.mediaserver import NotExistMediaInfo as _SchemaNotExistMediaInfo
from app.schemas.response import Response as _SchemaResponse
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.schemas.types import MediaSource, MediaType, SystemConfigKey

router = ResponseAPIRouter()

# 首页卡片只消费展示内容，播放进度仅由继续观看卡片使用。
_CARD_QUERY = MediaServerQueryOptions(fields=frozenset({MediaServerField.YEAR, MediaServerField.IMAGES, MediaServerField.LINK}))
_RESUME_QUERY = _CARD_QUERY.with_fields(MediaServerField.USER_STATE)
_LIBRARY_QUERY = MediaServerQueryOptions(fields=frozenset({
    MediaServerField.PATH, MediaServerField.IMAGES, MediaServerField.COUNTS, MediaServerField.LINK,
}))


def _require_mediaserver_result(result: Optional[List[Any]]) -> List[Any]:
    """
    保留媒体服务器成功空列表，并把提供方失败转换为明确的网关错误。
    """
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="媒体服务器请求失败",
        )
    return result


@router.get(
    "/play/{itemid:path}",
    summary="在线播放",
    response_model=_SchemaResponse[_SchemaMediaServerPlayData],
)
def play_item(
    itemid: str, _: _SchemaTokenPayload = Depends(verify_token)
) -> _SchemaResponse[dict[str, Any]]:
    """
    获取媒体服务器播放页面地址
    """
    if not itemid:
        return _SchemaResponse(success=False, message="参数错误")
    configs = get_mediaserver_configs()
    if not configs:
        return _SchemaResponse(success=False, message="未配置媒体服务器")
    media_chain = MediaServerChain()
    for config in configs:
        name = config.name
        if not name:
            continue
        item = media_chain.iteminfo(
            server=name,
            item_id=itemid,
            query=MediaServerQueryOptions(fields=frozenset()),
        )
        if item:
            play_url = media_chain.get_play_url(server=name, item_id=itemid)
            if play_url:
                return _SchemaResponse(
                    success=True,
                    data={
                        "url": play_url,
                        "item_id": item.item_id or itemid,
                        "server_id": item.server_id,
                        "server_type": item.server,
                    },
                )
    return _SchemaResponse(success=False, message="未找到播放地址")


@router.get(
    "/exists",
    summary="查询本地是否存在（数据库）",
    response_model=_SchemaResponse[_SchemaMediaServerExistsData],
)
async def exists_local(
    title: Optional[str] = None,
    year: Optional[str] = None,
    mtype: Optional[str] = None,
    media_source: Optional[MediaSource] = None,
    media_id: Optional[str] = None,
    season: Optional[int] = None,
    service: MediaServerQueryService = Depends(get_mediaserver_query_service),
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    判断本地是否存在
    """
    if bool(media_source) != bool(media_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="media_source 和 media_id 必须同时提供",
        )
    meta = MetaInfo(title) if title else None
    if season is None:
        season = meta.begin_season if meta else None
    # 返回对象
    ret_info = {}
    # 本地数据库是否存在
    item_id = await service.find_item_id(
        title=meta.name if meta else None,
        year=year,
        mtype=mtype,
        media_source=media_source,
        media_id=media_id,
        season=season,
    )
    if item_id:
        ret_info = {"id": item_id}
    return _SchemaResponse(success=True, data={"item": ret_info})


@router.post(
    "/exists_remote",
    summary="查询已存在的剧集信息（媒体服务器）",
    response_model=_SchemaMediaServerExistingEpisodes,
)
def exists(media_in: _SchemaMediaInfo, _: _SchemaTokenPayload = Depends(verify_token)) -> Any:
    """
    根据媒体信息查询媒体库已存在的剧集信息
    """
    # 转化为媒体信息对象
    mediainfo = MediaInfo()
    mediainfo.from_dict(media_in.model_dump())
    existsinfo = MediaServerChain().media_exists(
        mediainfo=mediainfo, query=MEDIA_SERVER_EXISTENCE_QUERY
    )
    if not existsinfo:
        return {}
    seasons = existsinfo.seasons or {}
    if media_in.season is not None:
        return {media_in.season: seasons.get(media_in.season) or []}
    return seasons


@router.post(
    "/notexists",
    summary="查询媒体库缺失信息（媒体服务器）",
    response_model=List[_SchemaNotExistMediaInfo],
    openapi_extra={COLLECTION_PAGINATION_OPENAPI_KEY: True},
)
def not_exists(
    media_in: _SchemaMediaInfo, _: _SchemaTokenPayload = Depends(verify_token),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> Any:
    """
    根据媒体信息查询缺失电影/剧集，媒体类型兼容英文与中文值
    """
    # 媒体信息
    meta = MetaInfo(title=media_in.title or "")
    mtype = MediaType.from_agent(media_in.type) or MediaType(media_in.type) if media_in.type else None
    if mtype:
        meta.type = mtype
    if media_in.season is not None:
        meta.begin_season = media_in.season
        meta.type = MediaType.TV
    if media_in.year:
        meta.year = media_in.year
    # 转化为媒体信息对象
    mediainfo = MediaInfo()
    # 复用已解析的类型，避免领域对象再次将 Agent 英文值按中文枚举解析。
    mediainfo.from_dict({**media_in.model_dump(), "type": mtype})
    exist_flag, no_exists = DownloadChain().get_no_exists_info(
        meta=meta, mediainfo=mediainfo
    )
    media_source, media_id = resolve_media_identity(media=mediainfo)
    mediakey = build_media_key(media_source, media_id)
    if mediainfo.type in {MediaType.MOVIE, MediaType.MUSIC}:
        # 电影和音乐都是原子存在性结果；专辑内部曲目完整性由下载入口校验。
        return [] if exist_flag else [NotExistMediaInfo()]
    elif no_exists and no_exists.get(mediakey):
        # 电视剧返回缺失的剧集
        return list(no_exists.get(mediakey).values())
    return []


@router.get("/latest", summary="最新入库条目", response_model=List[_SchemaMediaServerPlayItem])
def latest(
    server: str,
    count: Optional[int] = 20,
    userinfo: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    获取媒体服务器最新入库条目
    """
    return _require_mediaserver_result(
        MediaServerChain().latest(
            server=server,
            count=count,
            username=userinfo.username,
            query=_CARD_QUERY,
        )
    )


@router.get("/playing", summary="正在播放条目", response_model=List[_SchemaMediaServerPlayItem])
def playing(
    server: str,
    count: Optional[int] = 12,
    userinfo: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    获取媒体服务器正在播放条目
    """
    return _require_mediaserver_result(
        MediaServerChain().playing(
            server=server,
            count=count,
            username=userinfo.username,
            query=_RESUME_QUERY,
        )
    )


@router.get("/library", summary="媒体库列表", response_model=List[_SchemaMediaServerLibrary])
def library(
    server: str,
    hidden: Optional[bool] = False,
    userinfo: _SchemaTokenPayload = Depends(verify_token),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> Any:
    """
    获取媒体服务器媒体库列表
    """
    return _require_mediaserver_result(
        MediaServerChain().librarys(
            server=server,
            username=userinfo.username,
            hidden=hidden if hidden is not None else False,
            query=_LIBRARY_QUERY,
        )
    )


@router.get(
    "/clients",
    summary="查询可用媒体服务器",
    response_model=List[_SchemaServiceClientInfo],
)
async def clients(_: _SchemaTokenPayload = Depends(verify_token), page: CompatiblePageParam = None, count: CompatibleCountParam = None) -> Any:
    """
    查询已启用的媒体服务器名称与类型。

    :param _: 已验证的访问令牌
    :param page: 兼容客户端的分页参数，此接口不据此分页
    :param count: 兼容客户端的数量参数，此接口不据此截断
    :return: 已启用媒体服务器的名称和类型列表
    """
    mediaservers: List[dict[str, object]] = get_configured_system_config().get(SystemConfigKey.MediaServers)
    if mediaservers:
        return [
            {"name": d.get("name"), "type": d.get("type")}
            for d in mediaservers
            if d.get("enabled")
        ]
    return []
