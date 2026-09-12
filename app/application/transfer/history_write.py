"""整理历史成功与失败记录的稳定写入口。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Union

from app.application.transfer import history as history_projection
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.schemas.file import FileItem
from app.schemas.transfer import TransferInfo

if TYPE_CHECKING:
    from app.application.history import (
        TransferHistoryReplacePort,
        TransferHistorySnapshot,
    )


def add_transfer_success(
    fileitem: FileItem,
    mode: str,
    meta: MetaBase,
    mediainfo: Union[MediaInfo, MusicInfo],
    transferinfo: TransferInfo,
    downloader: Optional[str] = None,
    download_hash: Optional[str] = None,
    transfer_batch_id: Optional[str] = None,
    transfer_batch_title: Optional[str] = None,
    transfer_batch_root: Optional[str] = None,
    transfer_batch_total: Optional[int] = None,
    transfer_history_oper: Optional[TransferHistoryReplacePort] = None,
) -> TransferHistorySnapshot:
    """新增转移成功历史记录。"""
    from app.application.history import (
        TransferHistoryWrite,
        get_transfer_history_repository,
    )

    repository = transfer_history_oper or get_transfer_history_repository()
    fields = history_projection.success_fields(
        fileitem=fileitem,
        mode=mode,
        meta=meta,
        mediainfo=mediainfo,
        transferinfo=transferinfo,
        downloader=downloader,
        download_hash=download_hash,
        transfer_batch_id=transfer_batch_id,
        transfer_batch_title=transfer_batch_title,
        transfer_batch_root=transfer_batch_root,
        transfer_batch_total=transfer_batch_total,
    )
    return repository.replace(TransferHistoryWrite(**fields))


def add_transfer_fail(
    fileitem: FileItem,
    mode: str,
    meta: MetaBase,
    mediainfo: Optional[Union[MediaInfo, MusicInfo]] = None,
    transferinfo: Optional[TransferInfo] = None,
    downloader: Optional[str] = None,
    download_hash: Optional[str] = None,
    retry_count: Optional[int] = None,
    auto_paused: bool = False,
    transfer_batch_id: Optional[str] = None,
    transfer_batch_title: Optional[str] = None,
    transfer_batch_root: Optional[str] = None,
    transfer_batch_total: Optional[int] = None,
    transfer_history_oper: Optional[TransferHistoryReplacePort] = None,
) -> TransferHistorySnapshot:
    """新增转移失败历史记录。"""
    from app.application.history import (
        TransferHistoryWrite,
        get_transfer_history_repository,
    )

    repository = transfer_history_oper or get_transfer_history_repository()
    fields = history_projection.failure_fields(
        fileitem=fileitem,
        mode=mode,
        meta=meta,
        mediainfo=mediainfo,
        transferinfo=transferinfo,
        downloader=downloader,
        download_hash=download_hash,
        retry_count=retry_count,
        auto_paused=auto_paused,
        transfer_batch_id=transfer_batch_id,
        transfer_batch_title=transfer_batch_title,
        transfer_batch_root=transfer_batch_root,
        transfer_batch_total=transfer_batch_total,
    )
    return repository.replace(TransferHistoryWrite(**fields))
