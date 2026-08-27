"""插件可使用的订阅与历史只读查询门面。"""

from __future__ import annotations

from typing import Any, Protocol, cast

from app.schemas.query import (
    DEFAULT_QUERY_PAGE_SIZE,
    MAX_QUERY_PAGE_SIZE,
    DownloadHistoryFilter,
    DownloadHistorySnapshot,
    MediaIdentityQuery,
    QueryPage,
    QueryPageRequest,
    QuerySort,
    QuerySortDirection,
    QuerySortField,
    SubscriptionFilter,
    SubscriptionHistoryFilter,
    SubscriptionHistorySnapshot,
    SubscriptionSnapshot,
    TransferHistoryFilter,
    TransferHistorySnapshot,
)


class _DataQueryBackend(Protocol):
    """SDK 转发所需的最小类型合同，不向插件公开应用服务实现。"""

    def list_subscriptions(
        self,
        filters: SubscriptionFilter | dict[str, Any] | None = None,
        page: QueryPageRequest | dict[str, Any] | None = None,
    ) -> QueryPage[SubscriptionSnapshot]: ...

    async def async_list_subscriptions(
        self,
        filters: SubscriptionFilter | dict[str, Any] | None = None,
        page: QueryPageRequest | dict[str, Any] | None = None,
    ) -> QueryPage[SubscriptionSnapshot]: ...

    def get_subscription(
        self,
        subscription_id: int,
    ) -> SubscriptionSnapshot | None: ...

    async def async_get_subscription(
        self,
        subscription_id: int,
    ) -> SubscriptionSnapshot | None: ...

    def list_subscription_history(
        self,
        filters: SubscriptionHistoryFilter | dict[str, Any] | None = None,
        page: QueryPageRequest | dict[str, Any] | None = None,
    ) -> QueryPage[SubscriptionHistorySnapshot]: ...

    async def async_list_subscription_history(
        self,
        filters: SubscriptionHistoryFilter | dict[str, Any] | None = None,
        page: QueryPageRequest | dict[str, Any] | None = None,
    ) -> QueryPage[SubscriptionHistorySnapshot]: ...

    def get_subscription_history(
        self,
        history_id: int,
    ) -> SubscriptionHistorySnapshot | None: ...

    async def async_get_subscription_history(
        self,
        history_id: int,
    ) -> SubscriptionHistorySnapshot | None: ...

    def list_download_history(
        self,
        filters: DownloadHistoryFilter | dict[str, Any] | None = None,
        page: QueryPageRequest | dict[str, Any] | None = None,
    ) -> QueryPage[DownloadHistorySnapshot]: ...

    async def async_list_download_history(
        self,
        filters: DownloadHistoryFilter | dict[str, Any] | None = None,
        page: QueryPageRequest | dict[str, Any] | None = None,
    ) -> QueryPage[DownloadHistorySnapshot]: ...

    def get_download_history(
        self,
        history_id: int,
    ) -> DownloadHistorySnapshot | None: ...

    async def async_get_download_history(
        self,
        history_id: int,
    ) -> DownloadHistorySnapshot | None: ...

    def list_transfer_history(
        self,
        filters: TransferHistoryFilter | dict[str, Any] | None = None,
        page: QueryPageRequest | dict[str, Any] | None = None,
    ) -> QueryPage[TransferHistorySnapshot]: ...

    async def async_list_transfer_history(
        self,
        filters: TransferHistoryFilter | dict[str, Any] | None = None,
        page: QueryPageRequest | dict[str, Any] | None = None,
    ) -> QueryPage[TransferHistorySnapshot]: ...

    def get_transfer_history(
        self,
        history_id: int,
    ) -> TransferHistorySnapshot | None: ...

    async def async_get_transfer_history(
        self,
        history_id: int,
    ) -> TransferHistorySnapshot | None: ...


def _service() -> _DataQueryBackend:
    """获取启动阶段登记的查询服务，避免把应用服务暴露为 SDK 合同。"""
    from app.application.query import (
        get_configured_data_query_service,
    )

    return cast(_DataQueryBackend, get_configured_data_query_service())


def list_subscriptions(
    filters: SubscriptionFilter | dict[str, Any] | None = None,
    page: QueryPageRequest | dict[str, Any] | None = None,
) -> QueryPage[SubscriptionSnapshot]:
    """同步分页读取订阅 DTO。"""
    return _service().list_subscriptions(filters, page)


async def async_list_subscriptions(
    filters: SubscriptionFilter | dict[str, Any] | None = None,
    page: QueryPageRequest | dict[str, Any] | None = None,
) -> QueryPage[SubscriptionSnapshot]:
    """异步分页读取订阅 DTO。"""
    return await _service().async_list_subscriptions(filters, page)


