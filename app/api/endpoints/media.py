from pathlib import Path
from typing import Annotated, Any, List, Optional, Union

from fastapi import APIRouter, Depends

from app import schemas
from app.chain.media import MediaChain
from app.chain.tmdb import TmdbChain
from app.core.config import settings
from app.core.context import Context
from app.core.event import eventmanager
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfo, MetaInfoPath
from app.core.security import verify_token, verify_apitoken
from app.db.models import User
from app.db.user_oper import get_current_active_user, get_current_active_superuser
from app.schemas import MediaType, MediaRecognizeConvertEventData
from app.schemas.category import CategoryConfig
from app.schemas.types import ChainEventType
from app.utils.media import MEDIA_SOURCE_ID_FIELDS, parse_media_key

router = APIRouter()
MediaSource = str


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
        and title_path.suffix.lower() in settings.RMT_MEDIAEXT
    ):
        metainfo = MetaInfoPath(
            title_path,
            custom_words=custom_word_list,
        )
        metainfo.title = title
        return metainfo
    return MetaInfo(title, subtitle, custom_words=custom_word_list)


def _build_media_seasons(
        mediainfo: Any, season: Optional[int] = None,
) -> List[schemas.MediaSeason]:
    """将任意数据源的统一媒体信息转换为季信息响应。"""
    seasons_info = []
    for item in mediainfo.season_info or []:
        season_number = item.get("season_number")
        if season is not None and season_number != season:
            continue
        seasons_info.append(schemas.MediaSeason(
            air_date=item.get("air_date"),
            episode_count=item.get("episode_count"),
            name=item.get("name"),
            overview=item.get("overview"),
            poster_path=item.get("poster_path") or mediainfo.poster_path,
            season_number=season_number,
            vote_average=item.get("vote_average"),
        ))
    if seasons_info:
        return seasons_info

    season_numbers = sorted((mediainfo.seasons or {}).keys())
    if season is not None:
        season_numbers = [season]
    elif not season_numbers:
        season_numbers = [mediainfo.season or 1]
    return [
        schemas.MediaSeason(
            season_number=season_number,
            poster_path=mediainfo.poster_path,
            name=f"第 {season_number} 季",
            air_date=mediainfo.release_date,
            overview=mediainfo.overview,
            vote_average=mediainfo.vote_average,
            episode_count=(
                len((mediainfo.seasons or {}).get(season_number) or [])
                or mediainfo.number_of_episodes
            ),
        )
        for season_number in season_numbers
    ]


