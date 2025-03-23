from typing import List, Any, Optional

import jieba
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import schemas
from app.chain.storage import StorageChain
from app.core.config import settings
from app.core.event import eventmanager
from app.core.security import verify_token
from app.db import get_db
from app.db.models import User
from app.db.models.downloadhistory import DownloadHistory
from app.db.models.transferhistory import TransferHistory
from app.db.user_oper import get_current_active_superuser
from app.schemas.types import EventType, MediaType

router = APIRouter()


@router.get("/download", summary="查询下载历史记录", response_model=List[schemas.DownloadHistory])
def download_history(page: Optional[int] = 1,
                     count: Optional[int] = 30,
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


@router.get("/transfer", summary="查询整理记录", response_model=schemas.Response)
def transfer_history(title: Optional[str] = None,
                     page: Optional[int] = 1,
                     count: Optional[int] = 30,
                     status: Optional[bool] = None,
                     db: Session = Depends(get_db),
                     _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询整理记录
    """
    if title == "失败":
        title = None
        status = False
    elif title == "成功":
        title = None
        status = True

    if title:
        if settings.TOKENIZED_SEARCH:
            words = jieba.cut(title, HMM=False)
            title = "%".join(words)
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


@router.delete("/transfer", summary="删除整理记录", response_model=schemas.Response)
def delete_transfer_history(history_in: schemas.TransferHistory,
                            deletesrc: Optional[bool] = False,
                            deletedest: Optional[bool] = False,
                            db: Session = Depends(get_db),
                            _: schemas.TokenPayload = Depends(get_current_active_superuser)) -> Any:
    """
    删除整理记录
    """
    history: TransferHistory = TransferHistory.get(db, history_in.id)
    if not history:
        return schemas.Response(success=False, message="记录不存在")
    # 册除媒体库文件
    if deletedest and history.dest_fileitem:
        dest_fileitem = schemas.FileItem(**history.dest_fileitem)
        StorageChain().delete_media_file(fileitem=dest_fileitem, mtype=MediaType(history.type))

    # 删除源文件
    if deletesrc and history.src_fileitem:
        src_fileitem = schemas.FileItem(**history.src_fileitem)
        state = StorageChain().delete_media_file(src_fileitem)
        if not state:
            return schemas.Response(success=False, message=f"{src_fileitem.path} 删除失败")
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


@router.get("/empty/transfer", summary="清空整理记录", response_model=schemas.Response)
def delete_transfer_history(db: Session = Depends(get_db),
                            _: User = Depends(get_current_active_superuser)) -> Any:
    """
    清空整理记录
    """
    TransferHistory.truncate(db)
    return schemas.Response(success=True)
