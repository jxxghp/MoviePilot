"""插件只读数据查询的应用服务与领域端口。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any, Callable, Generic, Protocol, TypeVar, cast

from pydantic import BaseModel

from app.application.database import AsyncDatabaseExecutor
from app.schemas.query import (
    DownloadHistoryFilter,
    DownloadHistorySnapshot,
    QueryPage,
    QueryPageRequest,
    SubscriptionFilter,
    SubscriptionHistoryFilter,
    SubscriptionHistorySnapshot,
    SubscriptionSnapshot,
    TransferHistoryFilter,
    TransferHistorySnapshot,
)

RecordT = TypeVar("RecordT", covariant=True)
DtoT = TypeVar("DtoT", bound=BaseModel)
ResultT = TypeVar("ResultT")


@dataclass(frozen=True, slots=True)
class QueryRows(Generic[RecordT]):
    """查询端口返回的短生命周期结果，不跨越应用服务边界。"""

    items: Sequence[RecordT]
    total: int


class SubscriptionQueryPort(Protocol):
    """订阅领域查询端口，包含订阅和订阅完成历史两个只读切片。"""

    def list_subscriptions(
        self,
        *,
        filters: SubscriptionFilter,
        page: QueryPageRequest,
    ) -> QueryRows[object]:
        """按组合筛选和稳定分页读取订阅记录。"""
        ...

    def get_subscription(self, subscription_id: int) -> object | None:
        """按 ID 读取一条订阅记录。"""
        ...

    def list_subscription_history(
        self,
        *,
        filters: SubscriptionHistoryFilter,
        page: QueryPageRequest,
    ) -> QueryRows[object]:
        """按组合筛选和稳定分页读取订阅完成历史。"""
        ...

    def get_subscription_history(self, history_id: int) -> object | None:
        """按 ID 读取一条订阅完成历史。"""
        ...


class HistoryQueryPort(Protocol):
    """下载与整理历史查询端口。"""

    def list_download_history(
        self,
        *,
        filters: DownloadHistoryFilter,
        page: QueryPageRequest,
    ) -> QueryRows[object]:
        """按组合筛选和稳定分页读取下载历史。"""
        ...

    def get_download_history(self, history_id: int) -> object | None:
        """按 ID 读取一条下载历史。"""
        ...

    def list_transfer_history(
        self,
        *,
        filters: TransferHistoryFilter,
        page: QueryPageRequest,
    ) -> QueryRows[object]:
        """按组合筛选和稳定分页读取整理历史。"""
        ...

    def get_transfer_history(self, history_id: int) -> object | None:
        """按 ID 读取一条整理历史。"""
        ...


class DataQueryService:
    """把订阅和历史查询统一投影为不含持久化实现的 DTO。"""

    def __init__(
        self,
        *,
        subscriptions: SubscriptionQueryPort,
        histories: HistoryQueryPort,
        async_executor: AsyncDatabaseExecutor,
    ) -> None:
        """保存两个领域查询端口和异步数据库执行边界。"""
        self._subscriptions = subscriptions
        self._histories = histories
        self._async_executor = async_executor

    @staticmethod
    def _page_request(page: QueryPageRequest | dict[str, Any] | None) -> QueryPageRequest:
        """把调用方输入规范化为有界分页合同。"""
        if page is None:
            return QueryPageRequest()
        if isinstance(page, QueryPageRequest):
            return page
        return cast(QueryPageRequest, QueryPageRequest.model_validate(page))

    @staticmethod
    def _filter(
        value: Any,
        filter_type: type[SubscriptionFilter]
        | type[SubscriptionHistoryFilter]
        | type[DownloadHistoryFilter]
        | type[TransferHistoryFilter],
    ) -> Any:
        """把字典筛选条件转换为带媒体身份校验的模型。"""
        if value is None:
            return filter_type()
        if isinstance(value, filter_type):
            return value
        return filter_type.model_validate(value)

    @staticmethod
    def _rows(value: QueryRows[object]) -> QueryRows[object]:
        """校验端口结果的总数，避免负数污染分页合同。"""
        if not isinstance(value, QueryRows):
            raise TypeError("查询端口必须返回 QueryRows")
        return QueryRows(
            items=value.items,
            total=max(int(value.total), 0),
        )

    @classmethod
    def _to_page(
        cls,
        dto_type: type[DtoT],
        page: QueryPageRequest,
        rows: QueryRows[object],
    ) -> QueryPage[DtoT]:
        """在查询端口结果离开应用层前完成 DTO 投影。"""
        normalized_rows = cls._rows(rows)
        return cast(
            QueryPage[DtoT],
            QueryPage(
                items=[dto_type.model_validate(item) for item in normalized_rows.items],
                total=normalized_rows.total,
                page=page.page,
                count=page.count,
            ),
        )

    @staticmethod
    def _to_item(dto_type: type[DtoT], item: object | None) -> DtoT | None:
        """把单条端口记录投影为稳定 DTO。"""
        return dto_type.model_validate(item) if item is not None else None

    async def _async_run(self, operation: Callable[[], ResultT]) -> ResultT:
        """通过统一数据库执行器运行一个同步查询用例。"""
        return cast(ResultT, await self._async_executor.run(operation))

    def list_subscriptions(
        self,
        filters: SubscriptionFilter | dict[str, Any] | None = None,
        page: QueryPageRequest | dict[str, Any] | None = None,
    ) -> QueryPage[SubscriptionSnapshot]:
        """同步分页查询订阅。"""
        normalized_page = self._page_request(page)
        normalized_filters = self._filter(filters, SubscriptionFilter)
        rows = self._subscriptions.list_subscriptions(
            filters=normalized_filters,
            page=normalized_page,
        )
        return self._to_page(SubscriptionSnapshot, normalized_page, rows)

    async def async_list_subscriptions(
        self,
        filters: SubscriptionFilter | dict[str, Any] | None = None,
        page: QueryPageRequest | dict[str, Any] | None = None,
    ) -> QueryPage[SubscriptionSnapshot]:
        """异步分页查询订阅，业务规则在数据库 worker 中复用同步入口。"""
        return await self._async_run(partial(self.list_subscriptions, filters, page))

    def get_subscription(self, subscription_id: int) -> SubscriptionSnapshot | None:
        """同步按 ID 查询订阅。"""
        return self._to_item(
            SubscriptionSnapshot,
            self._subscriptions.get_subscription(subscription_id),
        )

    async def async_get_subscription(
        self,
        subscription_id: int,
    ) -> SubscriptionSnapshot | None:
        """异步按 ID 查询订阅。"""
        return await self._async_run(partial(self.get_subscription, subscription_id))

    def list_subscription_history(
        self,
        filters: SubscriptionHistoryFilter | dict[str, Any] | None = None,
        page: QueryPageRequest | dict[str, Any] | None = None,
    ) -> QueryPage[SubscriptionHistorySnapshot]:
        """同步分页查询订阅完成历史。"""
        normalized_page = self._page_request(page)
        normalized_filters = self._filter(filters, SubscriptionHistoryFilter)
        rows = self._subscriptions.list_subscription_history(
            filters=normalized_filters,
            page=normalized_page,
        )
        return self._to_page(SubscriptionHistorySnapshot, normalized_page, rows)

    async def async_list_subscription_history(
        self,
        filters: SubscriptionHistoryFilter | dict[str, Any] | None = None,
        page: QueryPageRequest | dict[str, Any] | None = None,
    ) -> QueryPage[SubscriptionHistorySnapshot]:
        """异步分页查询订阅完成历史。"""
        return await self._async_run(partial(self.list_subscription_history, filters, page))

    def get_subscription_history(
        self,
        history_id: int,
    ) -> SubscriptionHistorySnapshot | None:
        """同步按 ID 查询订阅完成历史。"""
        return self._to_item(
            SubscriptionHistorySnapshot,
            self._subscriptions.get_subscription_history(history_id),
        )

    async def async_get_subscription_history(
        self,
        history_id: int,
    ) -> SubscriptionHistorySnapshot | None:
        """异步按 ID 查询订阅完成历史。"""
        return await self._async_run(partial(self.get_subscription_history, history_id))

    def list_download_history(
        self,
        filters: DownloadHistoryFilter | dict[str, Any] | None = None,
        page: QueryPageRequest | dict[str, Any] | None = None,
    ) -> QueryPage[DownloadHistorySnapshot]:
        """同步分页查询下载历史。"""
        normalized_page = self._page_request(page)
        normalized_filters = self._filter(filters, DownloadHistoryFilter)
        rows = self._histories.list_download_history(
            filters=normalized_filters,
            page=normalized_page,
        )
        return self._to_page(DownloadHistorySnapshot, normalized_page, rows)

    async def async_list_download_history(
        self,
        filters: DownloadHistoryFilter | dict[str, Any] | None = None,
        page: QueryPageRequest | dict[str, Any] | None = None,
    ) -> QueryPage[DownloadHistorySnapshot]:
        """异步分页查询下载历史。"""
        return await self._async_run(partial(self.list_download_history, filters, page))

    def get_download_history(self, history_id: int) -> DownloadHistorySnapshot | None:
        """同步按 ID 查询下载历史。"""
        return self._to_item(
            DownloadHistorySnapshot,
            self._histories.get_download_history(history_id),
        )

    async def async_get_download_history(
        self,
        history_id: int,
    ) -> DownloadHistorySnapshot | None:
        """异步按 ID 查询下载历史。"""
        return await self._async_run(partial(self.get_download_history, history_id))

    def list_transfer_history(
        self,
        filters: TransferHistoryFilter | dict[str, Any] | None = None,
        page: QueryPageRequest | dict[str, Any] | None = None,
    ) -> QueryPage[TransferHistorySnapshot]:
        """同步分页查询整理历史。"""
        normalized_page = self._page_request(page)
        normalized_filters = self._filter(filters, TransferHistoryFilter)
        rows = self._histories.list_transfer_history(
            filters=normalized_filters,
            page=normalized_page,
        )
        return self._to_page(TransferHistorySnapshot, normalized_page, rows)

    async def async_list_transfer_history(
        self,
        filters: TransferHistoryFilter | dict[str, Any] | None = None,
        page: QueryPageRequest | dict[str, Any] | None = None,
    ) -> QueryPage[TransferHistorySnapshot]:
        """异步分页查询整理历史。"""
        return await self._async_run(partial(self.list_transfer_history, filters, page))

    def get_transfer_history(self, history_id: int) -> TransferHistorySnapshot | None:
        """同步按 ID 查询整理历史。"""
        return self._to_item(
            TransferHistorySnapshot,
            self._histories.get_transfer_history(history_id),
        )

    async def async_get_transfer_history(
        self,
        history_id: int,
    ) -> TransferHistorySnapshot | None:
        """异步按 ID 查询整理历史。"""
        return await self._async_run(partial(self.get_transfer_history, history_id))


_configured_data_query_service: DataQueryService | None = None


def configure_data_query_service(service: DataQueryService) -> None:
    """由启动组合根登记插件数据查询服务。"""
    global _configured_data_query_service
    _configured_data_query_service = service


def get_configured_data_query_service() -> DataQueryService:
    """返回启动阶段登记的插件数据查询服务。"""
    if _configured_data_query_service is None:
        raise RuntimeError("插件数据查询服务尚未配置")
    return _configured_data_query_service


__all__ = [
    "DataQueryService",
    "HistoryQueryPort",
    "QueryRows",
    "SubscriptionQueryPort",
    "configure_data_query_service",
    "get_configured_data_query_service",
]
