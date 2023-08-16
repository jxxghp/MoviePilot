from typing import List, Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from starlette.background import BackgroundTasks

from app import schemas
from app.chain.rss import RssChain
from app.core.security import verify_token
from app.db import get_db
from app.db.models.rss import Rss
from app.helper.rss import RssHelper
from app.schemas import MediaType

router = APIRouter()


def start_rss_refresh(db: Session, rssid: int = None):
    """
    启动自定义订阅刷新
    """
    RssChain(db).refresh(rssid=rssid, manual=True)


@router.get("/", summary="所有自定义订阅", response_model=List[schemas.Rss])
def read_rsses(
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询所有自定义订阅
    """
    return Rss.list(db)


@router.post("/", summary="新增自定义订阅", response_model=schemas.Response)
def create_rss(
        *,
        rss_in: schemas.Rss,
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)
) -> Any:
    """
    新增自定义订阅
    """
    if rss_in.type:
        mtype = MediaType(rss_in.type)
    else:
        mtype = None
    rssid, errormsg = RssChain(db).add(
        mtype=mtype,
        **rss_in.dict()
    )
    if not rssid:
        return schemas.Response(success=False, message=errormsg)
    return schemas.Response(success=True, data={
        "id": rssid
    })


@router.put("/", summary="更新自定义订阅", response_model=schemas.Response)
def update_rss(
        *,
        rss_in: schemas.Rss,
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)
) -> Any:
    """
    更新自定义订阅信息
    """
    rss = Rss.get(db, rss_in.id)
    if not rss:
        return schemas.Response(success=False, message="自定义订阅不存在")

    rss.update(db, rss_in.dict())
    return schemas.Response(success=True)


@router.get("/preview/{rssid}", summary="预览自定义订阅", response_model=List[schemas.TorrentInfo])
def preview_rss(
        rssid: int,
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据ID查询自定义订阅RSS报文
    """
    rssinfo: Rss = Rss.get(db, rssid)
    if not rssinfo:
        return []
    torrents = RssHelper.parse(rssinfo.url, proxy=True if rssinfo.proxy else False) or []
    return [schemas.TorrentInfo(
        title=t.get("title"),
        description=t.get("description"),
        enclosure=t.get("enclosure"),
        size=t.get("size"),
        page_url=t.get("link"),
        pubdate=t["pubdate"].strftime("%Y-%m-%d %H:%M:%S") if t.get("pubdate") else None,
    ) for t in torrents]


@router.get("/refresh/{rssid}", summary="刷新自定义订阅", response_model=schemas.Response)
def refresh_rss(
        rssid: int,
        background_tasks: BackgroundTasks,
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据ID刷新自定义订阅
    """
    background_tasks.add_task(start_rss_refresh,
                              db=db,
                              rssid=rssid)
    return schemas.Response(success=True)


@router.get("/{rssid}", summary="查询自定义订阅详情", response_model=schemas.Rss)
def read_rss(
        rssid: int,
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据ID查询自定义订阅详情
    """
    return Rss.get(db, rssid)


@router.delete("/{rssid}", summary="删除自定义订阅", response_model=schemas.Response)
def read_rss(
        rssid: int,
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据ID删除自定义订阅
    """
    Rss.delete(db, rssid)
    return schemas.Response(success=True)
