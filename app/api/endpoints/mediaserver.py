"""
提供媒体服务器配置对应的媒体库、条目与播放信息查询接口。
"""

from pathlib import Path
from typing import Any, List, Optional

from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from app.adapters.mediastream import (
    _AUDIO_EXTENSIONS,
    _VIDEO_EXTENSIONS,
    _file_stream_response,
    _file_stream_ticket_media_type,
    _find_lyrics_path,
    _find_subtitle_path,
    _is_audio_path,
    _is_file_stream_ticket,
    _remote_stream_response,
    _resolve_file_stream_ticket,
    _stream_source,
    _subtitle_tracks,
    _validate_stream_url,
)
from app.adapters.mediastream import _configured_stream_hosts as _configured_media_stream_hosts
from app.adapters.mediastream import _create_file_stream_ticket as _create_media_file_stream_ticket
from app.adapters.web.security.access import verify_resource_token, verify_token
from app.api.dependencies.auth import get_current_active_manage_user
from app.api.dependencies.history import get_mediaserver_query_service
from app.api.endpoints.storage import _ensure_manage_storage_item
from app.api.principal import ApiPrincipal
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
from app.application.storage import StorageHelper
from app.chain.download import DownloadChain
from app.chain.mediaserver import MediaServerChain
from app.chain.tmdb import TmdbChain
from app.domain.context import MediaInfo
from app.domain.context import MusicInfo as _DomainMusicInfo
from app.domain.metainfo import MetaInfo
from app.schemas.common import ServiceClientInfo as _SchemaServiceClientInfo
from app.schemas.context import MediaInfo as _SchemaMediaInfo
from app.schemas.media import build_media_key, resolve_media_identity
from app.schemas.mediaserver import MediaServerExistingEpisodes as _SchemaMediaServerExistingEpisodes
from app.schemas.mediaserver import MediaServerExistsData as _SchemaMediaServerExistsData
from app.schemas.mediaserver import MediaServerField, MediaServerQueryOptions, NotExistMediaInfo
from app.schemas.mediaserver import MediaServerFileStreamTicket as _SchemaMediaServerFileStreamTicket
from app.schemas.mediaserver import MediaServerFileStreamTicketRequest as _SchemaMediaServerFileStreamTicketRequest
from app.schemas.mediaserver import MediaServerItem as _SchemaMediaServerItem
from app.schemas.mediaserver import MediaServerLibrary as _SchemaMediaServerLibrary
from app.schemas.mediaserver import MediaServerMusicStreamRequest as _SchemaMediaServerMusicStreamRequest
from app.schemas.mediaserver import MediaServerPlayData as _SchemaMediaServerPlayData
from app.schemas.mediaserver import MediaServerPlayItem as _SchemaMediaServerPlayItem
from app.schemas.mediaserver import MediaServerStreamInfo as _SchemaMediaServerStreamInfo
from app.schemas.mediaserver import NotExistMediaInfo as _SchemaNotExistMediaInfo
from app.schemas.response import Response as _SchemaResponse
from app.schemas.tmdb import TmdbEpisode as _SchemaTmdbEpisode
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.schemas.types import MediaSource, MediaType, SystemConfigKey

router = ResponseAPIRouter()


def _configured_stream_hosts(path: Optional[Path], *, include_media_servers: bool = False) -> set[str]:
    """
    从应用层配置读取允许 STRM 使用的媒体服务器和云存储主机。

    :param path: 可选的本机文件或 STRM 文件路径
    :param include_media_servers: 是否允许代理已配置媒体服务器的原始流地址
    :return: 管理员明确配置的主机名集合
    """
    if not include_media_servers and (not path or path.suffix.casefold() != ".strm"):
        return set()
    return _configured_media_stream_hosts(get_mediaserver_configs(), StorageHelper.get_storagies())


def _find_stream_item(
    itemid: str, server: Optional[str] = None
) -> tuple[str, _SchemaMediaServerItem, Optional[Path]]:
    """
    查找已入库媒体条目，同时保留本机可读取时的文件路径。

    :param itemid: 媒体服务器条目标识
    :param server: 可选的媒体服务器配置名，用于精确定位同 ID 条目
    :return: 媒体服务器名称、条目和当前主机可读取时的文件路径
    :raises HTTPException: 媒体服务器条目不存在时
    """
    find_options = {"server": server} if server else {}
    result = MediaServerChain().find_item(
        itemid,
        **find_options,
        query=MediaServerQueryOptions(fields=frozenset({MediaServerField.PATH})),
    )
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到已入库媒体")
    server, item = result
    path = Path(item.path) if item.path else None
    return server, item, path if path and path.is_file() else None


