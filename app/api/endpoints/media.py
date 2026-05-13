from pathlib import Path
from typing import List, Any, Union, Annotated, Optional

from fastapi import APIRouter, Depends

from app import schemas
from app.chain.media import MediaChain
from app.chain.tmdb import TmdbChain
from app.core.config import settings
from app.core.context import Context
from app.core.event import eventmanager
from app.core.metainfo import MetaInfo
from app.core.security import verify_token, verify_apitoken
from app.db.models import User
from app.db.user_oper import get_current_active_user, get_current_active_superuser
from app.schemas import MediaType, MediaRecognizeConvertEventData
from app.schemas.category import CategoryConfig
from app.schemas.types import ChainEventType

router = APIRouter()


@router.get(
    "/recognize", summary="识别媒体信息（种子）", response_model=schemas.Context
)
async def recognize(
    title: str,
    subtitle: Optional[str] = None,
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    根据标题、副标题识别媒体信息
    """
    # 识别媒体信息
    metainfo = MetaInfo(title, subtitle)
    mediainfo = await MediaChain().async_recognize_by_meta(metainfo)
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
) -> Any:
    """
    根据标题、副标题识别媒体信息 API_TOKEN认证（?token=xxx）
    """
    # 识别媒体信息
    return await recognize(title, subtitle)


@router.get(
    "/recognize_file", summary="识别媒体信息（文件）", response_model=schemas.Context
)
async def recognize_file(
    path: str, _: schemas.TokenPayload = Depends(verify_token)
) -> Any:
    """
    根据文件路径识别媒体信息
    """
    # 识别媒体信息
    context = await MediaChain().async_recognize_by_path(path)
    if context:
        return context.to_dict()
    return schemas.Context()


@router.get(
    "/recognize_file2",
    summary="识别文件媒体信息（API_TOKEN）",
    response_model=schemas.Context,
)
async def recognize_file2(
    path: str, _: Annotated[str, Depends(verify_apitoken)]
) -> Any:
    """
    根据文件路径识别媒体信息 API_TOKEN认证（?token=xxx）
    """
    # 识别媒体信息
    return await recognize_file(path)


@router.get("/search", summary="搜索媒体/人物信息", response_model=List[dict])
async def search(
    title: str,
    type: Optional[str] = "media",
    page: int = 1,
    count: int = 8,
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    模糊搜索媒体/人物信息列表 media：媒体信息，person：人物信息
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
        _, medias = await media_chain.async_search(title=title)
        result = [media.to_dict() for media in medias] if medias else []
    elif type == "collection":
        collections = await media_chain.async_search_collections(name=title)
        result = (
            [collection.to_dict() for collection in collections] if collections else []
        )
    else:  # person
        persons = await media_chain.async_search_persons(name=title)
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
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    刮削媒体信息
    """
    if not fileitem or not fileitem.path:
        return schemas.Response(success=False, message="刮削路径无效")
    chain = MediaChain()
    # 识别媒体信息
    context = chain.recognize_by_path(fileitem.path, obtain_images=True)
    if not context or not context.media_info:
        return schemas.Response(success=False, message="刮削失败，无法识别媒体信息")
    if storage == "local":
        if not Path(fileitem.path).exists():
            return schemas.Response(success=False, message="刮削路径不存在")
    # 手动刮削 (暂时使用同步版本，可以后续优化为异步)
    chain.scrape_metadata(
        fileitem=fileitem,
        meta=context.meta_info,
        mediainfo=context.media_info,
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
        if mediaid.startswith("tmdb:"):
            tmdbid = int(mediaid[5:])
            seasons_info = await TmdbChain().async_tmdb_seasons(tmdbid=tmdbid)
            if seasons_info:
                if season is not None:
                    return [sea for sea in seasons_info if sea.season_number == season]
                return seasons_info
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
            if settings.RECOGNIZE_SOURCE == "themoviedb":
                seasons_info = await TmdbChain().async_tmdb_seasons(
                    tmdbid=mediainfo.tmdb_id
                )
                if seasons_info:
                    if season is not None:
                        return [
                            sea for sea in seasons_info if sea.season_number == season
                        ]
                    return seasons_info
            else:
                sea = season if season is not None else 1
                return [
                    schemas.MediaSeason(
                        season_number=sea,
                        poster_path=mediainfo.poster_path,
                        name=f"第 {sea} 季",
                        air_date=mediainfo.release_date,
                        overview=mediainfo.overview,
                        vote_average=mediainfo.vote_average,
                        episode_count=mediainfo.number_of_episodes,
                    )
                ]
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
    根据媒体ID查询themoviedb或豆瓣媒体信息，type_name: 电影/电视剧
    """
    mtype = MediaType(type_name)
    mediainfo = None
    mediachain = MediaChain()
    if mediaid.startswith("tmdb:"):
        mediainfo = await mediachain.async_recognize_media(
            tmdbid=int(mediaid[5:]), mtype=mtype
        )
    elif mediaid.startswith("douban:"):
        mediainfo = await mediachain.async_recognize_media(
            doubanid=mediaid[7:], mtype=mtype
        )
    elif mediaid.startswith("bangumi:"):
        mediainfo = await mediachain.async_recognize_media(
            bangumiid=int(mediaid[8:]), mtype=mtype
        )
    else:
        # 广播事件解析媒体信息
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
            if event_data.convert_type == "themoviedb":
                mediainfo = await mediachain.async_recognize_media(
                    tmdbid=new_id, mtype=mtype
                )
            elif event_data.convert_type == "douban":
                mediainfo = await mediachain.async_recognize_media(
                    doubanid=new_id, mtype=mtype
                )
        elif title:
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
