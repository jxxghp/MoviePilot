from typing import List, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import schemas
from app.chain.cookiecloud import CookieCloudChain
from app.db import get_db
from app.db.models.site import Site
from app.db.models.user import User
from app.db.userauth import get_current_active_user, get_current_active_superuser

router = APIRouter()


@router.get("/", response_model=List[schemas.Site])
async def read_sites(db: Session = Depends(get_db),
                     _: User = Depends(get_current_active_user)) -> List[dict]:
    """
    获取站点列表
    """
    return Site.list(db)


@router.put("/", response_model=schemas.Site)
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


@router.get("/cookiecloud", response_model=schemas.Response)
async def cookie_cloud_sync(_: User = Depends(get_current_active_user)) -> dict:
    """
    运行CookieCloud同步站点信息
    """
    status, error_msg = CookieCloudChain().process()
    if not status:
        return {"success": False, "message": error_msg}
    return {"success": True, "message": error_msg}
