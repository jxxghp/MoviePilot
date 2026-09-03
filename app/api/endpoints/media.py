from pathlib import Path
from typing import Annotated, Any, List, Optional, Union
from uuid import UUID

from fastapi import Depends, Query
from pydantic import BeforeValidator

from app.adapters.web.security.access import verify_apitoken, verify_token
from app.api.context import get_classification_runtime
from app.api.dependencies.auth import get_current_active_user
from app.api.response import (
    CompatibleCountParam,
    CompatiblePageParam,
    ResponseAPIRouter,
)
from app.application.classification.runtime import ClassificationRuntime
from app.application.configuration import get_api_runtime_config_snapshot
from app.chain.media import MediaChain
from app.chain.scraping import ScrapingChain
from app.chain.tmdb import TmdbChain
from app.domain.context import Context, MusicInfo
from app.domain.media import is_music_media_source, normalize_music_type, parse_media_source_selection
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfo, MetaInfoPath
from app.schemas.category import CategoryConfig as _SchemaCategoryConfig
from app.schemas.category import MediaCategoryMap as _SchemaMediaCategoryMap
from app.schemas.context import MediaEpisodeGroup as _SchemaMediaEpisodeGroup
from app.schemas.context import MediaPerson as _SchemaMediaPerson
from app.schemas.context import MediaSearchResults as _SchemaMediaSearchResults
from app.schemas.context import MediaSeason as _SchemaMediaSeason
from app.schemas.event import MediaSourceInfo as _SchemaMediaSourceInfo
from app.schemas.media import normalize_media_source, resolve_media_identity
from app.schemas.response import Response as _SchemaResponse
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.schemas.types import MUSIC_ENTITY_RECORDING, MediaSource, MediaType
from app.schemas.workflow import Context as _SchemaContext
from app.schemas.workflow import FileItem as _SchemaFileItem
from app.schemas.workflow import MediaInfo as _SchemaMediaInfo

router = ResponseAPIRouter()


_BUILTIN_MEDIA_SOURCES = (
    _SchemaMediaSourceInfo(name="TheMovieDb", media_source=MediaSource.TMDB),
    _SchemaMediaSourceInfo(name="豆瓣", media_source=MediaSource.Douban),
    _SchemaMediaSourceInfo(name="Bangumi", media_source=MediaSource.Bangumi),
    _SchemaMediaSourceInfo(name="AniList", media_source=MediaSource.AniList),
    _SchemaMediaSourceInfo(name="IMDb", media_source=MediaSource.IMDb),
    _SchemaMediaSourceInfo(name="TVDB", media_source=MediaSource.TVDB),
    _SchemaMediaSourceInfo(
        name="MusicBrainz",
        media_source=MediaSource.MusicBrainz,
        media_types=[MediaType.MUSIC],
    ),
    _SchemaMediaSourceInfo(
        name="TheAudioDB",
        media_source=MediaSource.TheAudioDB,
        media_types=[MediaType.MUSIC],
    ),
    _SchemaMediaSourceInfo(
        name="豆瓣音乐",
        media_source=MediaSource.DoubanMusic,
        media_types=[MediaType.MUSIC],
    ),
    _SchemaMediaSourceInfo(name="哔哩哔哩", media_source=MediaSource.Bilibili),
    _SchemaMediaSourceInfo(name="芒果TV", media_source=MediaSource.MangoTV),
    _SchemaMediaSourceInfo(name="咪咕视频", media_source=MediaSource.MiguVideo),
    _SchemaMediaSourceInfo(name="腾讯视频", media_source=MediaSource.TencentVideo),
    _SchemaMediaSourceInfo(name="爱奇艺", media_source=MediaSource.Iqiyi),
)


