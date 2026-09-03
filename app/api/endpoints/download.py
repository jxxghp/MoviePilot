from typing import Annotated, Any, List, Optional, Union

import anyio
from fastapi import Body, Depends

from app.adapters.web.security.access import verify_token
from app.api.dependencies.auth import get_current_active_user
from app.api.dependencies.site import get_site_sync_query_service
from app.api.principal import ApiPrincipal
from app.api.response import (
    CompatibleCountParam,
    CompatiblePageParam,
    ResponseAPIRouter,
)
from app.application.configuration import get_configured_system_config
from app.application.directory import DirectoryHelper
from app.application.download.tasks import DownloadTaskMutationService
from app.application.security.url import SecurityUtils
from app.application.site.query import (
    SiteQueryService,
    get_configured_site_query_service,
)
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.domain.context import Context, MediaInfo, MusicInfo, SubtitleInfo, TorrentInfo
from app.domain.media import is_music_media_source, normalize_music_type
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfo
from app.schemas.common import ServiceClientInfo as _SchemaServiceClientInfo
from app.schemas.download import DownloadAddedData as _SchemaDownloadAddedData
from app.schemas.download import DownloadDirectory as _SchemaDownloadDirectory
from app.schemas.download import DownloadTaskUpdateData as _SchemaDownloadTaskUpdateData
from app.schemas.download import DownloadTaskUpdateRequest as _SchemaDownloadTaskUpdateRequest
from app.schemas.download import SubtitleDownloadData as _SchemaSubtitleDownloadData
from app.schemas.file import FileURI as _SchemaFileURI
from app.schemas.response import Response as _SchemaResponse
from app.schemas.search import SubtitleInfo as _SchemaSubtitleInfo
from app.schemas.system import TorrentInfo as _SchemaTorrentInfo
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.schemas.transfer import DownloaderTorrent as _SchemaDownloaderTorrent
from app.schemas.transfer import MusicInfo as _SchemaMusicInfo
from app.schemas.types import (
    MUSIC_ENTITY_RECORDING,
    MediaSource,
    MediaType,
    MusicTargetEntityType,
    SystemConfigKey,
)
from app.schemas.workflow import MediaInfo as _SchemaMediaInfo

router = ResponseAPIRouter()


def _prepare_subtitle_download(
    subtitle: SubtitleInfo,
    query: SiteQueryService | None = None,
) -> tuple[bool, str]:
    """
    校验字幕下载签名，并用服务端站点配置覆盖请求凭据。
    """
    if subtitle.site is None:
        return False, "字幕站点信息为空"

    clean_url = SecurityUtils.verify_signed_url(
        subtitle.enclosure,
        purpose=SecurityUtils.subtitle_download_purpose(subtitle.site),
    )
    if not clean_url:
        return False, "字幕下载链接签名无效"

    site_query = query or get_configured_site_query_service()
    site = site_query.get_sync(subtitle.site)
    if not site:
        return False, "字幕站点信息不存在"

    subtitle.enclosure = clean_url
    subtitle.site_cookie = site.cookie
    subtitle.site_ua = site.ua
    subtitle.site_proxy = bool(site.proxy)
    return True, ""


def _build_unrecognized_media_info(
    torrent: _SchemaTorrentInfo,
    metainfo: MetaBase,
    is_music: bool = False,
    music_type: Optional[str] = None,
) -> MediaInfo | MusicInfo:
    """
    为用户确认的未识别资源构造最小下载上下文，影视与音乐统一处理。

    影视以种子分类兜底媒体类型并保留标题年份，音乐按解析标题构造音乐信息，
    两者都不再要求识别出统一媒体信息即可继续下载。
    """
    if is_music:
        return MusicInfo(
            title=metainfo.title or torrent.title,
            year=metainfo.year,
            music_type=music_type or MUSIC_ENTITY_RECORDING,
        )
    try:
        media_type = MediaType(torrent.category)
    except (TypeError, ValueError):
        media_type = MediaType.from_agent(torrent.category)
    if media_type == MediaType.COLLECTION:
        media_type = MediaType.MOVIE
    if media_type not in (MediaType.MOVIE, MediaType.TV):
        media_type = metainfo.type
        # 合集类型在回退到元数据后同样归一为电影，避免落到 UNKNOWN
        if media_type == MediaType.COLLECTION:
            media_type = MediaType.MOVIE
    if media_type not in (MediaType.MOVIE, MediaType.TV):
        media_type = MediaType.UNKNOWN
    return MediaInfo(
        type=media_type,
        title=metainfo.name or torrent.title,
        year=metainfo.year,
    )


