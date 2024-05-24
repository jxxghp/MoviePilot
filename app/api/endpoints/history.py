from pathlib import Path
from typing import List, Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import schemas
from app.chain.transfer import TransferChain
from app.core.event import eventmanager
from app.core.security import verify_token
from app.db import get_db
from app.db.models import User
from app.db.models.downloadhistory import DownloadHistory
from app.db.models.transferhistory import TransferHistory
from app.db.userauth import get_current_active_superuser
from app.schemas.types import EventType

router = APIRouter()


@router.get("/download", summary="查询下载历史记录", response_model=List[schemas.DownloadHistory])
def download_history(page: int = 1,
                     count: int = 30,
                     db: Session = Depends(get_db),
                     _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询下载历史记录
    """
    return DownloadHistory.list_by_page(db, page, count)


@router.delete("/download", summary="删除下载历史记录", response_model=schemas.Response)
def delete_download_history(history_in: schemas.DownloadHistory,
                            db: Session = Depends(get_db),
                            _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    删除下载历史记录
    """
    DownloadHistory.delete(db, history_in.id)
    return schemas.Response(success=True)


@router.get("/transfer", summary="查询转移历史记录", response_model=schemas.Response)
def transfer_history(title: str = None,
                     page: int = 1,
                     count: int = 30,
                     status: bool = None,
                     db: Session = Depends(get_db),
                     _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询转移历史记录
    """
    if title == "失败":
        title = None
        status = False
    elif title == "成功":
        title = None
        status = True

    if title:
        total = TransferHistory.count_by_title(db, title=title, status=status)
        result = TransferHistory.list_by_title(db, title=title, page=page,
                                               count=count, status=status)
    else:
        result = TransferHistory.list_by_page(db, page=page, count=count, status=status)
        total = TransferHistory.count(db, status=status)

    return schemas.Response(success=True,
                            data={
                                "list": result,
                                "total": total,
                            })


@router.delete("/transfer", summary="删除转移历史记录", response_model=schemas.Response)
def delete_transfer_history(history_in: schemas.TransferHistory,
                            deletesrc: bool = False,
                            deletedest: bool = False,
                            db: Session = Depends(get_db),
                            _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    删除转移历史记录
    """
    history = TransferHistory.get(db, history_in.id)
    if not history:
        return schemas.Response(success=False, msg="记录不存在")
    # 册除媒体库文件
    if deletedest and history.dest:
        state, msg = TransferChain().delete_files(Path(history.dest))
        if not state:
            return schemas.Response(success=False, msg=msg)
    # 删除源文件
    if deletesrc and history.src:
        state, msg = TransferChain().delete_files(Path(history.src))
        if not state:
            return schemas.Response(success=False, msg=msg)
        # 发送事件
        eventmanager.send_event(
            EventType.DownloadFileDeleted,
            {
                "src": history.src,
                "hash": history.download_hash
            }
        )
    # 删除记录
    TransferHistory.delete(db, history_in.id)
    return schemas.Response(success=True)


@router.get("/empty/transfer", summary="清空转移历史记录", response_model=schemas.Response)
def delete_transfer_history(db: Session = Depends(get_db),
                            _: User = Depends(get_current_active_superuser)) -> Any:
    """
    清空转移历史记录
    """
    TransferHistory.truncate(db)
    return schemas.Response(success=True)
