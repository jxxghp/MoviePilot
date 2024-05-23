from typing import List, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from starlette.background import BackgroundTasks

from app import schemas
from app.chain.site import SiteChain
from app.chain.torrents import TorrentsChain
from app.core.event import EventManager
from app.core.security import verify_token
from app.db import get_db
from app.db.models import User
from app.db.models.site import Site
from app.db.models.siteicon import SiteIcon
from app.db.models.sitestatistic import SiteStatistic
from app.db.systemconfig_oper import SystemConfigOper
from app.db.userauth import get_current_active_superuser
from app.helper.sites import SitesHelper
from app.scheduler import Scheduler
from app.schemas.types import SystemConfigKey, EventType
from app.utils.string import StringUtils

router = APIRouter()


@router.get("/", summary="所有站点", response_model=List[schemas.Site])
def read_sites(db: Session = Depends(get_db),
               _: schemas.TokenPayload = Depends(verify_token)) -> List[dict]:
    """
    获取站点列表
    """
    return Site.list_order_by_pri(db)


@router.post("/", summary="新增站点", response_model=schemas.Response)
def add_site(
        *,
        db: Session = Depends(get_db),
        site_in: schemas.Site,
        _: schemas.TokenPayload = Depends(verify_token)
) -> Any:
    """
    新增站点
    """
    if not site_in.url:
        return schemas.Response(success=False, message="站点地址不能为空")
    if SitesHelper().auth_level < 2:
        return schemas.Response(success=False, message="用户未通过认证，无法使用站点功能！")
    domain = StringUtils.get_url_domain(site_in.url)
    site_info = SitesHelper().get_indexer(domain)
    if not site_info:
        return schemas.Response(success=False, message="该站点不支持，请检查站点域名是否正确")
    if Site.get_by_domain(db, domain):
        return schemas.Response(success=False, message=f"{domain} 站点己存在")
    # 保存站点信息
    site_in.domain = domain
    # 校正地址格式
    _scheme, _netloc = StringUtils.get_url_netloc(site_in.url)
    site_in.url = f"{_scheme}://{_netloc}/"
    site_in.name = site_info.get("name")
    site_in.id = None
    site_in.public = 1 if site_info.get("public") else 0
    site = Site(**site_in.dict())
    site.create(db)
    # 通知站点更新
    EventManager().send_event(EventType.SiteUpdated, {
        "domain": domain
    })
    return schemas.Response(success=True)


@router.put("/", summary="更新站点", response_model=schemas.Response)
def update_site(
        *,
        db: Session = Depends(get_db),
        site_in: schemas.Site,
        _: schemas.TokenPayload = Depends(verify_token)
) -> Any:
    """
    更新站点信息
    """
    site = Site.get(db, site_in.id)
    if not site:
        return schemas.Response(success=False, message="站点不存在")
    # 校正地址格式
    _scheme, _netloc = StringUtils.get_url_netloc(site_in.url)
    site_in.url = f"{_scheme}://{_netloc}/"
    site.update(db, site_in.dict())
    # 通知站点更新
    EventManager().send_event(EventType.SiteUpdated, {
        "domain": site_in.domain
    })
    return schemas.Response(success=True)