def _resolve_add_media(
    torrent_in: _SchemaTorrentInfo,
    media_source: MediaSource | None,
    media_id: str | None,
    music_type: MusicTargetEntityType | None,
    allow_unrecognized: bool,
) -> tuple[MetaBase | None, MediaInfo | MusicInfo | None, _SchemaResponse | None]:
    """校验媒体身份并为无媒体信息下载构建识别上下文。"""
    normalized_music_type = normalize_music_type(music_type, allow_artist=False)
    if music_type is not None and not normalized_music_type:
        return (
            None,
            None,
            _SchemaResponse(
                success=False,
                message="音乐实体类型无效，仅支持 recording 或 album",
            ),
        )
    if (media_source is None) != (media_id is None):
        return (
            None,
            None,
            _SchemaResponse(
                success=False,
                message="媒体来源和媒体 ID 必须同时提供",
            ),
        )
    is_music = (
        torrent_in.category in (MediaType.MUSIC, MediaType.MUSIC.value, "music")
        or is_music_media_source(media_source)
        or normalized_music_type is not None
    )
    if is_music and media_source and not is_music_media_source(media_source):
        return (
            None,
            None,
            _SchemaResponse(
                success=False,
                message="音乐下载只能使用音乐元数据源",
            ),
        )
    if is_music and not normalized_music_type:
        normalized_music_type = MUSIC_ENTITY_RECORDING
    metainfo = (
        MetaMusic.parse_query(torrent_in.title)
        if is_music
        else MetaInfo(title=torrent_in.title, subtitle=torrent_in.description)
    )
    if media_source and media_id:
        mediainfo = MediaChain().recognize_media(
            meta=metainfo,
            media_source=media_source,
            media_id=media_id,
            mtype=MediaType.MUSIC if is_music else None,
            music_type=normalized_music_type,
        )
    else:
        mediainfo = MediaChain().recognize_by_meta(
            metainfo,
            media_source=media_source,
            obtain_images=False,
            mtype=MediaType.MUSIC if is_music else None,
            music_type=normalized_music_type,
        )
    if mediainfo:
        return metainfo, mediainfo, None
    if not allow_unrecognized:
        return (
            metainfo,
            None,
            _SchemaResponse(
                success=False,
                message="无法识别媒体信息",
                data=_SchemaDownloadAddedData(requires_confirmation=True),
            ),
        )
    return (
        metainfo,
        _build_unrecognized_media_info(
            torrent_in,
            metainfo,
            is_music=is_music,
            music_type=normalized_music_type,
        ),
        None,
    )


@router.get("/", summary="正在下载", response_model=List[_SchemaDownloaderTorrent])
def current(name: Optional[str] = None, _: _SchemaTokenPayload = Depends(verify_token), page: CompatiblePageParam = None, count: CompatibleCountParam = None) -> Any:
    """
    查询正在下载的任务
    """
    return DownloadChain().downloading(name)