@router.get(
    "/recognize", summary="识别媒体信息（种子）", response_model=schemas.Context
)
async def recognize(
    title: str,
    subtitle: Optional[str] = None,
    custom_words: Optional[str] = None,
    source: Optional[MediaSource] = None,
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    根据标题、副标题识别媒体信息
    :param title: 标题
    :param subtitle: 副标题
    :param custom_words: 临时识别词（每行一条规则），传入时仅在本次识别中生效，不会保存到系统配置
    :param source: 请求级识别数据源
    :param _:
    """
    # 识别媒体信息，传入临时识别词时优先于系统配置的识别词生效
    metainfo = _build_recognize_metainfo(title, subtitle, custom_words)
    mediainfo = await MediaChain().async_recognize_by_meta(
        metainfo,
        source=source,
    )
    if mediainfo:
        return Context(meta_info=metainfo, media_info=mediainfo).to_dict()
    return schemas.Context()


@router.get(
    "/recognize2",
    summary="识别种子媒体信息（API_TOKEN）",
    response_model=schemas.Context,
)
async def recognize2(
    _: Annotated[str, Depends(verify_apitoken)],
    title: str,
    subtitle: Optional[str] = None,
    custom_words: Optional[str] = None,
    source: Optional[MediaSource] = None,
) -> Any:
    """
    根据标题、副标题识别媒体信息 API_TOKEN认证（?token=xxx）
    """
    # 识别媒体信息
    return await recognize(title, subtitle, custom_words, source)


@router.get(
    "/recognize_file", summary="识别媒体信息（文件）", response_model=schemas.Context
)
async def recognize_file(
    path: str,
    source: Optional[MediaSource] = None,
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    根据文件路径识别媒体信息
    """
    # 识别媒体信息
    context = await MediaChain().async_recognize_by_path(path, source=source)
    if context:
        return context.to_dict()
    return schemas.Context()


@router.get(
    "/recognize_file2",
    summary="识别文件媒体信息（API_TOKEN）",
    response_model=schemas.Context,
)
async def recognize_file2(
    path: str,
    _: Annotated[str, Depends(verify_apitoken)],
    source: Optional[MediaSource] = None,
) -> Any:
    """
    根据文件路径识别媒体信息 API_TOKEN认证（?token=xxx）
    """
    # 识别媒体信息
    return await recognize_file(path, source)


@router.get("/search", summary="搜索媒体/人物信息", response_model=List[dict])
async def search(
    title: str,
    type: Optional[str] = "media",
    page: int = 1,
    count: int = 8,
    source: Optional[MediaSource] = None,
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    模糊搜索媒体、合集或人物信息列表。

    :param title: 搜索关键词
    :param type: 搜索类型，支持 media、collection、person
    :param page: 页码
    :param count: 每页数量
    :param source: 请求级搜索数据源
    :param _: Token校验
    :return: 搜索结果列表
    """

    def __get_source(obj: Union[schemas.MediaInfo, schemas.MediaPerson, dict]):
        """
        获取对象属性
        """
        if isinstance(obj, dict):
            return obj.get("source")
        return obj.source

    media_chain = MediaChain()
    if type == "media":
        _, medias = await media_chain.async_search(title=title, source=source)
        result = [media.to_dict() for media in medias] if medias else []
    elif type == "collection":
        collections = await media_chain.async_search_collections(
            name=title, source=source
        )
        result = (
            [collection.to_dict() for collection in collections] if collections else []
        )
    else:  # person
        persons = await media_chain.async_search_persons(name=title, source=source)
        result = [person.model_dump() for person in persons] if persons else []

    if not result:
        return []

    # 排序和分页
    setting_order = settings.SEARCH_SOURCE.split(",") if settings.SEARCH_SOURCE else []
    sort_order = {source: index for index, source in enumerate(setting_order)}

    sorted_result = sorted(result, key=lambda x: sort_order.get(__get_source(x), 4))
    return sorted_result[(page - 1) * count : page * count]


@router.post(
    "/scrape/{storage}", summary="刮削媒体信息", response_model=schemas.Response
)
def scrape(
    fileitem: schemas.FileItem,
    storage: Optional[str] = "local",
    media_source: Optional[MediaSource] = None,
    media_id: Optional[str] = None,
    type_name: Optional[MediaType] = None,
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    刮削媒体信息，可按请求指定媒体数据源及其原生ID

    :param fileitem: 待刮削文件项
    :param storage: 文件所在存储
    :param media_source: 请求级媒体数据源
    :param media_id: 数据源原生ID
    :param type_name: 媒体类型
    :param _: Token校验
    """
    if not fileitem or not fileitem.path:
        return schemas.Response(success=False, message="刮削路径无效")
    normalized_media_id = media_id.strip() if media_id else None
    if normalized_media_id and not media_source:
        return schemas.Response(
            success=False, message="指定媒体ID时必须同时指定媒体数据源"
        )
    if normalized_media_id and not normalized_media_id.isdigit():
        return schemas.Response(success=False, message="媒体ID格式无效")

    chain = MediaChain()
    if normalized_media_id:
        meta_info = MetaInfoPath(Path(fileitem.path))
        media_info = chain.recognize_media(
            meta=meta_info,
            mtype=type_name,
            source=media_source,
            mediaid=normalized_media_id,
        )
        if media_info:
            media_info.scrape_source = media_source
            chain.obtain_images(mediainfo=media_info)
    else:
        context = chain.recognize_by_path(
            fileitem.path,
            source=media_source,
            obtain_images=True,
        )
        meta_info = context.meta_info if context else None
        media_info = context.media_info if context else None

    if not media_info:
        return schemas.Response(success=False, message="刮削失败，无法识别媒体信息")
    if media_source:
        media_info.scrape_source = media_source
    if storage == "local":
        if not Path(fileitem.path).exists():
            return schemas.Response(success=False, message="刮削路径不存在")
    # 手动刮削 (暂时使用同步版本，可以后续优化为异步)
    chain.scrape_metadata(
        fileitem=fileitem,
        meta=meta_info,
        mediainfo=media_info,
        overwrite=True,
    )
    return schemas.Response(success=True, message=f"{fileitem.path} 刮削完成")


@router.get(
    "/category/config", summary="获取分类策略配置", response_model=schemas.Response
)
def get_category_config(_: User = Depends(get_current_active_user)):
    """
    获取分类策略配置
    """
    config = MediaChain().category_config()
    return schemas.Response(success=True, data=config.model_dump())


@router.post(
    "/category/config", summary="保存分类策略配置", response_model=schemas.Response
)
def save_category_config(
    config: CategoryConfig, _: User = Depends(get_current_active_superuser)
):
    """
    保存分类策略配置
    """
    if MediaChain().save_category_config(config):
        return schemas.Response(success=True, message="保存成功")
    else:
        return schemas.Response(success=False, message="保存失败")


@router.get("/category", summary="查询自动分类配置", response_model=dict)
async def category(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询自动分类配置
    """
    return MediaChain().media_category() or {}


@router.get(
    "/group/seasons/{episode_group}",
    summary="查询剧集组季信息",
    response_model=List[schemas.MediaSeason],
)
async def group_seasons(
    episode_group: str, _: schemas.TokenPayload = Depends(verify_token)
) -> Any:
    """
    查询剧集组季信息（themoviedb）
    """
    return await TmdbChain().async_tmdb_group_seasons(group_id=episode_group)


@router.get("/groups/{tmdbid}", summary="查询媒体剧集组", response_model=List[dict])
async def groups(tmdbid: int, _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询媒体剧集组列表（themoviedb）
    """
    mediainfo = await MediaChain().async_recognize_media(
        tmdbid=tmdbid, mtype=MediaType.TV
    )
    if not mediainfo:
        return []
    return mediainfo.episode_groups


@router.get(
    "/seasons", summary="查询媒体季信息", response_model=List[schemas.MediaSeason]
)
async def seasons(
    mediaid: Optional[str] = None,
    title: Optional[str] = None,
    year: str = None,
    season: int = None,
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    查询媒体季信息
    """
    if mediaid:
        media_source, source_media_id = parse_media_key(mediaid)
        if media_source == "themoviedb":
            tmdbid = int(source_media_id)
            seasons_info = await TmdbChain().async_tmdb_seasons(tmdbid=tmdbid)
            if seasons_info:
                if season is not None:
                    return [sea for sea in seasons_info if sea.season_number == season]
                return seasons_info
        elif media_source and source_media_id:
            mediainfo = await MediaChain().async_recognize_media(
                source=media_source,
                mediaid=source_media_id,
                mtype=MediaType.TV,
                cache=False,
            )
            if mediainfo:
                return _build_media_seasons(mediainfo, season)
        # 明确来源的查询不能按标题切换到默认识别源，避免辅助 TMDB 信息替换主身份。
        if media_source and source_media_id:
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
            if mediainfo.source == "themoviedb" and mediainfo.tmdb_id:
                seasons_info = await TmdbChain().async_tmdb_seasons(
                    tmdbid=mediainfo.tmdb_id
                )
                if seasons_info:
                    if season is not None:
                        return [
                            sea for sea in seasons_info if sea.season_number == season
                        ]
                    return seasons_info
            return _build_media_seasons(mediainfo, season)
    return []


@router.get("/{mediaid}", summary="查询媒体详情", response_model=schemas.MediaInfo)
async def detail(
    mediaid: str,
    type_name: str,
    title: Optional[str] = None,
    year: str = None,
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    根据带来源前缀的媒体ID查询媒体信息，type_name: 电影/电视剧
    """
    mtype = MediaType(type_name)
    mediainfo = None
    mediachain = MediaChain()
    media_source, source_media_id = parse_media_key(mediaid)
    if media_source and source_media_id:
        mediainfo = await mediachain.async_recognize_media(
            source=media_source,
            mediaid=source_media_id,
            mtype=mtype,
        )
    if not mediainfo and (
        not media_source or media_source not in MEDIA_SOURCE_ID_FIELDS
    ):
        # 旧探索插件可能只提供列表或转换事件，原生 ID 直识别失败后需保留原有兼容链路。
        event_data = MediaRecognizeConvertEventData(
            mediaid=mediaid, convert_type=settings.RECOGNIZE_SOURCE
        )
        event = await eventmanager.async_send_event(
            ChainEventType.MediaRecognizeConvert, event_data
        )
        # 使用事件返回的上下文数据
        if event and event.event_data and event.event_data.media_dict:
            event_data: MediaRecognizeConvertEventData = event.event_data
            new_id = event_data.media_dict.get("id")
            if new_id is not None and event_data.convert_type:
                mediainfo = await mediachain.async_recognize_media(
                    source=event_data.convert_type,
                    mediaid=str(new_id),
                    mtype=mtype,
                )
        if not mediainfo and title:
            # 使用名称识别兜底
            meta = MetaInfo(title)
            if year:
                meta.year = year
            if mtype:
                meta.type = mtype
            mediainfo = await mediachain.async_recognize_by_meta(
                meta,
                obtain_images=False,
            )
    # 识别
    if mediainfo:
        await mediachain.async_obtain_images(mediainfo)
        return mediainfo.to_dict()

    return schemas.MediaInfo()
