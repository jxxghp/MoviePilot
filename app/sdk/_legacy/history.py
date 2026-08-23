"""把旧整理历史 Oper 的业务写入方法转交给应用服务。"""

from typing import Any, Optional

from app.application.history import add_transfer_fail, add_transfer_success
from app.db.oper.transferhistory import TransferHistoryOper as CanonicalTransferHistoryOper
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.schemas.file import FileItem
from app.schemas.transfer import TransferInfo


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
            transfer_history_oper=self,
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
            transfer_history_oper=self,
        )


__all__ = ["TransferHistoryOper"]