def _resolve_stream_item(
    itemid: str, user_id: Optional[int], server: Optional[str] = None
) -> tuple[Optional[str], _SchemaMediaServerItem, Optional[Path]]:
    """
    解析媒体服务器条目或当前用户的单文件播放票据。

    :param itemid: 媒体服务器条目标识或单文件票据
    :param user_id: 当前已认证用户主键，用于限制票据使用人
    :param server: 可选的媒体服务器配置名，用于精确定位同 ID 条目
    :return: 媒体服务器名称、播放条目及可选的本地媒体路径
    :raises HTTPException: 条目缺失或票据不属于当前用户时
    """
    if not _is_file_stream_ticket(itemid):
        server_name, item, path = _find_stream_item(itemid, **({"server": server} if server else {}))
        return server_name, item, path
    path = _resolve_file_stream_ticket(itemid, user_id)
    item_type = "Audio" if _file_stream_ticket_media_type(itemid) == "audio" else "Episode"
    return None, _SchemaMediaServerItem(item_id=itemid, title=path.stem, item_type=item_type, path=str(path)), path


def _find_music_stream_item(
    music: _SchemaMediaServerMusicStreamRequest,
) -> tuple[str, _SchemaMediaServerItem, Optional[Path]]:
    """
    通过标准录音信息匹配已入库单曲，并从对应媒体服务器读取实际路径。

    :param music: 单曲来源身份、标题和表演者信息
    :return: 匹配的服务器配置名、单曲条目和可选的本地文件路径
    :raises HTTPException: 曲目未入库或实际条目不是可播放音频时
    """
    chain = MediaServerChain()
    mediainfo = _DomainMusicInfo(
        media_source=music.media_source,
        media_id=music.media_id,
        title=music.title,
        artists=music.artists,
        album=music.album,
        album_artist=music.album_artist,
        year=music.year,
        track_number=music.track_number,
        disc_number=music.disc_number,
    )
    match = chain.media_exists(
        mediainfo=mediainfo,
        query=MediaServerQueryOptions(fields=frozenset({MediaServerField.IDENTITY, MediaServerField.MUSIC})),
    )
    if not match or not match.server or not match.itemid:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到已入库音乐")
    item = chain.iteminfo(
        server=match.server,
        item_id=match.itemid,
        query=MediaServerQueryOptions(fields=frozenset({MediaServerField.PATH, MediaServerField.MUSIC})),
    )
    if not item or str(item.item_id or item.id or "") != str(match.itemid):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到已入库音乐")
    path = Path(item.path) if item.path else None
    path = path if path and path.is_file() else None
    if not _is_audio_item(item, path):
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="该音乐格式不支持在线播放")
    return match.server, item, path


def _playback_source(
    server: Optional[str], itemid: str, item: _SchemaMediaServerItem, path: Optional[Path]
) -> tuple[str, Path | str, set[str]]:
    """
    优先选择媒体服务器原始流，适配器不支持时才解析本地文件或 STRM。

    单文件票据没有媒体服务器来源，不会进入媒体服务器查询。

    :param server: 已定位的媒体服务器配置名，单文件票据为 None
    :param itemid: 已授权的条目标识或单文件票据
    :param item: 已授权的媒体服务器条目
    :param path: 可选的本机媒体文件路径
    :return: 流来源类型、文件路径或上游 URL，以及允许的已配置主机名
    :raises HTTPException: 媒体服务器不支持原始流且本地文件不可读取时
    """
    if server:
        upstream = MediaServerChain().get_stream_url(server, itemid, item.item_type)
        if upstream:
            hosts = _configured_stream_hosts(path, include_media_servers=True)
            return "server", _validate_stream_url(upstream, configured_hosts=hosts), hosts
    if not path:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="媒体服务器不支持文件流且本地文件不可读取")
    hosts = _configured_stream_hosts(path)
    source_type, source = _stream_source(path, hosts)
    return source_type, source, hosts


def _stream_item_note(item: _SchemaMediaServerItem, key: str) -> Optional[str]:
    """
    从媒体服务器的扩展信息中读取可展示的音乐文本，忽略非文本值。

    :param item: 已解析的媒体条目
    :param key: 音乐扩展信息的字段名
    :return: 字符串字段；缺失或格式不符时返回 None
    """
    value = item.note.get(key) if isinstance(item.note, dict) else None
    return value if isinstance(value, str) else None


