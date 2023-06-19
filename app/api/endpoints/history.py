from typing import List, Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import schemas
from app.db import get_db
from app.db.models.downloadhistory import DownloadHistory
from app.db.models.transferhistory import TransferHistory
from app.db.models.user import User
from app.db.userauth import get_current_active_user

router = APIRouter()


@router.get("/download", summary="下载历史记录", response_model=List[schemas.DownloadHistory])
async def download_history(page: int = 1,
                           count: int = 30,
                           db: Session = Depends(get_db),
                           _: User = Depends(get_current_active_user)) -> Any:
    """
    查询下载历史记录
    """
    return DownloadHistory.list_by_page(db, page, count)


@router.get("/transfer", summary="转移历史记录", response_model=List[schemas.TransferHistory])
async def transfer_history(title: str = None,
                           page: int = 1,
                           count: int = 30,
                           db: Session = Depends(get_db),
                           _: User = Depends(get_current_active_user)) -> Any:
    """
    查询转移历史记录
    """
    if title:
        return TransferHistory.list_by_title(db, title, page, count)
    else:
        return TransferHistory.list_by_page(db, page, count)