def get_subscription(subscription_id: int) -> SubscriptionSnapshot | None:
    """同步按 ID 读取订阅 DTO。"""
    return _service().get_subscription(subscription_id)


async def async_get_subscription(
    subscription_id: int,
) -> SubscriptionSnapshot | None:
    """异步按 ID 读取订阅 DTO。"""
    return await _service().async_get_subscription(subscription_id)


def list_subscription_history(
    filters: SubscriptionHistoryFilter | dict[str, Any] | None = None,
    page: QueryPageRequest | dict[str, Any] | None = None,
) -> QueryPage[SubscriptionHistorySnapshot]:
    """同步分页读取订阅完成历史 DTO。"""
    return _service().list_subscription_history(filters, page)


async def async_list_subscription_history(
    filters: SubscriptionHistoryFilter | dict[str, Any] | None = None,
    page: QueryPageRequest | dict[str, Any] | None = None,
) -> QueryPage[SubscriptionHistorySnapshot]:
    """异步分页读取订阅完成历史 DTO。"""
    return await _service().async_list_subscription_history(filters, page)


def get_subscription_history(history_id: int) -> SubscriptionHistorySnapshot | None:
    """同步按 ID 读取订阅完成历史 DTO。"""
    return _service().get_subscription_history(history_id)


async def async_get_subscription_history(
    history_id: int,
) -> SubscriptionHistorySnapshot | None:
    """异步按 ID 读取订阅完成历史 DTO。"""
    return await _service().async_get_subscription_history(history_id)


def list_download_history(
    filters: DownloadHistoryFilter | dict[str, Any] | None = None,
    page: QueryPageRequest | dict[str, Any] | None = None,
) -> QueryPage[DownloadHistorySnapshot]:
    """同步分页读取下载历史 DTO。"""
    return _service().list_download_history(filters, page)


async def async_list_download_history(
    filters: DownloadHistoryFilter | dict[str, Any] | None = None,
    page: QueryPageRequest | dict[str, Any] | None = None,
) -> QueryPage[DownloadHistorySnapshot]:
    """异步分页读取下载历史 DTO。"""
    return await _service().async_list_download_history(filters, page)


def get_download_history(history_id: int) -> DownloadHistorySnapshot | None:
    """同步按 ID 读取下载历史 DTO。"""
    return _service().get_download_history(history_id)


async def async_get_download_history(
    history_id: int,
) -> DownloadHistorySnapshot | None:
    """异步按 ID 读取下载历史 DTO。"""
    return await _service().async_get_download_history(history_id)


def list_transfer_history(
    filters: TransferHistoryFilter | dict[str, Any] | None = None,
    page: QueryPageRequest | dict[str, Any] | None = None,
) -> QueryPage[TransferHistorySnapshot]:
    """同步分页读取整理历史 DTO。"""
    return _service().list_transfer_history(filters, page)


async def async_list_transfer_history(
    filters: TransferHistoryFilter | dict[str, Any] | None = None,
    page: QueryPageRequest | dict[str, Any] | None = None,
) -> QueryPage[TransferHistorySnapshot]:
    """异步分页读取整理历史 DTO。"""
    return await _service().async_list_transfer_history(filters, page)


def get_transfer_history(history_id: int) -> TransferHistorySnapshot | None:
    """同步按 ID 读取整理历史 DTO。"""
    return _service().get_transfer_history(history_id)


async def async_get_transfer_history(
    history_id: int,
) -> TransferHistorySnapshot | None:
    """异步按 ID 读取整理历史 DTO。"""
    return await _service().async_get_transfer_history(history_id)


__all__ = [
    "DEFAULT_QUERY_PAGE_SIZE",
    "MAX_QUERY_PAGE_SIZE",
    "DownloadHistoryFilter",
    "DownloadHistorySnapshot",
    "MediaIdentityQuery",
    "QueryPage",
    "QueryPageRequest",
    "QuerySort",
    "QuerySortDirection",
    "QuerySortField",
    "SubscriptionFilter",
    "SubscriptionHistoryFilter",
    "SubscriptionHistorySnapshot",
    "SubscriptionSnapshot",
    "TransferHistoryFilter",
    "TransferHistorySnapshot",
    "async_get_download_history",
    "async_get_subscription",
    "async_get_subscription_history",
    "async_get_transfer_history",
    "async_list_download_history",
    "async_list_subscription_history",
    "async_list_subscriptions",
    "async_list_transfer_history",
    "get_download_history",
    "get_subscription",
    "get_subscription_history",
    "get_transfer_history",
    "list_download_history",
    "list_subscription_history",
    "list_subscriptions",
    "list_transfer_history",
]