def _lyrics_url(
    itemid: str, path: Path, request: Request, server: Optional[str] = None
) -> Optional[str]:
    """
    为音频的同目录同名 LRC 歌词创建当前实例流地址。

    :param itemid: 媒体服务器条目标识或短期文件票据
    :param path: 已验证的音频或 STRM 文件路径
    :param request: 当前请求，用于构造本实例歌词流地址
    :param server: 可选媒体服务器配置名，精确绑定搜索结果来源
    :return: 歌词文件存在时的同源地址，否则返回 None
    """
    if not _find_lyrics_path(path):
        return None
    query = {"itemid": itemid}
    if server:
        query["server"] = server
    return str(request.url_for("stream_item_lyrics").include_query_params(**query))


def _is_audio_item(item: _SchemaMediaServerItem, path: Optional[Path]) -> bool:
    """
    通过音频扩展名或媒体服务器的 Audio 类型识别普通音频和音频 STRM。

    :param item: 已解析的媒体服务器条目
    :param path: 条目报告的本地媒体路径，缺失时使用条目的媒体类型
    :return: 可作为音频播放时返回 True
    """
    return (path is not None and _is_audio_path(path)) or (item.item_type or "").casefold() in {
        "audio", "music", "track", MediaType.MUSIC.value.casefold(),
    }


@router.post(
    "/stream-ticket",
    summary="为文件管理器选中的本地媒体签发播放票据",
    response_model=_SchemaResponse[_SchemaMediaServerFileStreamTicket],
)
def create_file_stream_ticket(
    request: _SchemaMediaServerFileStreamTicketRequest,
    current_user: ApiPrincipal = Depends(get_current_active_manage_user),
) -> _SchemaResponse[_SchemaMediaServerFileStreamTicket]:
    """
    验证管理用户可访问的本地媒体文件并签发短期单文件票据。

    :param request: 文件管理器提交的文件条目，仅接受本地支持格式的媒体或 STRM 文件
    :param current_user: 已验证且拥有管理权限的当前用户
    :return: 与当前用户及签发时文件身份绑定的短期播放标识
    :raises HTTPException: 用户无权访问文件、文件不可读或格式不受支持时
    """
    fileitem = request.file
    if fileitem.storage != "local" or fileitem.type != "file":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="仅支持本地媒体文件")
    authorized_item = _ensure_manage_storage_item(fileitem, current_user)
    path = Path(authorized_item.path or "")
    if not path.is_absolute():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="媒体文件路径无效")
    media_type = request.media_type
    supported_extensions = _AUDIO_EXTENSIONS if media_type == "audio" else _VIDEO_EXTENSIONS
    if path.suffix.casefold() != ".strm" and path.suffix.casefold() not in supported_extensions:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="该文件格式不支持在线播放")
    item_id = _create_media_file_stream_ticket(path, current_user.id, media_type)
    return _SchemaResponse(
        success=True,
        data=_SchemaMediaServerFileStreamTicket(
            item_id=item_id, title=authorized_item.name or path.stem, media_type=media_type
        ),
    )


@router.get(
    "/search",
    summary="搜索已配置媒体服务器中的电视剧和音乐单曲",
    response_model=_SchemaResponse[List[_SchemaMediaServerPlayItem]],
)
def search_library(
    keyword: str = Query(min_length=1, max_length=120),
    type_filter: Optional[List[str]] = Query(default=None, alias="type"),
    limit: int = Query(default=30, ge=1, le=50),
    _: _SchemaTokenPayload = Depends(verify_token),
) -> _SchemaResponse[List[_SchemaMediaServerPlayItem]]:
    """
    使用已配置媒体服务器自身的搜索接口查找电视剧和音乐单曲。

    :param keyword: 搜索关键词
    :param type_filter: 可选业务类型，省略时搜索电视剧和音乐
    :param limit: 最多返回的结果数
    :param _: 已验证的访问令牌
    :return: 带海报、服务器来源和本地播放标识的媒体列表
    """
    allowed_types = {MediaType.TV.value, MediaType.MUSIC.value}
    selected_types = list(
        dict.fromkeys(value for value in (type_filter or [MediaType.TV.value, MediaType.MUSIC.value]) if value in allowed_types)
    )
    if not selected_types:
        return _SchemaResponse(success=True, data=[])
    results = MediaServerChain().search_library(keyword, selected_types, limit)
    return _SchemaResponse(success=True, data=results)


