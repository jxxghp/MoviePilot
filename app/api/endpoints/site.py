from typing import List, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import schemas
from app.chain.cookiecloud import CookieCloudChain
from app.chain.site import SiteChain
from app.db import get_db
from app.db.models.site import Site
from app.db.models.user import User
from app.db.siteicon_oper import SiteIconOper
from app.db.userauth import get_current_active_user, get_current_active_superuser

router = APIRouter()


@router.get("/", summary="所有站点", response_model=List[schemas.Site])
async def read_sites(db: Session = Depends(get_db),
                     _: User = Depends(get_current_active_user)) -> List[dict]:
    """
    获取站点列表
    """
    return Site.list(db)


@router.put("/", summary="更新站点", response_model=schemas.Site)
async def update_site(
        *,
        db: Session = Depends(get_db),
        site_in: schemas.Site,
        _: User = Depends(get_current_active_superuser),
) -> Any:
    """
    更新站点信息
    """
    site = Site.get(db, site_in.id)
    if not site:
        raise HTTPException(
            status_code=404,
            detail=f"站点 {site_in.id} 不存在",
        )
    site.update(db, **site_in.dict())
    return site


@router.get("/{site_id}", summary="站点详情", response_model=schemas.Site)
async def read_site(
        site_id: int,
        db: Session = Depends(get_db),
        _: User = Depends(get_current_active_user),
) -> Any:
    """
    获取站点信息
    """
    site = Site.get(db, site_id)
    if not site:
        raise HTTPException(
            status_code=404,
            detail=f"站点 {site_id} 不存在",
        )
    return site


@router.get("/cookiecloud", summary="CookieCloud同步", response_model=schemas.Response)
async def cookie_cloud_sync(_: User = Depends(get_current_active_user)) -> Any:
    """
    运行CookieCloud同步站点信息
    """
    status, error_msg = CookieCloudChain().process()
    if not status:
        schemas.Response(success=True, message=error_msg)
    return schemas.Response(success=True, message="同步成功！")


@router.get("/cookie", summary="更新站点Cookie&UA", response_model=schemas.Response)
async def update_cookie(
        site_id: int,
        username: str,
        password: str,
        db: Session = Depends(get_db),
        _: User = Depends(get_current_active_user)) -> Any:
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
    status, msg = SiteChain().update_cookie(site_info=site_info,
                                            username=username,
                                            password=password)
    if not status:
        return schemas.Response(success=False, message=msg)
    else:
        return schemas.Response(success=True, message=msg)


@router.get("/test", summary="连接测试", response_model=schemas.Response)
async def test_site(domain: str, _: User = Depends(get_current_active_user)) -> Any:
    """
    测试站点是否可用
    """
    status, message = SiteChain().test(domain)
    return schemas.Response(success=status, message=message)


@router.get("/icon", summary="站点图标", response_model=schemas.Response)
async def site_icon(domain: str, _: User = Depends(get_current_active_user)) -> Any:
    """
    获取站点图标：base64或者url
    """
    icon = SiteIconOper().get_by_domain(domain)
    if not icon:
        return schemas.Response(success=False, message="站点图标不存在！")
    return schemas.Response(success=True, data={
        "icon": icon.base64 if icon.base64 else icon.url
    })
