"""插件可使用的订阅与历史只读查询门面。"""

from __future__ import annotations

from typing import Any

from app.schemas.query import (
    DEFAULT_QUERY_PAGE_SIZE,
    MAX_QUERY_PAGE_SIZE,
    DownloadHistory,
    DownloadHistoryFilter,
    MediaIdentityQuery,
    QueryPage,
    QueryPageRequest,
    QuerySort,
    QuerySortDirection,
    QuerySortField,
    Subscribe,
    SubscribeHistory,
    SubscriptionFilter,
    SubscriptionHistoryFilter,
    TransferHistory,
    TransferHistoryFilter,
)


def _service() -> Any:
    """获取启动阶段登记的查询服务，避免把应用服务暴露为 SDK 合同。"""
    from app.application.data_query import (
        get_configured_data_query_service,
    )

    return get_configured_data_query_service()


def list_subscriptions(
    filters: SubscriptionFilter | dict[str, Any] | None = None,
    page: QueryPageRequest | dict[str, Any] | None = None,
) -> QueryPage[Subscribe]:
    """同步分页读取订阅 DTO。"""
    return _service().list_subscriptions(filters, page)


async def async_list_subscriptions(
    filters: SubscriptionFilter | dict[str, Any] | None = None,
    page: QueryPageRequest | dict[str, Any] | None = None,
) -> QueryPage[Subscribe]:
    """异步分页读取订阅 DTO。"""
    return await _service().async_list_subscriptions(filters, page)


def get_subscription(subscription_id: int) -> Subscribe | None:
    """同步按 ID 读取订阅 DTO。"""
    return _service().get_subscription(subscription_id)


async def async_get_subscription(subscription_id: int) -> Subscribe | None:
    """异步按 ID 读取订阅 DTO。"""
    return await _service().async_get_subscription(subscription_id)


def list_subscription_history(
    filters: SubscriptionHistoryFilter | dict[str, Any] | None = None,
    page: QueryPageRequest | dict[str, Any] | None = None,
) -> QueryPage[SubscribeHistory]:
    """同步分页读取订阅完成历史 DTO。"""
    return _service().list_subscription_history(filters, page)


async def async_list_subscription_history(
    filters: SubscriptionHistoryFilter | dict[str, Any] | None = None,
    page: QueryPageRequest | dict[str, Any] | None = None,
) -> QueryPage[SubscribeHistory]:
    """异步分页读取订阅完成历史 DTO。"""
    return await _service().async_list_subscription_history(filters, page)


def get_subscription_history(history_id: int) -> SubscribeHistory | None:
    """同步按 ID 读取订阅完成历史 DTO。"""
    return _service().get_subscription_history(history_id)


async def async_get_subscription_history(history_id: int) -> SubscribeHistory | None:
    """异步按 ID 读取订阅完成历史 DTO。"""
    return await _service().async_get_subscription_history(history_id)


def list_download_history(
    filters: DownloadHistoryFilter | dict[str, Any] | None = None,
    page: QueryPageRequest | dict[str, Any] | None = None,
) -> QueryPage[DownloadHistory]:
    """同步分页读取下载历史 DTO。"""
    return _service().list_download_history(filters, page)


async def async_list_download_history(
    filters: DownloadHistoryFilter | dict[str, Any] | None = None,
    page: QueryPageRequest | dict[str, Any] | None = None,
) -> QueryPage[DownloadHistory]:
    """异步分页读取下载历史 DTO。"""
    return await _service().async_list_download_history(filters, page)


def get_download_history(history_id: int) -> DownloadHistory | None:
    """同步按 ID 读取下载历史 DTO。"""
    return _service().get_download_history(history_id)


async def async_get_download_history(history_id: int) -> DownloadHistory | None:
    """异步按 ID 读取下载历史 DTO。"""
    return await _service().async_get_download_history(history_id)


def list_transfer_history(
    filters: TransferHistoryFilter | dict[str, Any] | None = None,
    page: QueryPageRequest | dict[str, Any] | None = None,
) -> QueryPage[TransferHistory]:
    """同步分页读取整理历史 DTO。"""
    return _service().list_transfer_history(filters, page)


async def async_list_transfer_history(
    filters: TransferHistoryFilter | dict[str, Any] | None = None,
    page: QueryPageRequest | dict[str, Any] | None = None,
) -> QueryPage[TransferHistory]:
    """异步分页读取整理历史 DTO。"""
    return await _service().async_list_transfer_history(filters, page)


def get_transfer_history(history_id: int) -> TransferHistory | None:
    """同步按 ID 读取整理历史 DTO。"""
    return _service().get_transfer_history(history_id)


async def async_get_transfer_history(history_id: int) -> TransferHistory | None:
    """异步按 ID 读取整理历史 DTO。"""
    return await _service().async_get_transfer_history(history_id)


__all__ = [
    "DEFAULT_QUERY_PAGE_SIZE",
    "MAX_QUERY_PAGE_SIZE",
    "DownloadHistory",
    "DownloadHistoryFilter",
    "MediaIdentityQuery",
    "QueryPage",
    "QueryPageRequest",
    "QuerySort",
    "QuerySortDirection",
    "QuerySortField",
    "Subscribe",
    "SubscribeHistory",
    "SubscriptionFilter",
    "SubscriptionHistoryFilter",
    "TransferHistory",
    "TransferHistoryFilter",
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
