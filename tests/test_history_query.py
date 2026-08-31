"""历史查询应用服务的分页、筛选和 DTO 边界测试。"""

from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest
from starlette.responses import Response

from app.api.endpoints.history import download_history
from app.application.history import HistoryQueryService
from app.schemas.history import DownloadHistory, TransferHistory


def _make_service(
    *,
    downloads: AsyncMock | None = None,
    transfers: AsyncMock | None = None,
) -> tuple[HistoryQueryService, AsyncMock, AsyncMock]:
    """构造使用可观察异步仓储的历史查询服务。"""
    download_repository = downloads or AsyncMock()
    transfer_repository = transfers or AsyncMock()
    return (
        HistoryQueryService(
            download_repository=download_repository,
            transfer_repository=transfer_repository,
        ),
        download_repository,
        transfer_repository,
    )


@pytest.mark.asyncio
async def test_list_download_returns_schema_dtos() -> None:
    """下载历史查询不得把仓储对象原样泄漏给 API。"""
    service, download_repository, _ = _make_service()
    raw_record = SimpleNamespace(id=7, title="Movie")
    download_repository.async_list_by_page.return_value = [raw_record]

    records = await service.list_download(page=2, count=10)

    assert records == [DownloadHistory(id=7, title="Movie")]
    assert records[0] is not raw_record
    download_repository.async_list_by_page.assert_awaited_once_with(2, 10)


@pytest.mark.asyncio
async def test_download_history_reports_exact_total_without_changing_list() -> None:
    """下载历史 API 应通过响应头报告精确总数并保持原列表返回。"""
    service, download_repository, _ = _make_service()
    download_repository.async_list_by_page.return_value = [
        SimpleNamespace(id=7, title="Movie")
    ]
    download_repository.async_count.return_value = 12
    response = Response()

    records = await download_history(
        page=2,
        count=10,
        query=service,
        _=SimpleNamespace(),
        response=response,
    )

    assert records == [DownloadHistory(id=7, title="Movie")]
    assert response.headers["X-Total-Count"] == "12"
    download_repository.async_count.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_list_transfer_uses_explicit_status_without_reinterpreting_title() -> None:
    """状态筛选必须使用显式参数，中文标题仍保持标题查询语义。"""
    service, _, transfer_repository = _make_service()
    transfer_repository.async_list_by_title.return_value = [
        SimpleNamespace(id=3, status=False)
    ]
    transfer_repository.async_count_by_title.return_value = 1

    page = await service.list_transfer(title="失败", page=3, count=5, status=False)

    assert page.list == [TransferHistory(id=3, status=False)]
    assert page.total == 1
    transfer_repository.async_count_by_title.assert_awaited_once()
    transfer_repository.async_list_by_title.assert_awaited_once_with(
        ANY,
        page=3,
        count=5,
        status=False,
        wildcard=False,
    )


@pytest.mark.asyncio
async def test_list_transfer_preserves_glob_escaping() -> None:
    """glob 查询应转义 SQL 通配符并显式启用 wildcard 模式。"""
    service, _, transfer_repository = _make_service()
    transfer_repository.async_list_by_title.return_value = []
    transfer_repository.async_count_by_title.return_value = 0

    page = await service.list_transfer(
        title=r"show_100%*.mkv",
        page=1,
        count=30,
        status=True,
    )

    assert page.total == 0
    pattern = r"show\_100\%%.mkv"
    transfer_repository.async_count_by_title.assert_awaited_once_with(
        pattern,
        status=True,
        wildcard=True,
    )
    transfer_repository.async_list_by_title.assert_awaited_once_with(
        pattern,
        page=1,
        count=30,
        status=True,
        wildcard=True,
    )


@pytest.mark.asyncio
async def test_get_transfers_preserves_order_and_reports_missing_ids() -> None:
    """批量 AI 重做准备应保持去重后的输入顺序并报告缺失记录。"""
    service, _, transfer_repository = _make_service()
    transfer_repository.async_get.side_effect = [
        SimpleNamespace(id=11),
        None,
        SimpleNamespace(id=13),
    ]

    records, missing_ids = await service.get_transfers([11, 12, 13])

    assert [record.id for record in records] == [11, 13]
    assert missing_ids == [12]
    assert transfer_repository.async_get.await_args_list == [
        ((11,), {}),
        ((12,), {}),
        ((13,), {}),
    ]
