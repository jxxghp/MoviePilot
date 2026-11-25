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
    _extract_local_strm_direct_url,
    _file_stream_response,
    _file_stream_ticket_media_type,
    _find_lyrics_path,
    _find_subtitle_path,
    _is_audio_path,
    _is_file_stream_ticket,
    _remote_stream_response,
    _resolve_file_stream_ticket,
    _resolve_storage_file_stream_ticket,
    _sanitize_strm_direct_url,
    _seekable_stream_response,
    _stream_source,
    _subtitle_tracks,
    _validate_stream_url,
)
from app.adapters.mediastream import _configured_stream_hosts as _configured_media_stream_hosts
from app.adapters.mediastream import _create_file_stream_ticket as _create_media_file_stream_ticket
from app.adapters.mediastream import (
    _create_storage_file_stream_ticket as _create_media_storage_file_stream_ticket,
)
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
from app.application.danmaku import is_danmaku_enabled
from app.application.mediaserver import (
    MEDIA_SERVER_EXISTENCE_QUERY,
    MediaServerQueryService,
    get_mediaserver_configs,
)
from app.application.playback import (
    BINDABLE_MEDIA_SERVER_TYPES,
    build_client_playback_routes,
    delete_user_mediaserver_credential,
    get_playback_session_registry,
    get_user_mediaserver_credential,
    list_user_mediaserver_bindings,
    mark_user_mediaserver_credential_invalid,
    resolve_server_sync_mode,
    save_user_mediaserver_credential,
)
from app.application.storage import StorageHelper
from app.chain.download import DownloadChain
from app.chain.mediaserver import MediaServerChain
from app.chain.storage import StorageChain
from app.chain.tmdb import TmdbChain
from app.domain.context import MediaInfo
from app.domain.context import MusicInfo as _DomainMusicInfo
from app.domain.metainfo import MetaInfo
from app.schemas.common import ServiceClientInfo as _SchemaServiceClientInfo
from app.schemas.context import MediaInfo as _SchemaMediaInfo
from app.schemas.file import FileItem as _SchemaFileItem
from app.schemas.media import build_media_key, resolve_media_identity
from app.schemas.mediaserver import MediaServerAccountBinding as _SchemaMediaServerAccountBinding
from app.schemas.mediaserver import MediaServerAccountBindRequest as _SchemaMediaServerAccountBindRequest
from app.schemas.mediaserver import MediaServerBrowseItem as _SchemaMediaServerBrowseItem
from app.schemas.mediaserver import MediaServerBrowsePage as _SchemaMediaServerBrowsePage
from app.schemas.mediaserver import MediaServerExistingEpisodes as _SchemaMediaServerExistingEpisodes
from app.schemas.mediaserver import MediaServerExistsData as _SchemaMediaServerExistsData
from app.schemas.mediaserver import MediaServerField, MediaServerQueryOptions, NotExistMediaInfo
from app.schemas.mediaserver import MediaServerFileStreamTicket as _SchemaMediaServerFileStreamTicket
from app.schemas.mediaserver import MediaServerFileStreamTicketRequest as _SchemaMediaServerFileStreamTicketRequest
from app.schemas.mediaserver import MediaServerFilterOptions as _SchemaMediaServerFilterOptions
from app.schemas.mediaserver import MediaServerItem as _SchemaMediaServerItem
from app.schemas.mediaserver import MediaServerItemDetail as _SchemaMediaServerItemDetail
from app.schemas.mediaserver import MediaServerLibrary as _SchemaMediaServerLibrary
from app.schemas.mediaserver import MediaServerMusicStreamRequest as _SchemaMediaServerMusicStreamRequest
from app.schemas.mediaserver import MediaServerPlaybackRoute as _SchemaMediaServerPlaybackRoute
from app.schemas.mediaserver import MediaServerPlayData as _SchemaMediaServerPlayData
from app.schemas.mediaserver import MediaServerPlayItem as _SchemaMediaServerPlayItem
from app.schemas.mediaserver import MediaServerProgressReportMode as _SchemaMediaServerProgressReportMode
from app.schemas.mediaserver import MediaServerProgressReportRequest as _SchemaMediaServerProgressReportRequest
from app.schemas.mediaserver import MediaServerProgressReportResult as _SchemaMediaServerProgressReportResult
from app.schemas.mediaserver import MediaServerStreamInfo as _SchemaMediaServerStreamInfo
from app.schemas.mediaserver import MediaServerSubtitleTrack as _SchemaMediaServerSubtitleTrack
from app.schemas.mediaserver import MediaServerSyncMode as _SchemaMediaServerSyncMode
from app.schemas.mediaserver import NotExistMediaInfo as _SchemaNotExistMediaInfo
from app.schemas.response import Response as _SchemaResponse
from app.schemas.system import MediaServerConf as _SchemaMediaServerConf
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
    storage_ticket = _resolve_storage_file_stream_ticket(itemid, user_id)
    if storage_ticket is not None:
        storage_item, ticket_media_type = storage_ticket
        item_type = "Audio" if ticket_media_type == "audio" else "Episode"
        filename = storage_item.name or Path(storage_item.path or "").name or itemid
        return (
            None,
            _SchemaMediaServerItem(
                item_id=itemid,
                title=Path(filename).stem or filename,
                item_type=item_type,
                path=storage_item.path,
                note={"storage_file_item": storage_item.model_dump(exclude_none=True)},
            ),
            None,
        )
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


_VALID_CLIENT_PLATFORMS = frozenset({"iOS", "Android", "HarmonyOS", "Web"})


def _extract_client_playback_context(request: Request) -> dict[str, Optional[str]]:
    """
    从请求头或查询参数提取客户端播放设备上下文，供会话创建和设备展示使用。

    :param request: 当前 HTTP 请求对象
    :return: 包含 `client_id`、`device_name`、`platform`、`app_version` 的上下文字典
    """
    raw_platform = (request.headers.get("x-mp-platform") or "").strip()
    platform = raw_platform if raw_platform in _VALID_CLIENT_PLATFORMS else None
    client_id = (request.headers.get("x-mp-client-id") or request.query_params.get("client_id") or "").strip() or None
    device_name = (request.headers.get("x-mp-device-name") or "").strip() or None
    app_version = (request.headers.get("x-mp-app-version") or "").strip() or None
    return {
        "client_id": client_id,
        "device_name": device_name,
        "platform": platform,
        "app_version": app_version,
    }