def _registered_media_sources() -> list[_SchemaMediaSourceInfo]:
    """合并内置与启用插件声明的媒体来源，并按来源标识去重。"""
    from app.application.plugin.runtime import get_plugin_manager

    result = list(_BUILTIN_MEDIA_SOURCES)
    seen = {source.media_source for source in result}
    for raw_source in get_plugin_manager().get_media_sources():
        try:
            source = _SchemaMediaSourceInfo.model_validate(raw_source)
        except Exception:
            continue
        if source.media_source in seen:
            continue
        result.append(source)
        seen.add(source.media_source)
    return result


def _split_media_source_query(value: object) -> tuple[str, ...]:
    """展开重复或逗号分隔的来源参数，并在枚举校验前规范历史别名。"""
    if value in (None, ""):
        return ()
    values = value if isinstance(value, (list, tuple)) else (value,)
    sources = tuple(source.strip() for item in values for source in str(item).split(",") if source.strip())
    return tuple(normalized.value if (normalized := normalize_media_source(source)) else source for source in sources)


MediaSourceQuery = Annotated[
    tuple[MediaSource, ...],
    BeforeValidator(_split_media_source_query),
    Query(),
]


def _is_valid_source_media_id(
    media_source: Optional[MediaSource],
    media_id: str,
) -> bool:
    """按媒体数据源校验原生 ID，并兼容豆瓣音乐的曲目复合 ID。"""
    normalized_source, normalized_media_id = resolve_media_identity(
        media_source=media_source,
        media_id=media_id,
    )
    if not normalized_source or not normalized_media_id:
        return False
    if normalized_source == MediaSource.MusicBrainz:
        try:
            UUID(normalized_media_id)
            return True
        except TypeError, ValueError:
            return False
    if normalized_source == MediaSource.DoubanMusic and ":" in normalized_media_id:
        album_id, track_number = normalized_media_id.split(":", 1)
        return album_id.isdigit() and track_number.isdigit()
    if normalized_source == MediaSource.IMDb:
        return normalized_media_id.startswith("tt") and normalized_media_id[2:].isdigit()
    return True


def _build_recognize_metainfo(
    title: str,
    subtitle: Optional[str] = None,
    custom_words: Optional[str] = None,
) -> MetaBase:
    """构造标题识别元数据，并兼容第三方客户端传入媒体文件路径。"""
    custom_word_list = custom_words.split("\n") if custom_words else None
    normalized_title = title.replace("\\", "/")
    title_path = Path(normalized_title)
    if (
        ("/" in title or "\\" in title)
        and "://" not in title
        and title_path.suffix.lower() in get_api_runtime_config_snapshot().media_extensions
    ):
        metainfo = MetaInfoPath(
            title_path,
            custom_words=custom_word_list,
        )
        metainfo.title = title
        return metainfo
    return MetaInfo(title, subtitle, custom_words=custom_word_list)


def _build_media_seasons(
    mediainfo: Any,
    season: Optional[int] = None,
) -> List[_SchemaMediaSeason]:
    """将任意数据源的统一媒体信息转换为季信息响应。"""
    seasons_info = []
    for item in mediainfo.season_info or []:
        season_number = item.get("season_number")
        if season is not None and season_number != season:
            continue
        seasons_info.append(
            _SchemaMediaSeason(
                air_date=item.get("air_date"),
                episode_count=item.get("episode_count"),
                name=item.get("name"),
                overview=item.get("overview"),
                poster_path=item.get("poster_path") or mediainfo.poster_path,
                season_number=season_number,
                vote_average=item.get("vote_average"),
            )
        )
    if seasons_info:
        return seasons_info

    season_numbers = sorted((mediainfo.seasons or {}).keys())
    if season is not None:
        season_numbers = [season]
    elif not season_numbers:
        season_numbers = [mediainfo.season or 1]
    return [
        _SchemaMediaSeason(
            season_number=season_number,
            poster_path=mediainfo.poster_path,
            name=f"第 {season_number} 季",
            air_date=mediainfo.release_date,
            overview=mediainfo.overview,
            vote_average=mediainfo.vote_average,
            episode_count=(len((mediainfo.seasons or {}).get(season_number) or []) or mediainfo.number_of_episodes),
        )
        for season_number in season_numbers
    ]