@router.post(
    "/",
    summary="添加下载（含媒体信息）",
    response_model=_SchemaResponse[_SchemaDownloadAddedData],
)
def download(
    media_in: Union[_SchemaMusicInfo, _SchemaMediaInfo],
    torrent_in: _SchemaTorrentInfo,
    downloader: Annotated[str | None, Body()] = None,
    save_path: Annotated[str | None, Body()] = None,
    current_user: ApiPrincipal = Depends(get_current_active_user),
) -> Any:
    """
    添加下载任务（含媒体信息）
    """
    if isinstance(media_in, _SchemaMusicInfo):
        mediainfo = MusicInfo.from_dict(media_in.model_dump())
        metainfo = MetaMusic.from_music_info(mediainfo)
        metainfo.org_string = torrent_in.title
    else:
        metainfo = MetaInfo(title=torrent_in.title, subtitle=torrent_in.description)
        mediainfo = MediaInfo()
        mediainfo.from_dict(media_in.model_dump())
    # 种子信息
    torrentinfo = TorrentInfo()
    torrentinfo.from_dict(torrent_in.model_dump())
    # 手动下载始终使用选择的下载器
    torrentinfo.site_downloader = downloader
    # 上下文
    context = Context(meta_info=metainfo, media_info=mediainfo, torrent_info=torrentinfo)
    did = DownloadChain().download_single(
        context=context,
        username=current_user.name,
        save_path=save_path,
        source="Manual",
    )
    if not did:
        return _SchemaResponse(success=False, message="任务添加失败")
    return _SchemaResponse(success=True, data={"download_id": did})


@router.post(
    "/add",
    summary="添加下载（不含媒体信息）",
    response_model=_SchemaResponse[_SchemaDownloadAddedData],
)
def add(
    torrent_in: _SchemaTorrentInfo,
    media_source: Annotated[MediaSource | None, Body()] = None,
    media_id: Annotated[str | None, Body()] = None,
    music_type: Annotated[MusicTargetEntityType | None, Body()] = None,
    allow_unrecognized: Annotated[bool, Body()] = False,
    downloader: Annotated[str | None, Body()] = None,
    # 保存路径, 支持<storage>:<path>, 如rclone:/MP, smb:/server/share/Movies等
    save_path: Annotated[str | None, Body()] = None,
    current_user: ApiPrincipal = Depends(get_current_active_user),
) -> Any:
    """
    添加下载任务（不含媒体信息）
    """
    metainfo, mediainfo, error = _resolve_add_media(
        torrent_in,
        media_source,
        media_id,
        music_type,
        allow_unrecognized,
    )
    if error:
        return error
    if metainfo is None or mediainfo is None:
        return _SchemaResponse(success=False, message="无法识别媒体信息")
    # 种子信息
    torrentinfo = TorrentInfo()
    torrentinfo.from_dict(torrent_in.model_dump())
    # 上下文
    context = Context(meta_info=metainfo, media_info=mediainfo, torrent_info=torrentinfo)

    did = DownloadChain().download_single(
        context=context,
        username=current_user.name,
        downloader=downloader,
        save_path=save_path,
        source="Manual",
    )
    if not did:
        return _SchemaResponse(success=False, message="任务添加失败")
    return _SchemaResponse(success=True, data={"download_id": did})


@router.post(
    "/subtitle",
    summary="下载字幕",
    response_model=_SchemaResponse[_SchemaSubtitleDownloadData],
)
def download_subtitle(
    subtitle_in: _SchemaSubtitleInfo,
    media_source: Annotated[MediaSource, Body()],
    media_id: Annotated[str, Body()],
    save_path: Annotated[str | None, Body()] = None,
    current_user: ApiPrincipal = Depends(get_current_active_user),
    query: SiteQueryService = Depends(get_site_sync_query_service),
) -> Any:
    """
    下载字幕资源。
    """
    subtitle_info = SubtitleInfo()
    subtitle_info.from_dict(subtitle_in.model_dump())
    # 直接调用 endpoint 的旧测试/插件入口不会经过 FastAPI 依赖解析；此时让
    # 应用查询端口自行提供服务，仍保留真实请求中的注入对象。
    if not hasattr(query, "get_sync"):
        valid, message = _prepare_subtitle_download(subtitle_info)
    else:
        valid, message = _prepare_subtitle_download(subtitle_info, query)
    if not valid:
        return _SchemaResponse(success=False, message=message)

    success, message, saved_files = DownloadChain().download_subtitle(
        subtitle=subtitle_info,
        media_source=media_source,
        media_id=media_id,
        save_path=save_path,
        username=current_user.name,
    )
    return _SchemaResponse(
        success=success,
        message=message,
        data={"files": saved_files} if saved_files else None,
    )