@router.get(
    "/stream-info",
    summary="查询媒体流与同名字幕",
    response_model=_SchemaResponse[_SchemaMediaServerStreamInfo],
)
def stream_info(
    itemid: str,
    request: Request,
    server: Optional[str] = None,
    userinfo: _SchemaTokenPayload = Depends(verify_token),
) -> _SchemaResponse[_SchemaMediaServerStreamInfo]:
    """
    返回当前实例代理的媒体流地址；媒体服务器条目优先通过服务器原始流播放。

    :param itemid: 媒体服务器条目标识
    :param request: 当前请求，用于构造本实例流地址
    :param server: 可选媒体服务器配置名，精确绑定搜索结果来源
    :param userinfo: 已验证的访问令牌
    :return: 可供原生播放器读取的媒体流清单
    """
    server_name, item, path = _resolve_stream_item(itemid, userinfo.sub, server=server)
    _playback_source(server_name, itemid, item, path)
    stream_query = {"itemid": itemid}
    if server_name:
        stream_query["server"] = server_name
    content_url = request.url_for("stream_item_content").include_query_params(**stream_query)
    return _SchemaResponse(
        success=True,
        data=_SchemaMediaServerStreamInfo(
            item_id=item.item_id or itemid,
            server=server_name,
            title=item.title or (path.stem if path else itemid),
            content_url=str(content_url),
            media_type="audio" if _is_audio_item(item, path) else "video",
            artist=_stream_item_note(item, "artist"),
            album=_stream_item_note(item, "album"),
            subtitles=[] if not path or _is_audio_item(item, path) else _subtitle_tracks(itemid, path, request, server),
            lyrics_url=_lyrics_url(itemid, path, request, server) if path and _is_audio_item(item, path) else None,
        ),
    )


