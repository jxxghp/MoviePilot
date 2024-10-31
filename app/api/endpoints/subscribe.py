from typing import List, Any

import cn2an
from fastapi import APIRouter, Request, BackgroundTasks, Depends, HTTPException, Header
from sqlalchemy.orm import Session

from app import schemas
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.core.security import verify_token, verify_apitoken
from app.db import get_db
from app.db.models.subscribe import Subscribe
from app.db.models.subscribehistory import SubscribeHistory
from app.db.models.user import User
from app.db.user_oper import get_current_active_user
from app.helper.subscribe import SubscribeHelper
from app.scheduler import Scheduler
from app.schemas.types import MediaType

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
def list_subscribes(_: str = Depends(verify_apitoken)) -> Any:
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
    sid, message = SubscribeChain().add(mtype=mtype,
                                        title=title,
                                        year=subscribe_in.year,
                                        tmdbid=subscribe_in.tmdbid,
                                        season=subscribe_in.season,
                                        doubanid=subscribe_in.doubanid,
                                        bangumiid=subscribe_in.bangumiid,
                                        username=current_user.name,
                                        best_version=subscribe_in.best_version,
                                        save_path=subscribe_in.save_path,
                                        search_imdbid=subscribe_in.search_imdbid,
                                        custom_words=subscribe_in.custom_words,
                                        media_category=subscribe_in.media_category,
                                        filter_groups=subscribe_in.filter_groups,
                                        exist_ok=True)
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
    return schemas.Response(success=True)


@router.get("/media/{mediaid}", summary="查询订阅", response_model=schemas.Subscribe)
def subscribe_mediaid(
        query: schemas.SubscribeMediaQuery = Depends(),
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据 TMDBID/豆瓣ID/BangumiId 查询订阅 tmdb:/douban:
    """
    result = None
    title_check = False
    mediaid = query.mediaid
    season = query.season
    title = query.title
    if mediaid.startswith("tmdb:"):
        tmdbid = mediaid[5:]
        if not tmdbid or not tmdbid.isdigit():
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
        if not bangumiid or not bangumiid.isdigit():
            return Subscribe()
        result = Subscribe.get_by_bangumiid(db, int(bangumiid))
        if not result and title:
            title_check = True
    # 使用名称检查订阅
    if title_check and title:
        meta = MetaInfo(title)
        if season:
            meta.begin_season = season
        result = Subscribe.get_by_title(db, title=meta.name, season=meta.begin_season)

    return result if result else Subscribe()


@router.post("/media", summary="批量查询订阅", response_model=List[schemas.Subscribe])
def subscribe_mediaid_batch(
        querys: List[schemas.SubscribeMediaQuery],
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据多个TMDBID/豆瓣ID/BangumiId批量查询订阅
    """
    # 一次性获取所有订阅信息
    subscriptions = Subscribe.list(db)
    # 将已获取的订阅信息按照不同的ID类型分组，以便快速查找
    tmdbs = {str(sub.tmdbid): sub for sub in subscriptions if sub.tmdbid is not None}
    doubans = {sub.doubanid: sub for sub in subscriptions if sub.doubanid is not None}
    bangumis = {str(sub.bangumiid): sub for sub in subscriptions if sub.bangumiid is not None}
    results = []
    for query in querys:
        result = None
        title_check = False
        mediaid = query.mediaid
        season = query.season
        title = query.title
        if mediaid.startswith("tmdb:"):
            tmdbid = mediaid[5:]
            if not tmdbid or not tmdbid.isdigit():
                results.append(Subscribe())
                continue
            result = tmdbs.get(tmdbid)
        elif mediaid.startswith("douban:"):
            doubanid = mediaid[7:]
            if not doubanid:
                results.append(Subscribe())
                continue
            result = doubans.get(doubanid)
            if not result and title:
                title_check = True
        elif mediaid.startswith("bangumi:"):
            bangumiid = mediaid[8:]
            if not bangumiid or not bangumiid.isdigit():
                results.append(Subscribe())
                continue
            result = bangumis.get(bangumiid)
            if not result and title:
                title_check = True
        # 使用名称检查订阅
        if title_check and title:
            meta = MetaInfo(title)
            if season:
                meta.begin_season = season
            result = next((sub for sub in subscriptions
                           if sub.name == meta.name and sub.season == meta.begin_season), None)
        results.append(result if result else Subscribe())
    return results


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
        subscribe.update(db, {
            "note": "",
            "lack_episode": subscribe.total_episode
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
        season: int = None,
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)
) -> Any:
    """
    根据TMDBID或豆瓣ID删除订阅 tmdb:/douban:
    """
    if mediaid.startswith("tmdb:"):
        tmdbid = mediaid[5:]
        if not tmdbid or not str(tmdbid).isdigit():
            return schemas.Response(success=False)
        Subscribe().delete_by_tmdbid(db, int(tmdbid), season)
    elif mediaid.startswith("douban:"):
        doubanid = mediaid[7:]
        if not doubanid:
            return schemas.Response(success=False)
        Subscribe().delete_by_doubanid(db, doubanid)

    return schemas.Response(success=True)


@router.post("/seerr", summary="OverSeerr/JellySeerr通知订阅", response_model=schemas.Response)
async def seerr_subscribe(request: Request, background_tasks: BackgroundTasks,
                          authorization: str = Header(None)) -> Any:
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
        page: int = 1,
        count: int = 30,
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
        page: int = 1,
        count: int = 30,
        min_sub: int = None,
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


@router.get("/shares", summary="查询分享的订阅", response_model=List[schemas.SubscribeShare])
def popular_subscribes(
        name: str = None,
        page: int = 1,
        count: int = 30,
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
    # 统计订阅
    SubscribeHelper().sub_done_async({
        "tmdbid": subscribe.tmdbid,
        "doubanid": subscribe.doubanid
    })
    return schemas.Response(success=True)
