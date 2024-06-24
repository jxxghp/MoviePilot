from pathlib import Path
from typing import List, Any, Union

from fastapi import APIRouter, Depends

from app import schemas
from app.chain.media import MediaChain
from app.core.config import settings
from app.core.context import Context
from app.core.metainfo import MetaInfo, MetaInfoPath
from app.core.security import verify_token, verify_apitoken
from app.schemas import MediaType

router = APIRouter()


@router.get("/recognize", summary="识别媒体信息（种子）", response_model=schemas.Context)
def recognize(title: str,
              subtitle: str = None,
              _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据标题、副标题识别媒体信息
    """
    # 识别媒体信息
    metainfo = MetaInfo(title, subtitle)
    mediainfo = MediaChain().recognize_by_meta(metainfo)
    if mediainfo:
        return Context(meta_info=metainfo, media_info=mediainfo).to_dict()
    return schemas.Context()


@router.get("/recognize2", summary="识别种子媒体信息（API_TOKEN）", response_model=schemas.Context)
def recognize2(title: str,
               subtitle: str = None,
               _: str = Depends(verify_apitoken)) -> Any:
    """
    根据标题、副标题识别媒体信息 API_TOKEN认证（?token=xxx）
    """
    # 识别媒体信息
    return recognize(title, subtitle)


@router.get("/recognize_file", summary="识别媒体信息（文件）", response_model=schemas.Context)
def recognize_file(path: str,
                   _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据文件路径识别媒体信息
    """
    # 识别媒体信息
    context = MediaChain().recognize_by_path(path)
    if context:
        return context.to_dict()
    return schemas.Context()


@router.get("/recognize_file2", summary="识别文件媒体信息（API_TOKEN）", response_model=schemas.Context)
def recognize_file2(path: str,
                    _: str = Depends(verify_apitoken)) -> Any:
    """
    根据文件路径识别媒体信息 API_TOKEN认证（?token=xxx）
    """
    # 识别媒体信息
    return recognize_file(path)


@router.get("/search", summary="搜索媒体/人物信息", response_model=List[dict])
def search(title: str,
           type: str = "media",
           page: int = 1,
           count: int = 8,
           _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    模糊搜索媒体/人物信息列表 media：媒体信息，person：人物信息
    """
    def __get_source(obj: Union[dict, schemas.MediaPerson]):
        """
        获取对象属性
        """
        if isinstance(obj, dict):
            return obj.get("source")
        return obj.source

    result = []
    if type == "media":
        _, medias = MediaChain().search(title=title)
        if medias:
            result = [media.to_dict() for media in medias]
    else:
        result = MediaChain().search_persons(name=title)
    if result:
        # 按设置的顺序对结果进行排序
        setting_order = settings.SEARCH_SOURCE.split(',') or []
        sort_order = {}
        for index, source in enumerate(setting_order):
            sort_order[source] = index
        result = sorted(result, key=lambda x: sort_order.get(__get_source(x), 4))
    return result[(page - 1) * count:page * count]


@router.post("/scrape/{storage}", summary="刮削媒体信息", response_model=schemas.Response)
def scrape(fileitem: schemas.FileItem,
           storage: str = "local",
           _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    刮削媒体信息
    """
    if not fileitem or not fileitem.path:
        return schemas.Response(success=False, message="刮削路径无效")
    chain = MediaChain()
    # 识别媒体信息
    scrape_path = Path(fileitem.path)
    meta = MetaInfoPath(scrape_path)
    mediainfo = chain.recognize_by_meta(meta)
    if not media_info:
        return schemas.Response(success=False, message="刮削失败，无法识别媒体信息")
    if storage == "local":
        if not scrape_path.exists():
            return schemas.Response(success=False, message="刮削路径不存在")
    else:
        if not fileitem.fileid:
            return schemas.Response(success=False, message="刮削文件ID无效")
    # 手动刮削
    chain.manual_scrape(storage=storage, fileitem=fileitem, meta=meta, mediainfo=mediainfo)
    return schemas.Response(success=True, message=f"{fileitem.path} 刮削完成")


@router.get("/category", summary="查询自动分类配置", response_model=dict)
def category(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询自动分类配置
    """
    return MediaChain().media_category() or {}


@router.get("/{mediaid}", summary="查询媒体详情", response_model=schemas.MediaInfo)
def media_info(mediaid: str, type_name: str,
               _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据媒体ID查询themoviedb或豆瓣媒体信息，type_name: 电影/电视剧
    """
    mtype = MediaType(type_name)
    tmdbid, doubanid, bangumiid = None, None, None
    if mediaid.startswith("tmdb:"):
        tmdbid = int(mediaid[5:])
    elif mediaid.startswith("douban:"):
        doubanid = mediaid[7:]
    elif mediaid.startswith("bangumi:"):
        bangumiid = int(mediaid[8:])
    if not tmdbid and not doubanid and not bangumiid:
        return schemas.MediaInfo()
    # 识别
    mediainfo = MediaChain().recognize_media(tmdbid=tmdbid, doubanid=doubanid, bangumiid=bangumiid, mtype=mtype)
    if mediainfo:
        MediaChain().obtain_images(mediainfo)
        return mediainfo.to_dict()
    return schemas.MediaInfo()
