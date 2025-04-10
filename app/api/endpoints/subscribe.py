from typing import List, Any, Annotated, Optional

import cn2an
from fastapi import APIRouter, Request, BackgroundTasks, Depends, HTTPException, Header
from sqlalchemy.orm import Session

from app import schemas
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.event import eventmanager
from app.core.metainfo import MetaInfo
from app.core.security import verify_token, verify_apitoken
from app.db import get_db
from app.db.models.subscribe import Subscribe
from app.db.models.subscribehistory import SubscribeHistory
from app.db.models.user import User
from app.db.systemconfig_oper import SystemConfigOper
from app.db.user_oper import get_current_active_user
from app.helper.subscribe import SubscribeHelper
from app.scheduler import Scheduler
from app.schemas.types import MediaType, EventType, SystemConfigKey

router = APIRouter()


def start_subscribe_add(title: str, year: str,
                        mtype: MediaType, tmdbid: int, season: int, username: str):
    """
    启动订阅任务
    """
    SubscribeChain().add(title=title, year=year,
                         mtype=mtype, tmdbid=tmdbid, season=season, username=username)


@router.get("/", summary="查询所有订阅", response_model=List[schemas.Subscribe])
def read_subscribes(
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询所有订阅
    """
    return Subscribe.list(db)


@router.get("/list", summary="查询所有订阅（API_TOKEN）", response_model=List[schemas.Subscribe])
def list_subscribes(_: Annotated[str, Depends(verify_apitoken)]) -> Any:
    """
    查询所有订阅 API_TOKEN认证（?token=xxx）
    """
    return read_subscribes()


@router.post("/", summary="新增订阅", response_model=schemas.Response)
def create_subscribe(
        *,
        subscribe_in: schemas.Subscribe,
        current_user: User = Depends(get_current_active_user),
) -> schemas.Response:
    """
    新增订阅
    """
    # 类型转换
    if subscribe_in.type:
        mtype = MediaType(subscribe_in.type)
    else:
        mtype = None
    # 豆瓣标理
    if subscribe_in.doubanid or subscribe_in.bangumiid:
        meta = MetaInfo(subscribe_in.name)
        subscribe_in.name = meta.name
        subscribe_in.season = meta.begin_season
    # 标题转换
    if subscribe_in.name:
        title = subscribe_in.name
    else:
        title = None
    # 订阅用户
    subscribe_in.username = current_user.name
    sid, message = SubscribeChain().add(mtype=mtype,
                                        title=title,
                                        exist_ok=True,
                                        **subscribe_in.dict())
    return schemas.Response(
        success=bool(sid), message=message, data={"id": sid}
    )


@router.put("/", summary="更新订阅", response_model=schemas.Response)
def update_subscribe(
        *,
        subscribe_in: schemas.Subscribe,
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)
) -> Any:
    """
    更新订阅信息
    """
    subscribe = Subscribe.get(db, subscribe_in.id)
    if not subscribe:
        return schemas.Response(success=False, message="订阅不存在")
    # 避免更新缺失集数
    old_subscribe_dict = subscribe.to_dict()
    subscribe_dict = subscribe_in.dict()
    if not subscribe_in.lack_episode:
        # 没有缺失集数时，缺失集数清空，避免更新为0
        subscribe_dict.pop("lack_episode")
    elif subscribe_in.total_episode:
        # 总集数增加时，缺失集数也要增加
        if subscribe_in.total_episode > (subscribe.total_episode or 0):
            subscribe_dict["lack_episode"] = (subscribe.lack_episode
                                              + (subscribe_in.total_episode
                                                 - (subscribe.total_episode or 0)))
    # 是否手动修改过总集数
    if subscribe_in.total_episode != subscribe.total_episode:
        subscribe_dict["manual_total_episode"] = 1
    subscribe.update(db, subscribe_dict)
    # 发送订阅调整事件
    eventmanager.send_event(EventType.SubscribeModified, {
        "subscribe_id": subscribe.id,
        "old_subscribe_info": old_subscribe_dict,
        "subscribe_info": subscribe.to_dict(),
    })
    return schemas.Response(success=True)


@router.put("/status/{subid}", summary="更新订阅状态", response_model=schemas.Response)
def update_subscribe_status(
        subid: int,
        state: str,
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    更新订阅状态
    """
    subscribe = Subscribe.get(db, subid)
    if not subscribe:
        return schemas.Response(success=False, message="订阅不存在")
    valid_states = ["R", "P", "S"]
    if state not in valid_states:
        return schemas.Response(success=False, message="无效的订阅状态")
    old_subscribe_dict = subscribe.to_dict()
    subscribe.update(db, {
        "state": state
    })
    # 发送订阅调整事件
    eventmanager.send_event(EventType.SubscribeModified, {
        "subscribe_id": subscribe.id,
        "old_subscribe_info": old_subscribe_dict,
        "subscribe_info": subscribe.to_dict(),
    })
    return schemas.Response(success=True)


@router.get("/media/{mediaid}", summary="查询订阅", response_model=schemas.Subscribe)
def subscribe_mediaid(
        mediaid: str,
        season: Optional[int] = None,
        title: Optional[str] = None,
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据 TMDBID/豆瓣ID/BangumiId 查询订阅 tmdb:/douban:
    """
    title_check = False
    if mediaid.startswith("tmdb:"):
        tmdbid = mediaid[5:]
        if not tmdbid or not str(tmdbid).isdigit():
            return Subscribe()
        result = Subscribe.exists(db, tmdbid=int(tmdbid), season=season)
    elif mediaid.startswith("douban:"):
        doubanid = mediaid[7:]
        if not doubanid:
            return Subscribe()
        result = Subscribe.get_by_doubanid(db, doubanid)
        if not result and title:
            title_check = True
    elif mediaid.startswith("bangumi:"):
        bangumiid = mediaid[8:]
        if not bangumiid or not str(bangumiid).isdigit():
            return Subscribe()
        result = Subscribe.get_by_bangumiid(db, int(bangumiid))
        if not result and title:
            title_check = True
    else:
        result = Subscribe.get_by_mediaid(db, mediaid)
        if not result and title:
            title_check = True
    # 使用名称检查订阅
    if title_check and title:
        meta = MetaInfo(title)
        if season:
            meta.begin_season = season
        result = Subscribe.get_by_title(db, title=meta.name, season=meta.begin_season)

    return result if result else Subscribe()


@router.get("/refresh", summary="刷新订阅", response_model=schemas.Response)
def refresh_subscribes(
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    刷新所有订阅
    """
    Scheduler().start("subscribe_refresh")
    return schemas.Response(success=True)


@router.get("/reset/{subid}", summary="重置订阅", response_model=schemas.Response)
def reset_subscribes(
        subid: int,
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    重置订阅
    """
    subscribe = Subscribe.get(db, subid)
    if subscribe:
        old_subscribe_dict = subscribe.to_dict()
        subscribe.update(db, {
            "note": [],
            "lack_episode": subscribe.total_episode,
            "state": "R"
        })
        # 发送订阅调整事件
        eventmanager.send_event(EventType.SubscribeModified, {
            "subscribe_id": subscribe.id,
            "old_subscribe_info": old_subscribe_dict,
            "subscribe_info": subscribe.to_dict(),
        })
        return schemas.Response(success=True)
    return schemas.Response(success=False, message="订阅不存在")


@router.get("/check", summary="刷新订阅 TMDB 信息", response_model=schemas.Response)
def check_subscribes(
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    刷新订阅 TMDB 信息
    """
    Scheduler().start("subscribe_tmdb")
    return schemas.Response(success=True)


@router.get("/search", summary="搜索所有订阅", response_model=schemas.Response)
def search_subscribes(
        background_tasks: BackgroundTasks,
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    搜索所有订阅
    """
    background_tasks.add_task(
        Scheduler().start,
        job_id="subscribe_search",
        **{
            "sid": None,
            "state": 'R',
            "manual": True
        }
    )
    return schemas.Response(success=True)


@router.get("/search/{subscribe_id}", summary="搜索订阅", response_model=schemas.Response)
def search_subscribe(
        subscribe_id: int,
        background_tasks: BackgroundTasks,
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据订阅编号搜索订阅
    """
    background_tasks.add_task(
        Scheduler().start,
        job_id="subscribe_search",
        **{
            "sid": subscribe_id,
            "state": None,
            "manual": True
        }
    )
    return schemas.Response(success=True)


@router.delete("/media/{mediaid}", summary="删除订阅", response_model=schemas.Response)
def delete_subscribe_by_mediaid(
        mediaid: str,
        season: Optional[int] = None,
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)
) -> Any:
    """
    根据TMDBID或豆瓣ID删除订阅 tmdb:/douban:
    """
    delete_subscribes = []
    if mediaid.startswith("tmdb:"):
        tmdbid = mediaid[5:]
        if not tmdbid or not str(tmdbid).isdigit():
            return schemas.Response(success=False)
        subscribes = Subscribe().get_by_tmdbid(db, int(tmdbid), season)
        delete_subscribes.extend(subscribes)
    elif mediaid.startswith("douban:"):
        doubanid = mediaid[7:]
        if not doubanid:
            return schemas.Response(success=False)
        subscribe = Subscribe().get_by_doubanid(db, doubanid)
        if subscribe:
            delete_subscribes.append(subscribe)
    else:
        subscribe = Subscribe().get_by_mediaid(db, mediaid)
        if subscribe:
            delete_subscribes.append(subscribe)
    for subscribe in delete_subscribes:
        Subscribe().delete(db, subscribe.id)
        # 发送事件
        eventmanager.send_event(EventType.SubscribeDeleted, {
            "subscribe_id": subscribe.id,
            "subscribe_info": subscribe.to_dict()
        })
    return schemas.Response(success=True)


@router.post("/seerr", summary="OverSeerr/JellySeerr通知订阅", response_model=schemas.Response)
async def seerr_subscribe(request: Request, background_tasks: BackgroundTasks,
                          authorization: Annotated[str | None, Header()] = None) -> Any:
    """
    Jellyseerr/Overseerr网络勾子通知订阅
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
        background_tasks.add_task(start_subscribe_add,
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
            background_tasks.add_task(start_subscribe_add,
                                      mtype=media_type,
                                      tmdbid=tmdbId,
                                      title=subject,
                                      year="",
                                      season=season,
                                      username=user_name)

    return schemas.Response(success=True)


@router.get("/history/{mtype}", summary="查询订阅历史", response_model=List[schemas.Subscribe])
def subscribe_history(
        mtype: str,
        page: Optional[int] = 1,
        count: Optional[int] = 30,
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询电影/电视剧订阅历史
    """
    return SubscribeHistory.list_by_type(db, mtype=mtype, page=page, count=count)


@router.delete("/history/{history_id}", summary="删除订阅历史", response_model=schemas.Response)
def delete_subscribe(
        history_id: int,
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)
) -> Any:
    """
    删除订阅历史
    """
    SubscribeHistory.delete(db, history_id)
    return schemas.Response(success=True)


@router.get("/popular", summary="热门订阅（基于用户共享数据）", response_model=List[schemas.MediaInfo])
def popular_subscribes(
        stype: str,
        page: Optional[int] = 1,
        count: Optional[int] = 30,
        min_sub: Optional[int] = None,
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询热门订阅
    """
    subscribes = SubscribeHelper().get_statistic(stype=stype, page=page, count=count)
    if subscribes:
        ret_medias = []
        for sub in subscribes:
            # 订阅人数
            count = sub.get("count")
            if min_sub and count < min_sub:
                continue
            media = MediaInfo()
            media.type = MediaType(sub.get("type"))
            media.tmdb_id = sub.get("tmdbid")
            # 处理标题
            title = sub.get("name")
            season = sub.get("season")
            if season and int(season) > 1 and media.tmdb_id:
                # 小写数据转大写
                season_str = cn2an.an2cn(season, "low")
                title = f"{title} 第{season_str}季"
            media.title = title
            media.year = sub.get("year")
            media.douban_id = sub.get("doubanid")
            media.bangumi_id = sub.get("bangumiid")
            media.tvdb_id = sub.get("tvdbid")
            media.imdb_id = sub.get("imdbid")
            media.season = sub.get("season")
            media.overview = sub.get("description")
            media.vote_average = sub.get("vote")
            media.poster_path = sub.get("poster")
            media.backdrop_path = sub.get("backdrop")
            media.popularity = count
            ret_medias.append(media)
        return [media.to_dict() for media in ret_medias]
    return []


@router.get("/user/{username}", summary="用户订阅", response_model=List[schemas.Subscribe])
def user_subscribes(
        username: str,
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询用户订阅
    """
    return Subscribe.list_by_username(db, username)


@router.get("/files/{subscribe_id}", summary="订阅相关文件信息", response_model=schemas.SubscrbieInfo)
def subscribe_files(
        subscribe_id: int,
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    订阅相关文件信息
    """
    subscribe = Subscribe.get(db, subscribe_id)
    if subscribe:
        return SubscribeChain().subscribe_files_info(subscribe)
    return schemas.SubscrbieInfo()


@router.post("/share", summary="分享订阅", response_model=schemas.Response)
def subscribe_share(
        sub: schemas.SubscribeShare,
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    分享订阅
    """
    state, errmsg = SubscribeHelper().sub_share(subscribe_id=sub.subscribe_id,
                                                share_title=sub.share_title,
                                                share_comment=sub.share_comment,
                                                share_user=sub.share_user)
    return schemas.Response(success=state, message=errmsg)


@router.delete("/share/{share_id}", summary="删除分享", response_model=schemas.Response)
def subscribe_share_delete(
        share_id: int,
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    删除分享
    """
    state, errmsg = SubscribeHelper().share_delete(share_id=share_id)
    return schemas.Response(success=state, message=errmsg)


@router.post("/fork", summary="复用订阅", response_model=schemas.Response)
def subscribe_fork(
        sub: schemas.SubscribeShare,
        current_user: User = Depends(get_current_active_user)) -> Any:
    """
    复用订阅
    """
    sub_dict = sub.dict()
    sub_dict.pop("id")
    for key in list(sub_dict.keys()):
        if not hasattr(schemas.Subscribe(), key):
            sub_dict.pop(key)
    result = create_subscribe(subscribe_in=schemas.Subscribe(**sub_dict),
                              current_user=current_user)
    if result.success:
        SubscribeHelper().sub_fork(share_id=sub.id)
    return result


@router.get("/follow", summary="查询已Follow的订阅分享人", response_model=List[str])
def followed_subscribers(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询已Follow的订阅分享人
    """
    return SystemConfigOper().get(SystemConfigKey.FollowSubscribers) or []


@router.post("/follow", summary="Follow订阅分享人", response_model=schemas.Response)
def follow_subscriber(
        share_uid: Optional[str] = None,
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    Follow订阅分享人
    """
    subscribers = SystemConfigOper().get(SystemConfigKey.FollowSubscribers) or []
    if share_uid and share_uid not in subscribers:
        subscribers.append(share_uid)
        SystemConfigOper().set(SystemConfigKey.FollowSubscribers, subscribers)
    return schemas.Response(success=True)


@router.delete("/follow", summary="取消Follow订阅分享人", response_model=schemas.Response)
def unfollow_subscriber(
        share_uid: Optional[str] = None,
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    取消Follow订阅分享人
    """
    subscribers = SystemConfigOper().get(SystemConfigKey.FollowSubscribers) or []
    if share_uid and share_uid in subscribers:
        subscribers.remove(share_uid)
        SystemConfigOper().set(SystemConfigKey.FollowSubscribers, subscribers)
    return schemas.Response(success=True)


@router.get("/shares", summary="查询分享的订阅", response_model=List[schemas.SubscribeShare])
def popular_subscribes(
        name: Optional[str] = None,
        page: Optional[int] = 1,
        count: Optional[int] = 30,
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询分享的订阅
    """
    return SubscribeHelper().get_shares(name=name, page=page, count=count)


@router.get("/{subscribe_id}", summary="订阅详情", response_model=schemas.Subscribe)
def read_subscribe(
        subscribe_id: int,
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据订阅编号查询订阅信息
    """
    if not subscribe_id:
        return Subscribe()
    return Subscribe.get(db, subscribe_id)


@router.delete("/{subscribe_id}", summary="删除订阅", response_model=schemas.Response)
def delete_subscribe(
        subscribe_id: int,
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)
) -> Any:
    """
    删除订阅信息
    """
    subscribe = Subscribe.get(db, subscribe_id)
    if subscribe:
        subscribe.delete(db, subscribe_id)
        # 发送事件
        eventmanager.send_event(EventType.SubscribeDeleted, {
            "subscribe_id": subscribe_id,
            "subscribe_info": subscribe.to_dict()
        })
        # 统计订阅
        SubscribeHelper().sub_done_async({
            "tmdbid": subscribe.tmdbid,
            "doubanid": subscribe.doubanid
        })
    return schemas.Response(success=True)
