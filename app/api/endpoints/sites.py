from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import schemas
from app.chain.cookiecloud import CookieCloudChain
from app.db import get_db
from app.db.models.site import Site
from app.db.models.user import User
from app.db.userauth import get_current_active_user

router = APIRouter()


@router.get("/", response_model=List[schemas.Site])
async def read_sites(db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_active_user)) -> List[dict]:
    """
    获取站点列表
    """
    if not current_user:
        raise HTTPException(
            status_code=400,
            detail="需要授权",
        )
    return Site.list(db)


@router.get("/cookiecloud", response_model=schemas.Response)
async def cookie_cloud_sync(current_user: User = Depends(get_current_active_user)) -> dict:
    """
    运行CookieCloud同步站点信息
    """
    if not current_user:
        raise HTTPException(
            status_code=400,
            detail="需要授权",
        )
    status, error_msg = CookieCloudChain().process()
    if not status:
        return {"success": False, "message": error_msg}
    return {"success": True, "message": error_msg}
