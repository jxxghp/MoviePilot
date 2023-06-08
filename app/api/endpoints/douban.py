from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import schemas
from app.chain.douban_sync import DoubanSyncChain
from app.db import get_db
from app.db.models.user import User
from app.db.userauth import get_current_active_superuser

router = APIRouter()


@router.get("/sync", response_model=schemas.Response)
async def sync_douban(
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_active_superuser)):
    """
    查询所有订阅
    """
    if not current_user:
        raise HTTPException(
            status_code=400,
            detail="需要授权",
        )
    DoubanSyncChain().process()
    return {"success": True}