@router.get("/recognize", summary="识别媒体信息（种子）", response_model=_SchemaContext)
async def recognize(
    title: str,
    subtitle: Optional[str] = None,
    custom_words: Optional[str] = None,
    media_source: Optional[MediaSource] = None,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    根据标题、副标题识别媒体信息
    :param title: 标题
    :param subtitle: 副标题
    :param custom_words: 临时识别词（每行一条规则），传入时仅在本次识别中生效，不会保存到系统配置
    :param media_source: 请求级识别数据源
    :param _:
    """
    # 识别媒体信息，传入临时识别词时优先于系统配置的识别词生效
    metainfo = _build_recognize_metainfo(title, subtitle, custom_words)
    # 显式音乐来源需要按音乐元数据解析，避免名称测试误入影视识别。
    if is_music_media_source(media_source) and not isinstance(metainfo, MetaMusic):
        metainfo = MetaMusic.parse_query(title)
    mediainfo = await MediaChain().async_recognize_by_meta(
        metainfo,
        media_source=media_source,
    )
    if mediainfo:
        return Context(meta_info=metainfo, media_info=mediainfo).to_dict()
    return _SchemaContext()


@router.get(
    "/recognize2",
    summary="识别种子媒体信息（API_TOKEN）",
    response_model=_SchemaContext,
)
async def recognize2(
    _: Annotated[str, Depends(verify_apitoken)],
    title: str,
    subtitle: Optional[str] = None,
    custom_words: Optional[str] = None,
    media_source: Optional[MediaSource] = None,
) -> Any:
    """
    根据标题、副标题识别媒体信息 API_TOKEN认证（?token=xxx）
    """
    # 识别媒体信息
    return await recognize(title, subtitle, custom_words, media_source)


@router.get("/recognize_file", summary="识别媒体信息（文件）", response_model=_SchemaContext)
async def recognize_file(
    path: str,
    media_source: Optional[MediaSource] = None,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    根据文件路径识别媒体信息，影视与音乐统一走媒体链路径识别入口
    """
    # 识别媒体信息
    context = await MediaChain().async_recognize_by_path(path, media_source=media_source)
    if context:
        return context.to_dict()
    return _SchemaContext()


@router.get(
    "/recognize_file2",
    summary="识别文件媒体信息（API_TOKEN）",
    response_model=_SchemaContext,
)
async def recognize_file2(
    path: str,
    _: Annotated[str, Depends(verify_apitoken)],
    media_source: Optional[MediaSource] = None,
) -> Any:
    """
    根据文件路径识别媒体信息 API_TOKEN认证（?token=xxx）
    """
    # 识别媒体信息
    return await recognize_file(path, media_source)


@router.get(
    "/search",
    summary="搜索媒体/人物信息",
    response_model=_SchemaMediaSearchResults,
)
async def search(
    title: str,
    type: Optional[str] = "media",
    page: int = 1,
    count: int = 8,
    media_source: MediaSourceQuery = (),
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    模糊搜索媒体、合集、人物或音乐信息列表。

    :param title: 搜索关键词
    :param type: 搜索类型，支持 media、music、collection、person
    :param page: 页码
    :param count: 每页数量
    :param media_source: 请求级搜索数据源枚举；可重复传入，逗号格式仅用于兼容旧客户端
    :param _: Token校验
    :return: 搜索结果列表
    """

    def __get_source(obj: Union[_SchemaMediaInfo, _SchemaMediaPerson, dict]):
        """
        获取对象属性
        """
        if isinstance(obj, dict):
            return obj.get("media_source")
        return obj.media_source

    # 直接函数调用也可能绕过 FastAPI/Pydantic，仅在该测试与内部兼容边界补一次规范化。
    selected_sources = (
        media_source
        if isinstance(media_source, tuple) and all(isinstance(source, MediaSource) for source in media_source)
        else parse_media_source_selection(",".join(_split_media_source_query(media_source)))
    )
    selected_sources = tuple(dict.fromkeys(selected_sources))
    source_selection = selected_sources or None

    media_chain = MediaChain()
    if type == "music" or any(is_music_media_source(source) for source in selected_sources):
        # 音乐搜索统一入口，与影视搜索共用 /media/search
        music_search_params = {"query": title, "limit": count}
        # 未指定来源时由 MediaChain 使用默认 MusicBrainz 来源。
        if source_selection:
            music_search_params["media_source"] = source_selection
        music_infos = await media_chain.async_search_music(**music_search_params)
        return [info.to_dict() for info in music_infos] if music_infos else []
    if type == "media":
        _, medias = await media_chain.async_search(title=title, media_source=source_selection)
        result = [media.to_dict() for media in medias] if medias else []
    elif type == "collection":
        collections = await media_chain.async_search_collections(name=title, media_source=source_selection)
        result = [collection.to_dict() for collection in collections] if collections else []
    else:  # person
        persons = await media_chain.async_search_persons(name=title, media_source=source_selection)
        result = [person.model_dump() for person in persons] if persons else []

    if not result:
        return []

    # 排序和分页
    search_source = get_api_runtime_config_snapshot().search_source
    setting_order = search_source.split(",") if search_source else []
    sort_order = {source: index for index, source in enumerate(setting_order)}

    sorted_result = sorted(result, key=lambda x: sort_order.get(__get_source(x), 4))
    return sorted_result[(page - 1) * count : page * count]


@router.get(
    "/source",
    summary="获取媒体数据源",
    response_model=list[_SchemaMediaSourceInfo],
)
def source(
    _: _SchemaTokenPayload = Depends(verify_token), page: CompatiblePageParam = None, count: CompatibleCountParam = None
) -> list[_SchemaMediaSourceInfo]:
    """返回内置及启用插件注册的媒体数据源，供前端统一构造来源选项。"""
    return _registered_media_sources()


def _scrape_impl(
    fileitem: _SchemaFileItem,
    storage: Optional[str] = "local",
    media_source: Optional[MediaSource] = None,
    media_id: Optional[str] = None,
    type_name: Optional[MediaType] = None,
    music_type: Optional[str] = None,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    刮削媒体信息，可按请求指定媒体数据源及其原生ID

    :param fileitem: 待刮削文件项
    :param storage: 文件所在存储
    :param media_source: 请求级媒体数据源
    :param media_id: 数据源原生ID
    :param type_name: 媒体类型
    :param music_type: 音乐实体类型，支持 recording 和 album
    :param _: Token校验
    """
    if not fileitem or not fileitem.path:
        return _SchemaResponse(success=False, message="刮削路径无效")
    has_explicit_media_id = media_id is not None
    normalized_media_id = str(media_id).strip() if has_explicit_media_id else None
    if has_explicit_media_id and not normalized_media_id:
        return _SchemaResponse(success=False, message="媒体ID格式无效")
    if normalized_media_id and not media_source:
        return _SchemaResponse(success=False, message="指定媒体ID时必须同时指定媒体数据源")
    if normalized_media_id and not _is_valid_source_media_id(media_source, normalized_media_id):
        return _SchemaResponse(success=False, message="媒体ID格式无效")

    is_music = (
        type_name == MediaType.MUSIC or is_music_media_source(media_source) or MediaChain.is_audio_path(fileitem.path)
    )
    if is_music:
        if type_name not in (None, MediaType.MUSIC):
            return _SchemaResponse(success=False, message="音乐元数据源只能用于音乐刮削")
        music_info: Optional[MusicInfo] = None
        if normalized_media_id:
            normalized_music_type = normalize_music_type(
                music_type or MUSIC_ENTITY_RECORDING,
                allow_artist=False,
            )
            if not normalized_music_type:
                return _SchemaResponse(
                    success=False,
                    message="音乐实体类型无效，仅支持 recording 或 album",
                )
            # 音乐与影视共用统一识别入口，按媒体源和原生 ID 恢复音乐详情
            music_info = MediaChain().recognize_media(
                media_source=media_source or MediaSource.MusicBrainz,
                media_id=normalized_media_id,
                mtype=MediaType.MUSIC,
                music_type=normalized_music_type,
            )
            if not music_info:
                return _SchemaResponse(success=False, message="刮削失败，无法识别音乐信息")
        success, message = ScrapingChain().scrape_music_metadata(
            fileitem=fileitem,
            mediainfo=music_info,
            overwrite=True,
            media_source=media_source,
        )
        return _SchemaResponse(success=success, message=message)

    chain = MediaChain()
    if normalized_media_id:
        meta_info = MetaInfoPath(Path(fileitem.path))
        media_info = chain.recognize_media(
            meta=meta_info,
            mtype=type_name,
            media_source=media_source,
            media_id=normalized_media_id,
        )
        if media_info:
            media_info.scrape_source = media_source
            chain.obtain_images(mediainfo=media_info)
    else:
        context = chain.recognize_by_path(
            fileitem.path,
            media_source=media_source,
            obtain_images=True,
        )
        meta_info = context.meta_info if context else None
        media_info = context.media_info if context else None

    if not media_info:
        return _SchemaResponse(success=False, message="刮削失败，无法识别媒体信息")
    if media_source:
        media_info.scrape_source = media_source
    if storage == "local":
        if not Path(fileitem.path).exists():
            return _SchemaResponse(success=False, message="刮削路径不存在")
    # 手动刮削 (暂时使用同步版本，可以后续优化为异步)
    ScrapingChain().scrape_metadata(
        fileitem=fileitem,
        meta=meta_info,
        mediainfo=media_info,
        overwrite=True,
    )
    return _SchemaResponse(success=True, message=f"{fileitem.path} 刮削完成")


@router.post("/scrape/{storage}", summary="刮削媒体信息", response_model=_SchemaResponse[None])
def scrape(
    fileitem: _SchemaFileItem,
    storage: Optional[str] = "local",
    media_source: Optional[MediaSource] = None,
    media_id: Optional[str] = None,
    type_name: Optional[MediaType] = None,
    music_type: Optional[str] = None,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """刮削媒体信息的兼容公开入口。"""
    return _scrape_impl(fileitem, storage, media_source, media_id, type_name, music_type, _)


@router.get(
    "/category/config",
    summary="获取分类策略配置",
    response_model=_SchemaResponse[_SchemaCategoryConfig],
)
def get_category_config(
    _: object = Depends(get_current_active_user),
    classification: ClassificationRuntime = Depends(get_classification_runtime),
):
    """
    获取分类策略配置
    """
    config = classification.legacy_config()
    return _SchemaResponse(success=True, data=config.model_dump())


@router.get(
    "/category",
    summary="查询自动分类配置",
    response_model=_SchemaMediaCategoryMap,
)
async def category(
    _: _SchemaTokenPayload = Depends(verify_token),
    classification: ClassificationRuntime = Depends(get_classification_runtime),
) -> Any:
    """
    查询自动分类配置
    """
    return classification.media_categories()


@router.get(
    "/group/seasons/{episode_group}",
    summary="查询剧集组季信息",
    response_model=List[_SchemaMediaSeason],
)
async def group_seasons(
    episode_group: str,
    _: _SchemaTokenPayload = Depends(verify_token),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> Any:
    """
    查询剧集组季信息（themoviedb）
    """
    _, normalized_group_id = resolve_media_identity(
        media_source=MediaSource.TMDB,
        media_id=episode_group,
    )
    if not normalized_group_id:
        return []
    return await TmdbChain().async_tmdb_group_seasons(group_id=normalized_group_id)


@router.get(
    "/groups/{tmdbid}",
    summary="查询媒体剧集组",
    response_model=List[_SchemaMediaEpisodeGroup],
)
async def groups(
    tmdbid: int,
    _: _SchemaTokenPayload = Depends(verify_token),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> Any:
    """
    查询媒体剧集组列表（themoviedb）
    """
    media_source, media_id = resolve_media_identity(
        media_source=MediaSource.TMDB,
        media_id=tmdbid,
    )
    if not media_source or not media_id:
        return []
    mediainfo = await MediaChain().async_recognize_media(
        media_source=media_source,
        media_id=media_id,
        mtype=MediaType.TV,
    )
    if not mediainfo:
        return []
    return mediainfo.episode_groups


@router.get("/seasons", summary="查询媒体季信息", response_model=List[_SchemaMediaSeason])
async def seasons(
    media_source: Optional[MediaSource] = None,
    media_id: Optional[str] = None,
    title: Optional[str] = None,
    year: str = None,
    season: int = None,
    _: _SchemaTokenPayload = Depends(verify_token),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> Any:
    """
    查询媒体季信息
    """
    if media_source is not None or media_id is not None:
        normalized_source, normalized_media_id = resolve_media_identity(
            media_source=media_source,
            media_id=media_id,
        )
        if not normalized_source or not normalized_media_id:
            return []
        if normalized_source == MediaSource.TMDB and normalized_media_id.isdigit():
            tmdbid = int(normalized_media_id)
            seasons_info = await TmdbChain().async_tmdb_seasons(tmdbid=tmdbid)
            if seasons_info:
                if season is not None:
                    return [sea for sea in seasons_info if sea.season_number == season]
                return seasons_info
        else:
            mediainfo = await MediaChain().async_recognize_media(
                media_source=normalized_source,
                media_id=normalized_media_id,
                mtype=MediaType.TV,
                cache=False,
            )
            if mediainfo:
                return _build_media_seasons(mediainfo, season)
        # 明确来源的查询不能按标题切换到默认识别源，避免辅助 TMDB 信息替换主身份。
        return []
    if title:
        meta = MetaInfo(title)
        if year:
            meta.year = year
        meta.type = MediaType.TV
        mediainfo = await MediaChain().async_recognize_by_meta(
            meta,
            obtain_images=False,
        )
        if mediainfo:
            recognized_source, recognized_media_id = resolve_media_identity(media=mediainfo)
            if recognized_source == MediaSource.TMDB and recognized_media_id and recognized_media_id.isdigit():
                seasons_info = await TmdbChain().async_tmdb_seasons(tmdbid=int(recognized_media_id))
                if seasons_info:
                    if season is not None:
                        return [sea for sea in seasons_info if sea.season_number == season]
                    return seasons_info
            return _build_media_seasons(mediainfo, season)
    return []


@router.get("/{media_id}", summary="查询媒体详情", response_model=_SchemaMediaInfo)
async def detail(
    media_id: str,
    media_source: MediaSource,
    type_name: str,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    根据媒体来源和原生 ID 查询媒体信息，type_name: 电影/电视剧
    """
    mtype = MediaType(type_name)
    normalized_source, normalized_media_id = resolve_media_identity(
        media_source=media_source,
        media_id=media_id,
    )
    if not normalized_source or not normalized_media_id:
        return _SchemaMediaInfo()
    mediachain = MediaChain()
    mediainfo = await mediachain.async_recognize_media(
        media_source=normalized_source,
        media_id=normalized_media_id,
        mtype=mtype,
    )
    # 识别
    if mediainfo:
        await mediachain.async_obtain_images(mediainfo)
        # 电视剧且有 TVDB ID 时，补充获取 slug 用于构建 TheTvDb 直达链接
        if mediainfo.type == MediaType.TV and mediainfo.tvdb_id and not mediainfo.tvdb_slug:
            slug = mediachain.tvdb_slug(mediainfo.tvdb_id)
            if slug:
                mediainfo.tvdb_slug = slug
        return mediainfo.to_dict()

    return _SchemaMediaInfo()
