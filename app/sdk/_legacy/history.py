"""把旧整理历史 Oper 的业务写入方法转交给应用服务。"""

from typing import Any, Optional, cast

from sqlalchemy.orm import Session

from app.application.history import (
    TransferHistoryReplacePort,
    TransferHistorySnapshot,
    TransferHistoryWrite,
    add_transfer_fail,
    add_transfer_success,
)
from app.db.adapters.history.transfer import (
    SessionTransferHistoryRepository,
    TransactionalTransferHistoryRepository,
)
from app.db.oper.transferhistory import TransferHistoryOper as CanonicalTransferHistoryOper
from app.db.session import SessionFactory, async_session_scope
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.schemas.file import FileItem
from app.schemas.transfer import TransferInfo


class _LegacyTransferHistoryStager:
    """把旧 Oper 写入口适配到 Application 类型化暂存合同。"""

    def __init__(self, owner: "TransferHistoryOper") -> None:
        """保存兼容 Oper。"""
        self._owner = owner

    def replace(self, history: TransferHistoryWrite) -> TransferHistorySnapshot:
        """把类型化写入转回旧 add_force ABI。"""
        return cast(
            TransferHistorySnapshot,
            self._owner.add_force(**history.to_payload()),
        )


class TransferHistoryOper(CanonicalTransferHistoryOper):
    """继承新的查询接口，并保留旧的成功/失败历史写入方法。"""

    def add_success(
            self,
            fileitem: FileItem,
            mode: str,
            meta: MetaBase,
            mediainfo: MediaInfo | MusicInfo,
            transferinfo: TransferInfo,
            downloader: Optional[str] = None,
            download_hash: Optional[str] = None,
    ) -> Optional[Any]:
        """
        按旧签名新增整理成功历史。

        :return: 落库后的整理记录
        """
        return add_transfer_success(
            fileitem=fileitem,
            mode=mode,
            meta=meta,
            mediainfo=mediainfo,
            transferinfo=transferinfo,
            downloader=downloader,
            download_hash=download_hash,
            transfer_history_oper=_LegacyTransferHistoryStager(self),
        )

    def add_fail(
            self,
            fileitem: FileItem,
            mode: str,
            meta: MetaBase,
            mediainfo: Optional[MediaInfo | MusicInfo] = None,
            transferinfo: Optional[TransferInfo] = None,
            downloader: Optional[str] = None,
            download_hash: Optional[str] = None,
    ) -> Optional[Any]:
        """
        按旧签名新增整理失败历史。

        :return: 落库后的整理记录
        """
        return add_transfer_fail(
            fileitem=fileitem,
            mode=mode,
            meta=meta,
            mediainfo=mediainfo,
            transferinfo=transferinfo,
            downloader=downloader,
            download_hash=download_hash,
            transfer_history_oper=_LegacyTransferHistoryStager(self),
        )

    def add_force(  # type: ignore[no-untyped-def]
        self,
        **kwargs,
    ) -> Optional[TransferHistorySnapshot]:
        """按旧动态字段签名替换同源历史，并返回脱离 Session 的快照。"""
        payload = dict(kwargs)
        payload.pop("date", None)
        repository: TransferHistoryReplacePort
        if isinstance(getattr(self, "_db", None), Session):
            repository = SessionTransferHistoryRepository(self._db)
        else:
            repository = TransactionalTransferHistoryRepository(
                sync_session=SessionFactory,
                async_session=async_session_scope,
            )
        return repository.replace(TransferHistoryWrite(**payload))


__all__ = ["TransferHistoryOper"]