def _find_mediaserver_config(server: Optional[str]) -> Optional[_SchemaMediaServerConf]:
    """
    按配置名称查找已启用的媒体服务器配置。

    :param server: 媒体服务器配置名称
    :return: 匹配的配置对象；未找到或配置未初始化时返回 None
    """
    if not server:
        return None
    try:
        configs = get_mediaserver_configs()
    except Exception:
        return None
    return next((conf for conf in configs if conf.enabled and conf.name == server), None)


def _prepare_manifest_playback_session(
    *,
    server_name: Optional[str],
    item_id: str,
    media_source_id: Optional[str],
    userinfo: _SchemaTokenPayload,
    request: Request,
) -> tuple[Optional[str], Optional[str], _SchemaMediaServerSyncMode]:
    """
    为 `stream-info` / `stream-info/music` 准备上游播放会话并登记进程内注册表。

    :param server_name: 媒体服务器配置名称；单文件票据为 None
    :param item_id: 媒体条目标识
    :param media_source_id: 可选多版本媒体源标识
    :param userinfo: 当前已验证用户的令牌载荷
    :param request: 当前 HTTP 请求对象
    :return: `(play_session_id, server_type, progress_sync_mode)`
    """
    if not server_name:
        return None, None, "none"
    conf = _find_mediaserver_config(server_name)
    server_type = (conf.type or "").strip().casefold() if conf and conf.type else None
    cred = get_user_mediaserver_credential(userinfo.username, server_name)
    sync_mode = resolve_server_sync_mode(server_type, cred, conf.config if conf else None)
    client_ctx = _extract_client_playback_context(request)
    session_info = MediaServerChain().prepare_playback_session(
        server=server_name,
        item_id=item_id,
        media_source_id=media_source_id,
        username=userinfo.username,
        user_token=cred.token if cred else None,
        client_id=client_ctx["client_id"],
        device_name=client_ctx["device_name"],
        platform=client_ctx["platform"],
        app_version=client_ctx["app_version"],
    )
    if not isinstance(session_info, dict):
        return None, server_type, sync_mode
    upstream_session_id = str(session_info.get("play_session_id") or "").strip() or None
    reported_sync_mode = session_info.get("sync_mode")
    if reported_sync_mode in {"session", "userdata", "scrobble", "none"}:
        sync_mode = reported_sync_mode
    raw_headers = session_info.get("stream_headers")
    upstream_headers = {str(k): str(v) for k, v in raw_headers.items()} if isinstance(raw_headers, dict) else {}
    registered = get_playback_session_registry().register(
        play_session_id=upstream_session_id,
        upstream_play_session_id=upstream_session_id,
        user_id=userinfo.sub,
        username=userinfo.username,
        server=server_name,
        server_type=server_type or "",
        item_id=item_id,
        media_source_id=str(session_info.get("media_source_id") or media_source_id or "") or None,
        client_id=client_ctx["client_id"],
        device_name=client_ctx["device_name"],
        platform=client_ctx["platform"],
        app_version=client_ctx["app_version"],
        upstream_headers=upstream_headers,
    )
    return registered.play_session_id, server_type, sync_mode


def _playback_source(
    server: Optional[str],
    itemid: str,
    item: _SchemaMediaServerItem,
    path: Optional[Path],
    media_source_id: Optional[str] = None,
    *,
    play_session_id: Optional[str] = None,
    user_token: Optional[str] = None,
) -> tuple[str, Path | str, set[str]]:
    """
    优先选择媒体服务器原始流，适配器不支持时才解析本地文件或 STRM。

    单文件票据没有媒体服务器来源，不会进入媒体服务器查询。

    :param server: 已定位的媒体服务器配置名，单文件票据为 None
    :param itemid: 已授权的条目标识或单文件票据
    :param item: 已授权的媒体服务器条目
    :param path: 可选的本机媒体文件路径
    :param media_source_id: 可选指定多版本媒体源标识
    :param play_session_id: 可选上游播放会话标识
    :param user_token: 可选用户绑定的访问令牌
    :return: 流来源类型、文件路径或上游 URL，以及允许的已配置主机名
    :raises HTTPException: 媒体服务器不支持原始流且本地文件不可读取时
    """
    if server:
        chain = MediaServerChain()
        extra_kwargs: dict[str, Any] = {}
        if media_source_id:
            extra_kwargs["media_source_id"] = media_source_id
        if play_session_id:
            extra_kwargs["play_session_id"] = play_session_id
        if user_token:
            extra_kwargs["user_token"] = user_token
        upstream = (
            chain.get_stream_url(server, itemid, item.item_type, **extra_kwargs)
            if extra_kwargs
            else chain.get_stream_url(server, itemid, item.item_type)
        )
        if upstream:
            hosts = _configured_stream_hosts(path, include_media_servers=True)
            return "server", _validate_stream_url(upstream, configured_hosts=hosts), hosts
    if not path:
        if isinstance(item.note, dict) and isinstance(item.note.get("storage_file_item"), dict):
            return "storage", itemid, set()
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="媒体服务器不支持文件流且本地文件不可读取")
    hosts = _configured_stream_hosts(path)
    source_type, source = _stream_source(path, hosts)
    return source_type, source, hosts


