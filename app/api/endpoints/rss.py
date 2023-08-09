from typing import List, Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import schemas
from app.core.security import verify_token
from app.db import get_db
from app.db.models.rss import Rss

router = APIRouter()


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
        db: Session = Depends(get_db),
        rss_in: schemas.Rss,
        _: schemas.TokenPayload = Depends(verify_token)
) -> Any:
    """
    新增自定义订阅
    """
    rss = Rss.get_by_tmdbid(db, tmdbid=rss_in.tmdbid, season=rss_in.season)
    if rss:
        return schemas.Response(success=False, message="自定义订阅已存在")
    rss = Rss(**rss_in.dict())
    rss.create(db)
    return schemas.Response(success=True)


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


@router.get("/{rssid}", summary="查询订阅详情", response_model=schemas.Rss)
def read_rss(
        rssid: int,
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据ID查询自定义订阅详情
    """
    return Rss.get(db, rssid)
