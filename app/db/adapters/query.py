"""插件只读数据查询的持久化端口适配器。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TypeVar

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.application.query import QueryRows
from app.db.oper.downloadhistory import DownloadHistoryOper
from app.db.oper.subscribe import SubscribeOper
from app.db.oper.subscribehistory import SubscribeHistoryOper
from app.db.oper.transferhistory import TransferHistoryOper
from app.schemas.query import (
    DownloadHistoryFilter,
    DownloadHistorySnapshot,
    QueryPageRequest,
    SubscriptionFilter,
    SubscriptionHistoryFilter,
    SubscriptionHistorySnapshot,
    SubscriptionSnapshot,
    TransferHistoryFilter,
    TransferHistorySnapshot,
)

_SnapshotT = TypeVar("_SnapshotT", bound=BaseModel)


class SqlAlchemyDataQueryAdapter:
    """以短生命周期 Session 和显式 Oper 实现统一查询端口。

    Oper 拥有表级筛选、排序和分页语义；适配器只管理一次操作使用的 Session，
    并在 Session 关闭前把持久化记录冻结为稳定 Pydantic 快照。
    """

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        """保存由启动组合根提供的同步 Session 工厂。"""
        self._session_factory = session_factory

    @staticmethod
    def _rows(
        snapshot_type: type[_SnapshotT],
        records: Sequence[object],
        total: int,
    ) -> QueryRows[_SnapshotT]:
        """在会话有效期内投影一页记录，并保护非负总数合同。"""
        return QueryRows(
            items=[snapshot_type.model_validate(record) for record in records],
            total=max(int(total), 0),
        )

    @staticmethod
    def _item(
        snapshot_type: type[_SnapshotT],
        record: object | None,
    ) -> _SnapshotT | None:
        """把单条持久化记录投影为稳定快照。"""
        return snapshot_type.model_validate(record) if record is not None else None

    def list_subscriptions(
        self,
        *,
        filters: SubscriptionFilter,
        page: QueryPageRequest,
    ) -> QueryRows[SubscriptionSnapshot]:
        """通过订阅 Oper 读取并冻结一页当前订阅。"""
        with self._session_factory() as session:
            records, total = SubscribeOper(session).query(filters, page)
            return self._rows(SubscriptionSnapshot, records, total)

    def get_subscription(self, subscription_id: int) -> SubscriptionSnapshot | None:
        """通过订阅 Oper 按 ID 读取并冻结当前订阅。"""
        with self._session_factory() as session:
            record = SubscribeOper(session).get_by_id(subscription_id)
            return self._item(SubscriptionSnapshot, record)

    def list_subscription_history(
        self,
        *,
        filters: SubscriptionHistoryFilter,
        page: QueryPageRequest,
    ) -> QueryRows[SubscriptionHistorySnapshot]:
        """通过订阅历史 Oper 读取并冻结一页完成历史。"""
        with self._session_factory() as session:
            records, total = SubscribeHistoryOper(session).query(filters, page)
            return self._rows(SubscriptionHistorySnapshot, records, total)

    def get_subscription_history(
        self,
        history_id: int,
    ) -> SubscriptionHistorySnapshot | None:
        """通过订阅历史 Oper 按 ID 读取并冻结完成历史。"""
        with self._session_factory() as session:
            record = SubscribeHistoryOper(session).get_by_id(history_id)
            return self._item(SubscriptionHistorySnapshot, record)

    def list_download_history(
        self,
        *,
        filters: DownloadHistoryFilter,
        page: QueryPageRequest,
    ) -> QueryRows[DownloadHistorySnapshot]:
        """通过下载历史 Oper 读取并冻结一页下载记录。"""
        with self._session_factory() as session:
            records, total = DownloadHistoryOper(session).query(filters, page)
            return self._rows(DownloadHistorySnapshot, records, total)

    def get_download_history(
        self,
        history_id: int,
    ) -> DownloadHistorySnapshot | None:
        """通过下载历史 Oper 按 ID 读取并冻结下载记录。"""
        with self._session_factory() as session:
            record = DownloadHistoryOper(session).get_by_id(history_id)
            return self._item(DownloadHistorySnapshot, record)

    def list_transfer_history(
        self,
        *,
        filters: TransferHistoryFilter,
        page: QueryPageRequest,
    ) -> QueryRows[TransferHistorySnapshot]:
        """通过整理历史 Oper 读取并冻结一页整理记录。"""
        with self._session_factory() as session:
            records, total = TransferHistoryOper(session).query(filters, page)
            return self._rows(TransferHistorySnapshot, records, total)

    def get_transfer_history(
        self,
        history_id: int,
    ) -> TransferHistorySnapshot | None:
        """通过整理历史 Oper 按 ID 读取并冻结整理记录。"""
        with self._session_factory() as session:
            record = TransferHistoryOper(session).get_by_id(history_id)
            return self._item(TransferHistorySnapshot, record)


__all__ = ["SqlAlchemyDataQueryAdapter"]
