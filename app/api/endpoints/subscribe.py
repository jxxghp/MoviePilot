from typing import List, Any

from fastapi import APIRouter, Request, BackgroundTasks, Depends, HTTPException, Header
from sqlalchemy.orm import Session

from app import schemas
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.db import get_db
from app.db.models.subscribe import Subscribe
from app.db.models.user import User
from app.db.userauth import get_current_active_superuser
from app.schemas.types import MediaType

router = APIRouter()


def start_subscribe_chain(title: str, year: str,
                          mtype: MediaType, tmdbid: int, season: int, username: str):
    """
    启动订阅链式任务
    """
    SubscribeChain().add(title=title, year=year,
                         mtype=mtype, tmdbid=tmdbid, season=season, username=username)


@router.get("/", summary="所有订阅", response_model=List[schemas.Subscribe])
async def read_subscribes(
        db: Session = Depends(get_db),
        _: User = Depends(get_current_active_superuser)) -> Any:
    """
    查询所有订阅
    """
    return Subscribe.list(db)


@router.post("/", summary="新增订阅", response_model=schemas.Response)
async def create_subscribe(
        *,
        subscribe_in: schemas.Subscribe,
        _: User = Depends(get_current_active_superuser),
) -> Any:
    """
    新增订阅
    """
    result = SubscribeChain().add(**subscribe_in.dict())
    return schemas.Response(success=result)


@router.put("/", summary="更新订阅", response_model=schemas.Subscribe)
async def update_subscribe(
        *,
        db: Session = Depends(get_db),
        subscribe_in: schemas.Subscribe,
        _: User = Depends(get_current_active_superuser),
) -> Any:
    """
    更新订阅信息
    """
    subscribe = Subscribe.get(db, subscribe_in.id)
    if not subscribe:
        raise HTTPException(
            status_code=404,
            detail=f"订阅 {subscribe_in.id} 不存在",
        )
    subscribe.update(db, **subscribe_in.dict())
    return subscribe


@router.delete("/", summary="删除订阅", response_model=schemas.Response)
async def delete_subscribe(
        *,
        db: Session = Depends(get_db),
        subscribe_in: schemas.Subscribe,
        _: User = Depends(get_current_active_superuser),
) -> Any:
    """
    删除订阅信息
    """
    Subscribe.delete(db, subscribe_in.id)
    return schemas.Response(success=True)


@router.post("/seerr", summary="OverSeerr/JellySeerr通知订阅", response_model=schemas.Response)
async def seerr_subscribe(request: Request, background_tasks: BackgroundTasks,
                          authorization: str = Header(None)) -> Any:
    """
    Jellyseerr/Overseerr订阅
    """
    if not authorization or authorization != settings.API_TOKEN:
        raise HTTPException(
            status_code=400,
            detail="授权失败",
        )
    req_json = await request.json()
    if not req_json:
        raise HTTPException(
            status_code=500,
            detail="报文内容为空",
        )
    notification_type = req_json.get("notification_type")
    if notification_type not in ["MEDIA_APPROVED", "MEDIA_AUTO_APPROVED"]:
        return schemas.Response(success=False, message="不支持的通知类型")
    subject = req_json.get("subject")
    media_type = MediaType.MOVIE if req_json.get("media", {}).get("media_type") == "movie" else MediaType.TV
    tmdbId = req_json.get("media", {}).get("tmdbId")
    if not media_type or not tmdbId or not subject:
        return schemas.Response(success=False, message="请求参数不正确")
    user_name = req_json.get("request", {}).get("requestedBy_username")
    # 添加订阅
    if media_type == MediaType.MOVIE:
        background_tasks.add_task(start_subscribe_chain,
                                  mtype=media_type,
                                  tmdbid=tmdbId,
                                  title=subject,
                                  year="",
                                  season=0,
                                  username=user_name)
    else:
        seasons = []
        for extra in req_json.get("extra", []):
            if extra.get("name") == "Requested Seasons":
                seasons = [int(str(sea).strip()) for sea in extra.get("value").split(", ") if str(sea).isdigit()]
                break
        for season in seasons:
            background_tasks.add_task(start_subscribe_chain,
                                      mtype=media_type,
                                      tmdbid=tmdbId,
                                      title=subject,
                                      year="",
                                      season=season,
                                      username=user_name)

    return schemas.Response(success=True)


@router.get("/refresh", summary="刷新订阅", response_model=schemas.Response)
async def refresh_subscribes(
        _: User = Depends(get_current_active_superuser)) -> Any:
    """
    刷新所有订阅
    """
    SubscribeChain().refresh()
    return schemas.Response(success=True)


@router.get("/search", summary="搜索订阅", response_model=schemas.Response)
async def search_subscribes(
        _: User = Depends(get_current_active_superuser)) -> Any:
    """
    搜索所有订阅
    """
    SubscribeChain().search(state='R')
    return schemas.Response(success=True)