@router.post(
    "/stream-info/music",
    summary="查找并返回已入库音乐曲目的流地址",
    response_model=_SchemaResponse[_SchemaMediaServerStreamInfo],
)
def music_stream_info(
    music: _SchemaMediaServerMusicStreamRequest,
    request: Request,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> _SchemaResponse[_SchemaMediaServerStreamInfo]:
    """
    将音乐元数据源的曲目身份匹配到媒体服务器中的单曲并返回流与 LRC 歌词。

    :param music: 详情页选中的单曲元数据，不接受专辑或艺术家 ID
    :param request: 当前请求，用于生成当前实例流地址
    :param _: 已验证的访问令牌
    :return: 实际媒体服务器单曲的音频流和可选同名歌词清单
    """
    server, item, path = _find_music_stream_item(music)
    itemid = str(item.item_id or item.id or "")
    _playback_source(server, itemid, item, path)
    content_url = request.url_for("stream_item_content").include_query_params(itemid=itemid, server=server)
    return _SchemaResponse(
        success=True,
        data=_SchemaMediaServerStreamInfo(
            item_id=itemid,
            server=server,
            title=item.title or music.title,
            content_url=str(content_url),
            media_type="audio",
            artist=music.artist or music.album_artist or next(iter(music.artists), None) or _stream_item_note(item, "artist"),
            album=music.album or _stream_item_note(item, "album"),
            lyrics_url=_lyrics_url(itemid, path, request, server) if path else None,
        ),
    )


@router.get(
    "/stream",
    summary="流式读取已入库媒体",
    name="stream_item_content",
    response_class=StreamingResponse,
    response_model=None,
    responses={
        206: {"description": "部分媒体内容"},
        416: {"model": _SchemaResponse[None], "description": "字节范围无效"},
    },
)
def stream_item_content(
    itemid: str,
    request: Request,
    server: Optional[str] = None,
    userinfo: _SchemaTokenPayload = Depends(verify_resource_token),
) -> StreamingResponse:
    """
    媒体服务器条目优先代理原始流，明确不支持时回退本地；文件票据只读本地。

    :param itemid: 媒体服务器条目标识
    :param request: 当前请求，包含可选 Range 头
    :param server: 可选媒体服务器配置名，精确绑定搜索结果来源
    :param userinfo: 已验证的访问令牌
    :return: 本地文件或受控 STRM 上游流式响应
    """
    server_name, item, path = _resolve_stream_item(itemid, userinfo.sub, server=server)
    source_type, source, configured_hosts = _playback_source(server_name, itemid, item, path)
    range_header = request.headers.get("range")
    if source_type == "server":
        try:
            return _remote_stream_response(str(source), range_header, configured_hosts)
        except HTTPException as error:
            if not path or error.status_code not in {status.HTTP_404_NOT_FOUND, status.HTTP_501_NOT_IMPLEMENTED}:
                raise
            configured_hosts = _configured_stream_hosts(path)
            source_type, source = _stream_source(path, configured_hosts)
    if source_type == "file":
        return _file_stream_response(Path(source), range_header)
    return _remote_stream_response(str(source), range_header, configured_hosts)


@router.get(
    "/stream-subtitle",
    summary="流式读取同名外挂字幕",
    name="stream_item_subtitle",
    response_class=StreamingResponse,
    response_model=None,
    responses={
        206: {"description": "部分字幕内容"},
        416: {"model": _SchemaResponse[None], "description": "字节范围无效"},
    },
)
def stream_item_subtitle(
    itemid: str,
    subtitle_id: str,
    request: Request,
    userinfo: _SchemaTokenPayload = Depends(verify_token),
    server: Optional[str] = None,
) -> StreamingResponse:
    """
    只读取当前影片同目录且名称匹配的字幕文件。

    :param itemid: 媒体服务器条目标识
    :param subtitle_id: 字幕文件名，不接受目录片段
    :param request: 当前请求，包含可选 Range 头
    :param server: 可选媒体服务器配置名，精确绑定搜索结果来源
    :param userinfo: 已验证的访问令牌
    :return: 字幕文件流式响应
    :raises HTTPException: 字幕不存在或不属于当前影片时返回 404
    """
    _, _, video_path = _resolve_stream_item(itemid, userinfo.sub, server=server)
    if not video_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到同名字幕")
    subtitle_path = _find_subtitle_path(video_path, subtitle_id)
    return _file_stream_response(subtitle_path, request.headers.get("range"))


@router.get(
    "/stream-lyrics",
    summary="流式读取音乐同名 LRC 歌词",
    name="stream_item_lyrics",
    response_class=StreamingResponse,
    response_model=None,
)
def stream_item_lyrics(
    itemid: str,
    request: Request,
    userinfo: _SchemaTokenPayload = Depends(verify_token),
    server: Optional[str] = None,
) -> StreamingResponse:
    """
    只读取当前音频同目录且基础文件名一致的 LRC 歌词。

    :param itemid: 媒体服务器条目标识或当前用户的单文件票据
    :param request: 当前请求，包含可选 Range 头
    :param server: 可选媒体服务器配置名，精确绑定搜索结果来源
    :param userinfo: 已验证的访问令牌
    :return: UTF-8 文本歌词的流式响应
    :raises HTTPException: 歌词不存在或条目不是音频时返回 404
    """
    _, item, audio_path = _resolve_stream_item(itemid, userinfo.sub, server=server)
    if not audio_path or not _is_audio_item(item, audio_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到音乐歌词")
    lyrics_path = _find_lyrics_path(audio_path)
    if not lyrics_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到音乐歌词")
    return _file_stream_response(lyrics_path, request.headers.get("range"))


@router.get(
    "/episodes",
    summary="查询电视剧全部可播放单集",
    response_model=_SchemaResponse[dict[int, dict[int, str]]],
)
def episode_ids(
    itemid: str,
    _: _SchemaTokenPayload = Depends(verify_token),
    server: Optional[str] = None,
) -> _SchemaResponse[dict[int, dict[int, str]]]:
    """
    按媒体服务器的季集编号返回所有已入库单集的条目标识。

    :param itemid: 媒体服务器中的电视剧系列条目标识
    :param server: 可选媒体服务器配置名，精确绑定搜索结果来源
    :param _: 已验证的访问令牌
    :return: 季号到集号及其可播放条目标识的映射
    """
    find_options = {"server": server} if server else {}
    result = MediaServerChain().find_item(
        itemid, **find_options, query=MediaServerQueryOptions(fields=frozenset())
    )
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到已入库电视剧")
    chain = MediaServerChain()
    server, item = result
    item_id = item.item_id or itemid
    seasons = chain.episodes(server=server, item_id=item_id, query=MediaServerQueryOptions(fields=frozenset()))
    if seasons is None:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="媒体服务器剧集查询失败")
    result_map: dict[int, dict[int, str]] = {}
    for season_info in seasons:
        season = season_info.season
        if season is None:
            continue
        episode_ids = chain.get_season_episode_ids(server=server, item_id=item_id, season=season)
        existing_numbers = {int(number) for number in season_info.episodes or []}
        available = {
            int(episode): str(episode_id)
            for episode, episode_id in episode_ids.items()
            if episode_id and (not existing_numbers or int(episode) in existing_numbers)
        }
        if available:
            result_map[season] = available
    return _SchemaResponse(success=True, data=result_map)


@router.get(
    "/episode-metadata",
    summary="查询已入库电视剧单季的集展示信息",
    response_model=_SchemaResponse[list[_SchemaTmdbEpisode]],
)
def episode_metadata(
    itemid: str,
    season: int = Query(ge=0),
    server: Optional[str] = None,
    _: _SchemaTokenPayload = Depends(verify_token),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> _SchemaResponse[list[_SchemaTmdbEpisode]]:
    """
    沿用已入库系列的明确 TMDB 身份补充集海报、名称和简介，不按标题猜测作品。

    :param itemid: 媒体服务器中的系列条目标识
    :param season: 媒体服务器的原生季号，0 表示特别篇
    :param server: 搜索结果提供的媒体服务器配置名称
    :param _: 已验证的访问令牌
    :param page: 兼容分页参数，省略时返回该季全部展示信息
    :param count: 兼容分页大小，由统一响应路由处理显式分页窗口
    :return: 该季的可选展示信息；缺少明确 TMDB 身份时为空，不改变可播放集映射
    :raises HTTPException: 系列条目不存在时返回 404
    """
    find_options = {"server": server} if server else {}
    result = MediaServerChain().find_item(
        itemid, **find_options,
        query=MediaServerQueryOptions(fields=frozenset({MediaServerField.IDENTITY})),
    )
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到已入库电视剧")
    item = result[1]
    source, media_id = resolve_media_identity(media=item)
    if source != MediaSource.TMDB or not media_id or not media_id.isdigit():
        return _SchemaResponse(success=True, data=[])
    episodes = TmdbChain().tmdb_episodes(tmdbid=int(media_id), season=season)
    return _SchemaResponse(success=True, data=episodes)


@router.get(
    "/episodes/{itemid}/{season}",
    summary="查询电视剧单集条目标识",
    response_model=_SchemaResponse[dict[int, str]],
)
def season_episode_ids(
    itemid: str,
    season: int,
    _: _SchemaTokenPayload = Depends(verify_token),
    server: Optional[str] = None,
) -> _SchemaResponse[dict[int, str]]:
    """
    按电视剧系列条目和季号返回可播放单集的媒体服务器 ID。

    :param itemid: 媒体服务器中的电视剧系列条目标识
    :param season: 季号，特别篇使用 0
    :param server: 可选媒体服务器配置名，精确绑定搜索结果来源
    :param _: 已验证的访问令牌
    :return: 集数到已入库单集条目标识的映射
    """
    find_options = {"server": server} if server else {}
    result = MediaServerChain().find_item(
        itemid, **find_options, query=MediaServerQueryOptions(fields=frozenset())
    )
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到已入库电视剧")
    server, item = result
    episodes = MediaServerChain().get_season_episode_ids(server=server, item_id=item.item_id or itemid, season=season)
    return _SchemaResponse(success=True, data=episodes or {})


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
    found = await service.find_item(
        title=meta.name if meta else None,
        year=year,
        mtype=mtype,
        media_source=media_source,
        media_id=media_id,
        season=season,
    )
    if found and found.item_id:
        ret_info = {"id": found.item_id}
        # 条目标识只在所属服务器内唯一；旧缓存缺少服务器名称时省略该键，不输出空值
        if found.server:
            ret_info["server"] = found.server
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
    page: Optional[int] = None,
    userinfo: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    获取媒体服务器最新入库条目；传入 `page` 时按 `count` 分页切片返回，支持客户端无限滚动加载。
    """
    if page is None or page < 1:
        return _require_mediaserver_result(
            MediaServerChain().latest(
                server=server,
                count=count,
                username=userinfo.username,
                query=_CARD_QUERY,
            )
        )
    page_size = max(1, min(count or 20, 50))
    offset = (page - 1) * page_size
    max_total = 200
    if offset >= max_total:
        return []
    fetch_count = min(offset + page_size, max_total)
    items = _require_mediaserver_result(
        MediaServerChain().latest(
            server=server,
            count=fetch_count,
            username=userinfo.username,
            query=_CARD_QUERY,
        )
    )
    return items[offset : offset + page_size]


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