@router.get("/start/{hashString}", summary="开始任务", response_model=_SchemaResponse[None])
def start(
    hashString: str,
    name: Optional[str] = None,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    开如下载任务
    """
    ret = DownloadChain().set_downloading(hashString, "start", name=name)
    return _SchemaResponse(success=True if ret else False)


@router.get("/stop/{hashString}", summary="暂停任务", response_model=_SchemaResponse[None])
def stop(
    hashString: str,
    name: Optional[str] = None,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    暂停下载任务
    """
    ret = DownloadChain().set_downloading(hashString, "stop", name=name)
    return _SchemaResponse(success=True if ret else False)


@router.patch(  # type: ignore[misc]
    "/{hashString}",
    summary="高级更新下载任务",
    response_model=_SchemaResponse[_SchemaDownloadTaskUpdateData],
)
async def update_task(
    hashString: str,
    payload: _SchemaDownloadTaskUpdateRequest,
    _: ApiPrincipal = Depends(get_current_active_user),
) -> _SchemaResponse[Any]:
    """执行下载任务启停、标签、限速、Tracker 和保存位置修改。"""
    chain = DownloadChain()
    service = DownloadTaskMutationService(
        list_torrents=chain.list_torrents,
        set_tags=chain.set_torrents_tag,
        set_downloading=chain.set_downloading,
        update_torrent=chain.update_torrent,
    )
    try:
        data = await anyio.to_thread.run_sync(
            lambda: service.update(
                hash_value=hashString,
                **payload.model_dump(),
            )
        )
    except ValueError as error:
        return _SchemaResponse(success=False, message=str(error))
    return _SchemaResponse(
        success=all(item.get("success") for item in data["results"]),
        data=data,
    )


@router.get(
    "/clients",
    summary="查询可用下载器",
    response_model=List[_SchemaServiceClientInfo],
)
async def clients(_: _SchemaTokenPayload = Depends(verify_token), page: CompatiblePageParam = None, count: CompatibleCountParam = None) -> Any:
    """
    查询可用下载器
    """
    downloaders: List[dict] = get_configured_system_config().get(SystemConfigKey.Downloaders)
    if downloaders:
        return [{"name": d.get("name"), "type": d.get("type")} for d in downloaders if d.get("enabled")]
    return []


@router.get("/paths", summary="查询可用下载路径", response_model=List[_SchemaDownloadDirectory])
def paths(_: _SchemaTokenPayload = Depends(verify_token), page: CompatiblePageParam = None, count: CompatibleCountParam = None) -> Any:
    """
    查询可直接用于下载接口 save_path 参数的下载路径
    """
    return [
        _SchemaDownloadDirectory(
            name=dir_info.name,
            storage=dir_info.storage or "local",
            download_path=dir_info.download_path,
            save_path=_SchemaFileURI(
                storage=dir_info.storage or "local",
                path=dir_info.download_path,
            ).uri,
            priority=dir_info.priority,
            media_type=dir_info.media_type,
            media_category=dir_info.media_category,
            media_category_id=dir_info.media_category_id,
        )
        for dir_info in DirectoryHelper().get_download_dirs()
        if dir_info.download_path
    ]


@router.delete("/{hashString}", summary="删除下载任务", response_model=_SchemaResponse[None])
def delete(
    hashString: str,
    name: Optional[str] = None,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    删除下载任务
    """
    ret = DownloadChain().remove_downloading(hashString, name=name)
    return _SchemaResponse(success=True if ret else False)