@router.get("/cookiecloud", summary="CookieCloud同步", response_model=schemas.Response)
def cookie_cloud_sync(background_tasks: BackgroundTasks,
                      _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    运行CookieCloud同步站点信息
    """
    background_tasks.add_task(Scheduler().start, job_id="cookiecloud")
    return schemas.Response(success=True, message="CookieCloud同步任务已启动！")


@router.get("/reset", summary="重置站点", response_model=schemas.Response)
def reset(db: Session = Depends(get_db),
          _: User = Depends(get_current_active_superuser)) -> Any:
    """
    清空所有站点数据并重新同步CookieCloud站点信息
    """
    Site.reset(db)
    SystemConfigOper().set(SystemConfigKey.IndexerSites, [])
    SystemConfigOper().set(SystemConfigKey.RssSites, [])
    # 启动定时服务
    Scheduler().start("cookiecloud", manual=True)
    # 插件站点删除
    EventManager().send_event(EventType.SiteDeleted,
                              {
                                  "site_id": "*"
                              })
    return schemas.Response(success=True, message="站点已重置！")


@router.post("/priorities", summary="批量更新站点优先级", response_model=schemas.Response)
def update_sites_priority(
        priorities: List[dict],
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    批量更新站点优先级
    """
    for priority in priorities:
        site = Site.get(db, priority.get("id"))
        if site:
            site.update(db, {"pri": priority.get("pri")})
    return schemas.Response(success=True)


@router.get("/cookie/{site_id}", summary="更新站点Cookie&UA", response_model=schemas.Response)
def update_cookie(
        site_id: int,
        username: str,
        password: str,
        code: str = None,
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    使用用户密码更新站点Cookie
    """
    # 查询站点
    site_info = Site.get(db, site_id)
    if not site_info:
        raise HTTPException(
            status_code=404,
            detail=f"站点 {site_id} 不存在！",
        )
    # 更新Cookie
    state, message = SiteChain().update_cookie(site_info=site_info,
                                               username=username,
                                               password=password,
                                               two_step_code=code)
    return schemas.Response(success=state, message=message)


@router.get("/test/{site_id}", summary="连接测试", response_model=schemas.Response)
def test_site(site_id: int,
              db: Session = Depends(get_db),
              _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    测试站点是否可用
    """
    site = Site.get(db, site_id)
    if not site:
        raise HTTPException(
            status_code=404,
            detail=f"站点 {site_id} 不存在",
        )
    status, message = SiteChain().test(site.domain)
    return schemas.Response(success=status, message=message)


@router.get("/icon/{site_id}", summary="站点图标", response_model=schemas.Response)
def site_icon(site_id: int,
              db: Session = Depends(get_db),
              _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    获取站点图标：base64或者url
    """
    site = Site.get(db, site_id)
    if not site:
        raise HTTPException(
            status_code=404,
            detail=f"站点 {site_id} 不存在",
        )
    icon = SiteIcon.get_by_domain(db, site.domain)
    if not icon:
        return schemas.Response(success=False, message="站点图标不存在！")
    return schemas.Response(success=True, data={
        "icon": icon.base64 if icon.base64 else icon.url
    })


@router.get("/resource/{site_id}", summary="站点资源", response_model=List[schemas.TorrentInfo])
def site_resource(site_id: int,
                  db: Session = Depends(get_db),
                  _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    浏览站点资源
    """
    site = Site.get(db, site_id)
    if not site:
        raise HTTPException(
            status_code=404,
            detail=f"站点 {site_id} 不存在",
        )
    torrents = TorrentsChain().browse(domain=site.domain)
    if not torrents:
        return []
    return [torrent.to_dict() for torrent in torrents]


@router.get("/domain/{site_url}", summary="站点详情", response_model=schemas.Site)
def read_site_by_domain(
        site_url: str,
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)
) -> Any:
    """
    通过域名获取站点信息
    """
    domain = StringUtils.get_url_domain(site_url)
    site = Site.get_by_domain(db, domain)
    if not site:
        raise HTTPException(
            status_code=404,
            detail=f"站点 {domain} 不存在",
        )
    return site


@router.get("/statistic/{site_url}", summary="站点统计信息", response_model=schemas.SiteStatistic)
def read_site_by_domain(
        site_url: str,
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)
) -> Any:
    """
    通过域名获取站点统计信息
    """
    domain = StringUtils.get_url_domain(site_url)
    sitestatistic = SiteStatistic.get_by_domain(db, domain)
    if sitestatistic:
        return sitestatistic
    return schemas.SiteStatistic(domain=domain)


@router.get("/rss", summary="所有订阅站点", response_model=List[schemas.Site])
def read_rss_sites(db: Session = Depends(get_db)) -> List[dict]:
    """
    获取站点列表
    """
    # 选中的rss站点
    selected_sites = SystemConfigOper().get(SystemConfigKey.RssSites) or []

    # 所有站点
    all_site = Site.list_order_by_pri(db)
    if not selected_sites:
        return all_site

    # 选中的rss站点
    rss_sites = [site for site in all_site if site and site.id in selected_sites]
    return rss_sites


@router.get("/{site_id}", summary="站点详情", response_model=schemas.Site)
def read_site(
        site_id: int,
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)
) -> Any:
    """
    通过ID获取站点信息
    """
    site = Site.get(db, site_id)
    if not site:
        raise HTTPException(
            status_code=404,
            detail=f"站点 {site_id} 不存在",
        )
    return site


@router.delete("/{site_id}", summary="删除站点", response_model=schemas.Response)
def delete_site(
        site_id: int,
        db: Session = Depends(get_db),
        _: User = Depends(get_current_active_superuser)
) -> Any:
    """
    删除站点
    """
    Site.delete(db, site_id)
    # 插件站点删除
    EventManager().send_event(EventType.SiteDeleted,
                              {
                                  "site_id": site_id
                              })
    return schemas.Response(success=True)