def _server_subtitle_tracks(
    detail: Optional[_SchemaMediaServerItemDetail],
    itemid: str,
    request: Request,
    server: Optional[str],
    media_source_id: Optional[str],
) -> list[_SchemaMediaServerSubtitleTrack]:
    """
    从媒体服务器详情的媒体源轨道中收集可代理的外挂字幕清单。

    :param detail: 可选媒体服务器详情模型
    :param itemid: 条目标识
    :param request: 当前请求，用于构造本实例字幕代理地址
    :param server: 媒体服务器配置名
    :param media_source_id: 可选目标媒体源标识；省略时采用首个媒体源
    :return: 代理后的服务端外挂字幕列表
    """
    if not detail or not detail.media_sources:
        return []
    target_source = next(
        (src for src in detail.media_sources if media_source_id and src.id == media_source_id),
        detail.media_sources[0],
    )
    tracks: list[_SchemaMediaServerSubtitleTrack] = []
    for stream in target_source.streams:
        if stream.type != "subtitle" or not stream.subtitle_id:
            continue
        query = {"itemid": itemid, "subtitle_id": stream.subtitle_id}
        if server:
            query["server"] = server
        url = str(request.url_for("stream_item_subtitle").include_query_params(**query))
        tracks.append(
            _SchemaMediaServerSubtitleTrack(
                subtitle_id=stream.subtitle_id,
                title=stream.display_title or stream.title or stream.language or "外挂字幕",
                language=stream.language,
                url=url,
            )
        )
    return tracks


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
    summary="为文件管理器选中的媒体文件签发流式播放票据",
    response_model=_SchemaResponse[_SchemaMediaServerFileStreamTicket],
)
def create_file_stream_ticket(
    request: _SchemaMediaServerFileStreamTicketRequest,
    current_user: ApiPrincipal = Depends(get_current_active_manage_user),
) -> _SchemaResponse[_SchemaMediaServerFileStreamTicket]:
    """
    验证管理用户可访问的媒体文件（本地或支持 Range 流式的远端存储，如 SMB / OpenList / 阿里云盘 / 115）并签发短期票据。

    :param request: 文件管理器提交的文件条目
    :param current_user: 已验证且拥有管理权限的当前用户
    :return: 与当前用户及文件身份绑定的短期播放标识
    :raises HTTPException: 用户无权访问文件、存储不支持在线流、文件不可读或格式不受支持时
    """
    fileitem = request.file
    if fileitem.type != "file":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="仅支持媒体文件")
    authorized_item = _ensure_manage_storage_item(fileitem, current_user)
    raw_path = authorized_item.path or ""
    path = Path(raw_path)
    media_type = request.media_type
    supported_extensions = _AUDIO_EXTENSIONS if media_type == "audio" else _VIDEO_EXTENSIONS
    suffix = Path(authorized_item.name or raw_path).suffix.casefold()
    if authorized_item.storage == "local":
        if not path.is_absolute():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="媒体文件路径无效")
        if suffix != ".strm" and suffix not in supported_extensions:
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="该文件格式不支持在线播放")
        item_id = _create_media_file_stream_ticket(path, current_user.id, media_type)
        return _SchemaResponse(
            success=True,
            data=_SchemaMediaServerFileStreamTicket(
                item_id=item_id, title=authorized_item.name or path.stem, media_type=media_type
            ),
        )
    if suffix == ".strm" or suffix not in supported_extensions:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="该文件格式不支持在线流式播放")
    if not authorized_item.storage or not StorageChain().supports_file_stream(authorized_item.storage):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="该存储暂不支持在线流式播放")
    item_id = _create_media_storage_file_stream_ticket(authorized_item, current_user.id, media_type)
    return _SchemaResponse(
        success=True,
        data=_SchemaMediaServerFileStreamTicket(
            item_id=item_id, title=authorized_item.name or path.stem, media_type=media_type
        ),
    )


@router.get(
    "/search",
    summary="搜索已配置媒体服务器中的电影、电视剧和音乐单曲",
    response_model=_SchemaResponse[List[_SchemaMediaServerPlayItem]],
)
def search_library(
    keyword: str = Query(min_length=1, max_length=120),
    type_filter: Optional[List[str]] = Query(default=None, alias="type"),
    limit: int = Query(default=30, ge=1, le=50),
    server: Optional[str] = None,
    library_id: Optional[str] = None,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> _SchemaResponse[List[_SchemaMediaServerPlayItem]]:
    """
    使用已配置媒体服务器自身的搜索接口查找电影、电视剧和音乐单曲。

    :param keyword: 搜索关键词
    :param type_filter: 可选业务类型，省略时搜索电影、电视剧和音乐
    :param limit: 最多返回的结果数
    :param server: 可选媒体服务器配置名，限定单一服务器搜索
    :param library_id: 可选媒体库标识，限定单一媒体库内搜索
    :param _: 已验证的访问令牌
    :return: 带海报、服务器来源和本地播放标识的媒体列表
    """
    allowed_types = {MediaType.MOVIE.value, MediaType.TV.value, MediaType.MUSIC.value}
    default_types = [MediaType.MOVIE.value, MediaType.TV.value, MediaType.MUSIC.value]
    selected_types = list(
        dict.fromkeys(value for value in (type_filter or default_types) if value in allowed_types)
    )
    if not selected_types:
        return _SchemaResponse(success=True, data=[])
    search_kwargs: dict[str, Any] = {}
    if server:
        search_kwargs["server"] = server
    if library_id:
        search_kwargs["library_id"] = library_id
    results = MediaServerChain().search_library(keyword, selected_types, limit, **search_kwargs)
    return _SchemaResponse(success=True, data=results)


def _resolve_manifest_strm_url(
    item: _SchemaMediaServerItem,
    path: Optional[Path],
    detail: Optional[_SchemaMediaServerItemDetail],
    media_source_id: Optional[str] = None,
) -> Optional[str]:
    """
    提取当前播放条目可用的无凭据 STRM HTTP(S) 客户端直链地址。

    优先读取本机 STRM 文件中的目标直链，其次采用媒体服务器详情报告的远端 STRM 媒体源，
    最后兼容媒体服务器条目路径本身即为 HTTP(S) STRM 目标的场景。

    :param item: 已解析的媒体服务器条目或文件票据条目
    :param path: 本机可读取的媒体或 STRM 文件路径
    :param detail: 可选媒体服务器完整详情
    :param media_source_id: 可选指定的多版本媒体源标识
    :return: 规范化后的 HTTP(S) 直链地址；非 STRM 或为本地路径时返回 None
    """
    if path is not None and path.suffix.casefold() == ".strm":
        local_direct = _extract_local_strm_direct_url(path)
        if local_direct:
            return local_direct
    if detail and detail.media_sources:
        target_source = next(
            (src for src in detail.media_sources if media_source_id and src.id == media_source_id),
            detail.media_sources[0],
        )
        if target_source.direct_url:
            sanitized = _sanitize_strm_direct_url(target_source.direct_url)
            if sanitized:
                return sanitized
    if item.path and item.path.strip().casefold().startswith(("http://", "https://")):
        return _sanitize_strm_direct_url(item.path)
    return None


def _build_manifest_playback_routes(
    *,
    content_url: str,
    strm_url: Optional[str],
    server_name: Optional[str],
    item_id: str,
    item: _SchemaMediaServerItem,
    path: Optional[Path],
    media_source_id: Optional[str],
    play_session_id: Optional[str],
    userinfo: _SchemaTokenPayload,
) -> list[_SchemaMediaServerPlaybackRoute]:
    """
    为 `stream-info` / `stream-info/music` 构造安全有序的客户端播放路由候选。

    仅在当前用户已绑定该媒体服务器的有效个人令牌时附加局域网/外网直连路由，未绑定用户仅返回代理及无凭据 STRM 路由。
    """
    if not server_name:
        return build_client_playback_routes(content_url=content_url, strm_url=strm_url)
    conf = _find_mediaserver_config(server_name)
    server_type = (conf.type or "").strip().casefold() if conf and conf.type else None
    cred = get_user_mediaserver_credential(userinfo.username, server_name)
    user_token = cred.token if cred and cred.token else None
    session_state = (
        get_playback_session_registry().get_for_stream(
            play_session_id,
            user_id=userinfo.sub,
            server=server_name,
            item_id=item.item_id or item_id,
        )
        if play_session_id
        else None
    )
    upstream_stream_url: Optional[str] = None
    if user_token:
        try:
            src_type, src_val, _ = _playback_source(
                server_name,
                item_id,
                item,
                path,
                media_source_id=media_source_id,
                play_session_id=session_state.upstream_play_session_id if session_state else None,
                user_token=user_token,
            )
            if src_type == "server":
                upstream_stream_url = str(src_val)
        except HTTPException:
            upstream_stream_url = None
    return build_client_playback_routes(
        content_url=content_url,
        strm_url=strm_url,
        server_type=server_type,
        server_config=conf.config if conf else None,
        upstream_stream_url=upstream_stream_url,
        user_token=user_token,
        upstream_headers=session_state.upstream_headers if session_state else None,
    )


def _extract_stream_danmaku_metadata(
    item: _SchemaMediaServerItem,
    path: Optional[Path],
    detail: Optional[_SchemaMediaServerItemDetail],
    resolved_source_id: Optional[str],
) -> tuple[Optional[int], Optional[str], Optional[str], Optional[int]]:
    """
    从已加载的条目与详情中安全提取年份、TMDB 标识、纯文件名与文件大小，不触发额外媒体服务器请求。

    :param item: 媒体服务器条目
    :param path: 可选本地文件路径
    :param detail: 可选媒体服务器详情
    :param resolved_source_id: 解析出的媒体源标识
    :return: ``(year, tmdb_id, file_name, file_size)`` 四元组
    """
    target_source = (
        next(
            (src for src in detail.media_sources if resolved_source_id and src.id == resolved_source_id),
            detail.media_sources[0],
        )
        if detail and detail.media_sources
        else None
    )
    raw_year = detail.year if detail and detail.year is not None else item.year
    parsed_year: Optional[int] = None
    if raw_year is not None and not isinstance(raw_year, bool):
        try:
            parsed_year = int(str(raw_year).strip())
        except ValueError:
            parsed_year = None
    resolved_tmdb_id = (
        str(detail.tmdb_id).strip()
        if detail and detail.tmdb_id
        else (
            str(item.media_id).strip()
            if item.media_source == MediaSource.TMDB and item.media_id
            else None
        )
    ) or None
    safe_file_name: Optional[str] = None
    if path is not None:
        safe_file_name = path.name or None
    elif item.path and not item.path.strip().casefold().startswith(("http://", "https://")):
        safe_file_name = Path(item.path).name or None
    resolved_file_size = target_source.size if target_source and target_source.size is not None else None
    if resolved_file_size is None and path is not None:
        try:
            resolved_file_size = path.stat().st_size
        except OSError:
            resolved_file_size = None
    return parsed_year, resolved_tmdb_id, safe_file_name, resolved_file_size


@router.get(
    "/stream-info",
    summary="查询媒体流与外挂字幕",
    response_model=_SchemaResponse[_SchemaMediaServerStreamInfo],
)
def stream_info(
    itemid: str,
    request: Request,
    server: Optional[str] = None,
    media_source_id: Optional[str] = None,
    userinfo: _SchemaTokenPayload = Depends(verify_token),
) -> _SchemaResponse[_SchemaMediaServerStreamInfo]:
    """
    返回当前实例代理的媒体流地址；当条目确认为无内嵌凭据的 HTTP(S) STRM 时同时附带客户端直链地址。

    :param itemid: 媒体服务器条目标识
    :param request: 当前请求，用于构造本实例流地址
    :param server: 可选媒体服务器配置名，精确绑定搜索结果来源
    :param media_source_id: 可选多版本媒体源标识
    :param userinfo: 已验证的访问令牌
    :return: 可供原生播放器读取的媒体流清单
    """
    server_name, item, path = _resolve_stream_item(itemid, userinfo.sub, server=server)
    is_audio = _is_audio_item(item, path)
    detail: Optional[_SchemaMediaServerItemDetail] = None
    item_path_is_strm = bool(item.path and item.path.strip().casefold().endswith(".strm"))
    if server_name and (not is_audio or item_path_is_strm):
        raw_detail = MediaServerChain().item_detail(
            server=server_name, item_id=item.item_id or itemid, username=userinfo.username
        )
        if isinstance(raw_detail, _SchemaMediaServerItemDetail):
            detail = raw_detail
    strm_url = _resolve_manifest_strm_url(item, path, detail, media_source_id=media_source_id)
    try:
        _playback_source(server_name, itemid, item, path, media_source_id=media_source_id)
    except HTTPException as exc:
        if not (strm_url and exc.status_code == status.HTTP_501_NOT_IMPLEMENTED):
            raise
    resolved_source_id = media_source_id or (
        detail.media_sources[0].id if detail and detail.media_sources else None
    )
    play_session_id, server_type, sync_mode = _prepare_manifest_playback_session(
        server_name=server_name,
        item_id=item.item_id or itemid,
        media_source_id=resolved_source_id,
        userinfo=userinfo,
        request=request,
    )
    stream_query = {"itemid": itemid}
    if server_name:
        stream_query["server"] = server_name
    if media_source_id:
        stream_query["media_source_id"] = media_source_id
    if play_session_id:
        stream_query["play_session_id"] = play_session_id
    content_url = str(request.url_for("stream_item_content").include_query_params(**stream_query))
    playback_routes = _build_manifest_playback_routes(
        content_url=content_url,
        strm_url=strm_url,
        server_name=server_name,
        item_id=itemid,
        item=item,
        path=path,
        media_source_id=resolved_source_id,
        play_session_id=play_session_id,
        userinfo=userinfo,
    )
    subtitles: list[_SchemaMediaServerSubtitleTrack] = (
        [] if not path or is_audio else _subtitle_tracks(itemid, path, request, server_name)
    )
    if detail and not is_audio:
        seen_ids = {track.subtitle_id for track in subtitles}
        for track in _server_subtitle_tracks(detail, itemid, request, server_name, media_source_id):
            if track.subtitle_id not in seen_ids:
                seen_ids.add(track.subtitle_id)
                subtitles.append(track)
    parsed_year, resolved_tmdb_id, safe_file_name, resolved_file_size = _extract_stream_danmaku_metadata(
        item, path, detail, resolved_source_id
    )
    return _SchemaResponse(
        success=True,
        data=_SchemaMediaServerStreamInfo(
            item_id=item.item_id or itemid,
            server=server_name,
            server_type=server_type,
            title=item.title or (path.stem if path else itemid),
            content_url=content_url,
            strm_url=strm_url,
            playback_routes=playback_routes,
            media_type="audio" if is_audio else "video",
            artist=_stream_item_note(item, "artist"),
            album=_stream_item_note(item, "album"),
            subtitles=subtitles,
            lyrics_url=_lyrics_url(itemid, path, request, server_name) if path and is_audio else None,
            media_source_id=resolved_source_id,
            play_session_id=play_session_id,
            progress_sync_mode=sync_mode,
            resume_position_ms=detail.resume_position_ms if detail else None,
            duration_ms=detail.duration_ms if detail else None,
            danmaku_enabled=False if is_audio else is_danmaku_enabled(),
            series_name=detail.series_name if detail else None,
            season_number=detail.season_number if detail else None,
            episode_number=detail.episode_number if detail else None,
            year=parsed_year,
            tmdb_id=resolved_tmdb_id,
            file_name=safe_file_name,
            file_size=resolved_file_size,
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
    userinfo: _SchemaTokenPayload = Depends(verify_token),
) -> _SchemaResponse[_SchemaMediaServerStreamInfo]:
    """
    将音乐元数据源的曲目身份匹配到媒体服务器中的单曲并返回流与 LRC 歌词。

    :param music: 详情页选中的单曲元数据，不接受专辑或艺术家 ID
    :param request: 当前请求，用于生成当前实例流地址
    :param userinfo: 已验证的访问令牌
    :return: 实际媒体服务器单曲的音频流和可选同名歌词清单
    """
    server, item, path = _find_music_stream_item(music)
    itemid = str(item.item_id or item.id or "")
    detail: Optional[_SchemaMediaServerItemDetail] = None
    if item.path and item.path.strip().casefold().endswith(".strm"):
        raw_detail = MediaServerChain().item_detail(
            server=server, item_id=itemid, username=userinfo.username
        )
        if isinstance(raw_detail, _SchemaMediaServerItemDetail):
            detail = raw_detail
    strm_url = _resolve_manifest_strm_url(item, path, detail)
    try:
        _playback_source(server, itemid, item, path)
    except HTTPException as exc:
        if not (strm_url and exc.status_code == status.HTTP_501_NOT_IMPLEMENTED):
            raise
    play_session_id, server_type, sync_mode = _prepare_manifest_playback_session(
        server_name=server,
        item_id=itemid,
        media_source_id=None,
        userinfo=userinfo,
        request=request,
    )
    stream_query = {"itemid": itemid, "server": server}
    if play_session_id:
        stream_query["play_session_id"] = play_session_id
    content_url = str(request.url_for("stream_item_content").include_query_params(**stream_query))
    playback_routes = _build_manifest_playback_routes(
        content_url=content_url,
        strm_url=strm_url,
        server_name=server,
        item_id=itemid,
        item=item,
        path=path,
        media_source_id=None,
        play_session_id=play_session_id,
        userinfo=userinfo,
    )
    return _SchemaResponse(
        success=True,
        data=_SchemaMediaServerStreamInfo(
            item_id=itemid,
            server=server,
            server_type=server_type,
            title=item.title or music.title,
            content_url=content_url,
            strm_url=strm_url,
            playback_routes=playback_routes,
            media_type="audio",
            artist=music.artist or music.album_artist or next(iter(music.artists), None) or _stream_item_note(item, "artist"),
            album=music.album or _stream_item_note(item, "album"),
            lyrics_url=_lyrics_url(itemid, path, request, server) if path else None,
            play_session_id=play_session_id,
            progress_sync_mode=sync_mode,
        ),
    )


@router.api_route(
    "/stream",
    methods=["GET", "HEAD"],
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
    media_source_id: Optional[str] = None,
    play_session_id: Optional[str] = None,
    userinfo: _SchemaTokenPayload = Depends(verify_resource_token),
) -> StreamingResponse:
    """
    媒体服务器条目优先代理原始流，明确不支持时回退本地；文件票据只读本地。
    同时支持 `GET` 分块传输与 `HEAD` 元数据/Range 探测，确保本地音视频边加载边播放。

    :param itemid: 媒体服务器条目标识
    :param request: 当前请求，包含可选 Range 头
    :param server: 可选媒体服务器配置名，精确绑定搜索结果来源
    :param media_source_id: 可选指定的多版本媒体源标识
    :param play_session_id: 可选本次播放会话标识，用于向同源媒体服务器透传会话头
    :param userinfo: 已验证的访问令牌
    :return: 本地文件或受控 STRM 上游流式响应
    """
    server_name, item, path = _resolve_stream_item(itemid, userinfo.sub, server=server)
    session_state = (
        get_playback_session_registry().get_for_stream(
            play_session_id,
            user_id=userinfo.sub,
            server=server_name,
            item_id=item.item_id or itemid,
        )
        if play_session_id and server_name
        else None
    )
    user_cred = (
        get_user_mediaserver_credential(userinfo.username, server_name)
        if session_state and server_name and userinfo.username
        else None
    )
    source_type, source, configured_hosts = _playback_source(
        server_name,
        itemid,
        item,
        path,
        media_source_id=media_source_id,
        play_session_id=session_state.upstream_play_session_id if session_state else None,
        user_token=user_cred.token if user_cred else None,
    )
    range_header = request.headers.get("range")
    head_only = request.method.upper() == "HEAD"
    upstream_headers = session_state.upstream_headers if session_state and session_state.upstream_headers else None
    if source_type == "server":
        try:
            if upstream_headers:
                return _remote_stream_response(
                    str(source), range_header, configured_hosts, extra_headers=upstream_headers, head_only=head_only
                )
            return _remote_stream_response(str(source), range_header, configured_hosts, head_only=head_only)
        except HTTPException as error:
            if not path or error.status_code not in {status.HTTP_404_NOT_FOUND, status.HTTP_501_NOT_IMPLEMENTED}:
                raise
            configured_hosts = _configured_stream_hosts(path)
            source_type, source = _stream_source(path, configured_hosts)
    if source_type == "storage":
        raw_storage_item = item.note.get("storage_file_item") if isinstance(item.note, dict) else None
        if not isinstance(raw_storage_item, dict):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到远端媒体文件")
        storage_file = _SchemaFileItem.model_validate(raw_storage_item)
        chain = StorageChain()
        stream_url_result = chain.get_file_stream_url(storage_file)
        if stream_url_result:
            stream_url, storage_headers = stream_url_result
            hosts = _configured_stream_hosts(None, include_media_servers=True)
            return _remote_stream_response(
                stream_url,
                range_header,
                hosts,
                extra_headers=storage_headers or None,
                head_only=head_only,
            )
        seekable_result = chain.open_file_stream(storage_file)
        if seekable_result:
            stream_handle, stream_size = seekable_result
            filename = storage_file.name or Path(storage_file.path or "media").name
            return _seekable_stream_response(
                stream_handle, stream_size, filename, range_header, head_only=head_only
            )
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="该存储暂不支持在线流式播放")
    if source_type == "file":
        return _file_stream_response(Path(source), range_header, head_only=head_only)
    return _remote_stream_response(str(source), range_header, configured_hosts, head_only=head_only)


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
    读取影片同目录外挂字幕或代理媒体服务器托管的外挂字幕流。

    :param itemid: 媒体服务器条目标识
    :param subtitle_id: 字幕文件名或 `ms:` 前缀的服务端字幕标识
    :param request: 当前请求，包含可选 Range 头
    :param server: 可选媒体服务器配置名，精确绑定搜索结果来源
    :param userinfo: 已验证的访问令牌
    :return: 字幕文件流式响应
    :raises HTTPException: 字幕不存在或不属于当前影片时返回 404
    """
    server_name, item, video_path = _resolve_stream_item(itemid, userinfo.sub, server=server)
    if subtitle_id.startswith("ms:") and server_name:
        upstream = MediaServerChain().get_subtitle_url(
            server=server_name, item_id=item.item_id or itemid, subtitle_id=subtitle_id
        )
        if not upstream:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到媒体服务器字幕")
        hosts = _configured_stream_hosts(video_path, include_media_servers=True)
        validated = _validate_stream_url(upstream, configured_hosts=hosts)
        return _remote_stream_response(validated, request.headers.get("range"), hosts)
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
    "/browse",
    summary="分页浏览媒体服务器媒体库内容",
    response_model=_SchemaResponse[_SchemaMediaServerBrowsePage],
)
def browse_library(
    server: str,
    library_id: Optional[str] = None,
    parent_id: Optional[str] = None,
    item_type: Optional[str] = None,
    genre: Optional[str] = None,
    year: Optional[int] = None,
    played_filter: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
    keyword: Optional[str] = None,
    start_index: int = Query(default=0, ge=0),
    limit: int = Query(default=30, ge=1, le=100),
    userinfo: _SchemaTokenPayload = Depends(verify_token),
) -> _SchemaResponse[_SchemaMediaServerBrowsePage]:
    """
    按统一契约分页浏览媒体服务器媒体库，支持筛选、排序和库内搜索。

    :param server: 媒体服务器配置名称
    :param library_id: 可选媒体库标识
    :param parent_id: 可选父目录标识
    :param item_type: 可选维度或媒体类型过滤
    :param genre: 可选风格过滤
    :param year: 可选年份过滤
    :param played_filter: 可选观看状态过滤（played / unplayed）
    :param sort_by: 排序字段（date_created / premiere_date / rating / name / random）
    :param sort_order: 排序方向（asc / desc）
    :param keyword: 可选关键词搜索
    :param start_index: 零基分页偏移
    :param limit: 单页数量上限
    :param userinfo: 已验证的访问令牌
    :return: 分页浏览结果
    """
    page = MediaServerChain().browse_items(
        server=server,
        library_id=library_id,
        parent_id=parent_id,
        item_type=item_type,
        genre=genre,
        year=year,
        played_filter=played_filter,
        sort_by=sort_by,
        sort_order=sort_order,
        keyword=keyword,
        start_index=start_index,
        limit=limit,
        username=userinfo.username,
    )
    if page is None:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="媒体服务器浏览请求失败")
    return _SchemaResponse(success=True, data=page)


@router.get(
    "/filters",
    summary="查询媒体库可用的筛选与排序选项",
    response_model=_SchemaResponse[_SchemaMediaServerFilterOptions],
)
def library_filters(
    server: str,
    library_id: Optional[str] = None,
    userinfo: _SchemaTokenPayload = Depends(verify_token),
) -> _SchemaResponse[_SchemaMediaServerFilterOptions]:
    """
    返回指定媒体服务器媒体库支持的风格、年份和排序键列表。

    :param server: 媒体服务器配置名称
    :param library_id: 可选媒体库标识
    :param userinfo: 已验证的访问令牌
    :return: 可用筛选项模型
    """
    options = MediaServerChain().filter_options(
        server=server, library_id=library_id, username=userinfo.username
    )
    return _SchemaResponse(
        success=True,
        data=options
        or _SchemaMediaServerFilterOptions(
            sort_options=["date_created", "premiere_date", "rating", "name"]
        ),
    )


@router.get(
    "/detail",
    summary="查询媒体服务器单个条目完整详情",
    response_model=_SchemaResponse[_SchemaMediaServerItemDetail],
)
def item_detail(
    itemid: str,
    server: Optional[str] = None,
    userinfo: _SchemaTokenPayload = Depends(verify_token),
) -> _SchemaResponse[_SchemaMediaServerItemDetail]:
    """
    返回电影、剧集、季、集或音乐条目的完整详情（含多版本与音轨字幕轨道）。

    :param itemid: 媒体服务器条目标识
    :param server: 可选媒体服务器配置名称；省略时依次查询已启用服务器
    :param userinfo: 已验证的访问令牌
    :return: 条目完整详情
    :raises HTTPException: 未找到条目时返回 404
    """
    chain = MediaServerChain()
    servers = [server] if server else [c.name for c in get_mediaserver_configs() if c.name]
    for server_name in servers:
        if not server_name:
            continue
        detail = chain.item_detail(server=server_name, item_id=itemid, username=userinfo.username)
        if detail:
            return _SchemaResponse(success=True, data=detail)
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到媒体服务器条目详情")


@router.get(
    "/seasons",
    summary="查询电视剧全部季卡片信息",
    response_model=_SchemaResponse[List[_SchemaMediaServerBrowseItem]],
)
def item_seasons(
    itemid: str,
    server: str,
    userinfo: _SchemaTokenPayload = Depends(verify_token),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> _SchemaResponse[List[_SchemaMediaServerBrowseItem]]:
    """
    按季号升序返回媒体服务器中指定电视剧的全部季信息。

    :param itemid: 电视剧在媒体服务器中的条目标识
    :param server: 媒体服务器配置名称
    :param userinfo: 已验证的访问令牌
    :param page: 兼容客户端的分页页码，由统一响应路由按需切片
    :param count: 兼容客户端的每页数量，由统一响应路由按需切片
    :return: 季卡片列表
    """
    _ = page, count
    seasons = MediaServerChain().seasons(
        server=server, item_id=itemid, username=userinfo.username
    )
    if seasons is None:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="媒体服务器季列表查询失败")
    return _SchemaResponse(success=True, data=seasons)


@router.get(
    "/season-episodes",
    summary="查询指定季的全部单集详情卡片",
    response_model=_SchemaResponse[List[_SchemaMediaServerBrowseItem]],
)
def item_season_episodes(
    itemid: str,
    server: str,
    season_id: Optional[str] = None,
    season: Optional[int] = Query(default=None, ge=0),
    userinfo: _SchemaTokenPayload = Depends(verify_token),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> _SchemaResponse[List[_SchemaMediaServerBrowseItem]]:
    """
    返回媒体服务器中指定电视剧单季的全部单集详情卡片（含剧照、时长、简介与播放状态）。

    :param itemid: 电视剧或专辑条目标识
    :param server: 媒体服务器配置名称
    :param season_id: 可选季条目标识，优先级高于 season
    :param season: 可选季号，0 表示特别篇
    :param userinfo: 已验证的访问令牌
    :param page: 兼容客户端的分页页码，由统一响应路由按需切片
    :param count: 兼容客户端的每页数量，由统一响应路由按需切片
    :return: 单集卡片列表
    """
    _ = page, count
    episodes = MediaServerChain().season_episodes(
        server=server,
        item_id=itemid,
        season_id=season_id,
        season=season,
        username=userinfo.username,
    )
    if episodes is None:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="媒体服务器分集列表查询失败")
    return _SchemaResponse(success=True, data=episodes)


def _coerce_progress_report_mode(raw_mode: Any) -> _SchemaMediaServerProgressReportMode:
    """
    将适配器返回的模式字符串规整为受契约约束的枚举字面量。
    """
    if raw_mode == "session":
        return "session"
    if raw_mode == "userdata":
        return "userdata"
    if raw_mode == "scrobble":
        return "scrobble"
    return "unsupported"


def _dispatch_progress_report(
    chain: MediaServerChain,
    payload: _SchemaMediaServerProgressReportRequest,
    *,
    username: Optional[str],
    session: Any,
    user_token: Optional[str],
    allow_userdata_write: bool,
) -> dict[str, Any]:
    """
    调用媒体服务器链执行进度上报，兼容仅实现旧布尔回执的测试桩与扩展适配器。
    """
    raw = chain.report_progress_detail(
        server=payload.server,
        item_id=payload.item_id,
        position_ms=payload.position_ms,
        media_source_id=payload.media_source_id or session.media_source_id,
        duration_ms=payload.duration_ms,
        is_paused=payload.is_paused,
        state=payload.state,
        username=username,
        play_session_id=session.upstream_play_session_id,
        event_name=payload.event_name,
        client_id=payload.client_id or session.client_id,
        device_name=payload.device_name or session.device_name,
        platform=payload.platform or session.platform,
        app_version=payload.app_version or session.app_version,
        play_method=payload.play_method or session.play_method,
        playback_time_ms=payload.playback_time_ms,
        audio_stream_index=payload.audio_stream_index,
        subtitle_stream_index=payload.subtitle_stream_index,
        user_token=user_token,
        started_reported=session.started_reported,
        allow_userdata_write=allow_userdata_write,
        scrobbled=session.scrobbled,
    )
    if isinstance(raw, dict):
        return raw
    # 兼容仅对 chain.report_progress 打桩的旧单测
    legacy_ok = bool(
        chain.report_progress(
            server=payload.server,
            item_id=payload.item_id,
            position_ms=payload.position_ms,
            media_source_id=payload.media_source_id,
            duration_ms=payload.duration_ms,
            is_paused=payload.is_paused,
            state=payload.state,
            username=username,
        )
    )
    return {
        "reported": legacy_ok,
        "mode": "session" if legacy_ok else "unsupported",
        "started_sent": legacy_ok and payload.state in {"started", "playing"},
        "scrobbled": False,
        "userdata_written": False,
        "token_invalid": False,
        "needs_account_bind": False,
        "detail": None,
    }


@router.post(
    "/progress",
    summary="回传原生播放器播放进度到媒体服务器",
    response_model=_SchemaResponse[_SchemaMediaServerProgressReportResult],
)
def report_progress(
    payload: _SchemaMediaServerProgressReportRequest,
    userinfo: _SchemaTokenPayload = Depends(verify_token),
) -> _SchemaResponse[_SchemaMediaServerProgressReportResult]:
    """
    将客户端当前播放位置与会话生命周期同步回源媒体服务器，更新正在播放、继续观看与已看标记。

    :param payload: 播放进度与会话生命周期请求体
    :param userinfo: 已验证的访问令牌
    :return: 包含同步模式与账号绑定建议的结构化回执
    """
    conf = _find_mediaserver_config(payload.server)
    server_type = (conf.type or "").strip().casefold() if conf and conf.type else ""
    raw_cred = get_user_mediaserver_credential(userinfo.username, payload.server, include_invalid=True)
    valid_cred = raw_cred if (raw_cred and not raw_cred.invalid and raw_cred.token) else None
    registry = get_playback_session_registry()
    session, should_skip, allow_userdata_write = registry.begin_progress_event(
        play_session_id=payload.play_session_id,
        user_id=userinfo.sub,
        username=userinfo.username,
        server=payload.server,
        server_type=server_type,
        item_id=payload.item_id,
        media_source_id=payload.media_source_id,
        state=payload.state,
        seq=payload.seq,
        client_id=payload.client_id,
        device_name=payload.device_name,
        platform=payload.platform,
        app_version=payload.app_version,
        play_method=payload.play_method,
    )
    if should_skip:
        sync_mode = resolve_server_sync_mode(server_type, valid_cred, conf.config if conf else None)
        report_mode = _coerce_progress_report_mode("unsupported" if sync_mode == "none" else sync_mode)
        return _SchemaResponse(
            success=True,
            data=_SchemaMediaServerProgressReportResult(
                reported=True,
                mode=report_mode,
                play_session_id=session.play_session_id,
                needs_account_bind=bool(raw_cred and raw_cred.invalid),
                detail="过期或重复的播放事件已忽略",
            ),
        )

    outcome = _dispatch_progress_report(
        MediaServerChain(),
        payload,
        username=userinfo.username,
        session=session,
        user_token=valid_cred.token if valid_cred else None,
        allow_userdata_write=allow_userdata_write,
    )
    if outcome.get("token_invalid") and userinfo.username:
        mark_user_mediaserver_credential_invalid(userinfo.username, payload.server)
    reported = bool(outcome.get("reported"))
    registry.record_progress_outcome(
        session,
        state=payload.state,
        seq=payload.seq,
        reported=reported,
        started_sent=bool(outcome.get("started_sent")),
        scrobbled=bool(outcome.get("scrobbled")),
        userdata_written=bool(outcome.get("userdata_written")),
    )
    mode = _coerce_progress_report_mode(outcome.get("mode"))
    needs_bind = bool(outcome.get("needs_account_bind") or outcome.get("token_invalid") or (raw_cred and raw_cred.invalid))
    detail_msg = str(outcome.get("detail") or "") or (
        "媒体服务器账号凭据已失效，请在设置中重新绑定以恢复实时播放会话"
        if outcome.get("token_invalid")
        else None
    )
    result = _SchemaMediaServerProgressReportResult(
        reported=reported,
        mode=mode,
        play_session_id=session.play_session_id,
        needs_account_bind=needs_bind,
        detail=detail_msg,
    )
    return _SchemaResponse(
        success=reported,
        message="" if reported else (detail_msg or "当前媒体服务器未记录播放进度"),
        data=result,
    )


@router.get(
    "/account-bindings",
    summary="查询当前用户的媒体服务器账号绑定状态",
    response_model=_SchemaResponse[List[_SchemaMediaServerAccountBinding]],
)
def list_account_bindings(
    userinfo: _SchemaTokenPayload = Depends(verify_token),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> _SchemaResponse[List[_SchemaMediaServerAccountBinding]]:
    """
    返回当前登录用户在全部已启用媒体服务器上的账号绑定状态与播放同步模式。

    :param userinfo: 已验证的访问令牌
    :param page: 兼容客户端的分页页码，由统一响应路由按需切片
    :param count: 兼容客户端的每页数量，由统一响应路由按需切片
    :return: 不含任何敏感令牌或密码的绑定状态列表
    """
    _ = page, count
    if not userinfo.username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="当前登录身份缺少用户名")
    bindings = list_user_mediaserver_bindings(userinfo.username, get_mediaserver_configs())
    return _SchemaResponse(success=True, data=bindings)


@router.post(
    "/account-bindings",
    summary="绑定当前用户的媒体服务器账号以启用实时播放会话",
    response_model=_SchemaResponse[_SchemaMediaServerAccountBinding],
)
def bind_account(
    payload: _SchemaMediaServerAccountBindRequest,
    userinfo: _SchemaTokenPayload = Depends(verify_token),
) -> _SchemaResponse[_SchemaMediaServerAccountBinding]:
    """
    一次性验证用户提供的媒体服务器账号密码或 Plex 访问令牌，并加密持久化用户级令牌。

    :param payload: 媒体服务器绑定请求体（密码仅用于本次换取令牌，绝不落库）
    :param userinfo: 已验证的访问令牌
    :return: 绑定后的脱敏状态摘要
    :raises HTTPException: 服务器不存在、类型不支持绑定、输入不完整或媒体服务器认证失败时
    """
    if not userinfo.username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="当前登录身份缺少用户名")
    configs = get_mediaserver_configs()
    conf = next((item for item in configs if item.enabled and item.name == payload.server), None)
    if not conf or not conf.type:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到对应的已启用媒体服务器")
    server_type = conf.type.strip().casefold()
    if server_type not in BINDABLE_MEDIA_SERVER_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="当前媒体服务器无需单独绑定账号")

    clean_token = (payload.token or "").strip()
    clean_username = (payload.username or "").strip()
    clean_password = payload.password or ""
    if clean_token:
        if server_type != "plex":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="仅 Plex 支持直接提交访问令牌绑定")
        auth_result = MediaServerChain().authenticate_user(
            server=payload.server,
            username=clean_username or userinfo.username,
            token=clean_token,
        )
    else:
        if not clean_username or not clean_password:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="请填写媒体服务器用户名和密码")
        auth_result = MediaServerChain().authenticate_user(
            server=payload.server,
            username=clean_username,
            password=clean_password,
        )
    if not auth_result or not auth_result.token:
        # MoviePilot 会话本身有效，此处为远端媒体服务器凭据校验失败，返回 400 避免前端/App 误判登录失效退出
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="媒体服务器账号验证失败，请核对凭据后重试")

    save_user_mediaserver_credential(
        username=userinfo.username,
        server=payload.server,
        token=auth_result.token,
        remote_username=auth_result.username or clean_username or userinfo.username,
        source="manual",
    )
    bindings = list_user_mediaserver_bindings(userinfo.username, configs)
    updated = next((item for item in bindings if item.server == payload.server), None)
    if not updated:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="保存媒体服务器账号绑定失败")
    return _SchemaResponse(success=True, message="媒体服务器账号绑定成功", data=updated)


@router.delete(
    "/account-bindings/{server}",
    summary="解绑当前用户的媒体服务器账号",
    response_model=_SchemaResponse[None],
)
def unbind_account(
    server: str,
    userinfo: _SchemaTokenPayload = Depends(verify_token),
) -> _SchemaResponse[None]:
    """
    清除当前用户在指定媒体服务器上绑定的访问令牌，恢复为服务器默认同步模式。

    :param server: 目标媒体服务器配置名称
    :param userinfo: 已验证的访问令牌
    :return: 操作结果响应
    """
    if not userinfo.username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="当前登录身份缺少用户名")
    removed = delete_user_mediaserver_credential(userinfo.username, server)
    return _SchemaResponse(
        success=True,
        message="已解除媒体服务器账号绑定" if removed else "当前服务器未绑定账号",
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
