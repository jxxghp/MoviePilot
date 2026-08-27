"""统一插件只读查询 SDK 的真实 SQLite 合同测试。

这些用例从真实 SQLAlchemy 表读取，再经查询服务和 ``app.sdk.queries`` 返回，
因此同时约束筛选、分页、DTO 投影以及异步数据库执行边界。所有数据都由共享的
隔离 SQLite harness 写入；测试不调用外部服务。
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from app.db.models.downloadhistory import DownloadHistory as DownloadHistoryModel
from app.db.models.subscribe import Subscribe as SubscribeModel
from app.db.models.subscribehistory import SubscribeHistory as SubscribeHistoryModel
from app.db.models.transferhistory import TransferHistory as TransferHistoryModel
from app.schemas.query import (
    DownloadHistoryFilter,
    DownloadHistorySnapshot,
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
from app.schemas.types import MediaSource, MediaType

TMDB = MediaSource.TMDB.value


class _RecordingExecutor:
    """记录服务是否把异步查询提交到独立执行线程。"""

    def __init__(self) -> None:
        self.calls = 0
        self.worker_thread_ids: list[int] = []

    async def run(self, operation: Callable[[], Any]) -> Any:
        """在线程中执行同步查询，模拟宿主数据库 worker 的调用合同。"""
        self.calls += 1

        def invoke() -> Any:
            self.worker_thread_ids.append(threading.get_ident())
            return operation()

        return await asyncio.to_thread(invoke)


@pytest.fixture
def query_sdk(db, monkeypatch):
    """装配真实数据查询适配器，并把 SDK 绑定到本用例的 SQLite 服务。"""
    from app.application import data_query as data_query_module
    from app.application.data_query import DataQueryService
    from app.db.adapters.data_query import SqlAlchemyDataQueryAdapter
    from app.db.session import SessionFactory
    from app.sdk import queries as sdk

    adapter = SqlAlchemyDataQueryAdapter(SessionFactory)
    executor = _RecordingExecutor()
    service = DataQueryService(
        subscriptions=adapter,
        histories=adapter,
        async_executor=executor,
    )
    monkeypatch.setattr(
        data_query_module,
        "_configured_data_query_service",
        service,
    )
    return sdk, executor


def _subscribe(
    name: str,
    *,
    media_id: str,
    state: str = "N",
    username: str = "alice",
    mtype: str = MediaType.TV.value,
    season: int | None = 1,
    episode_group: str | None = None,
    date: str = "2026-08-27 10:00:00",
    music_type: str | None = None,
    manual_total_episode: int | None = 0,
) -> SubscribeModel:
    """构造隔离用例使用的订阅行。"""
    return SubscribeModel(
        name=name,
        type=mtype,
        state=state,
        media_source=TMDB,
        media_id=media_id,
        season=season,
        episode_group=episode_group,
        username=username,
        date=date,
        music_type=music_type,
        manual_total_episode=manual_total_episode,
    )


def _subscribe_history(
    name: str,
    *,
    media_id: str,
    username: str = "alice",
    mtype: str = MediaType.TV.value,
    season: int | None = 1,
    episode_group: str | None = None,
    date: str = "2026-08-27 10:00:00",
    music_type: str | None = None,
) -> SubscribeHistoryModel:
    """构造隔离用例使用的订阅完成历史行。"""
    return SubscribeHistoryModel(
        name=name,
        type=mtype,
        media_source=TMDB,
        media_id=media_id,
        season=season,
        episode_group=episode_group,
        username=username,
        date=date,
        music_type=music_type,
    )


def _download_history(
    title: str,
    *,
    media_id: str,
    path: str,
    year: str = "2026",
    seasons: str | None = "S01",
    episodes: str | None = "E01",
    username: str = "alice",
    download_hash: str | None = "hash-1",
    episode_group: str | None = None,
    date: str = "2026-08-27 10:00:00",
    mtype: str = MediaType.TV.value,
    music_type: str | None = None,
) -> DownloadHistoryModel:
    """构造隔离用例使用的下载历史行。"""
    return DownloadHistoryModel(
        path=path,
        type=mtype,
        title=title,
        year=year,
        media_source=TMDB,
        media_id=media_id,
        music_type=music_type,
        seasons=seasons,
        episodes=episodes,
        username=username,
        download_hash=download_hash,
        episode_group=episode_group,
        date=date,
    )


def _transfer_history(
    title: str,
    *,
    media_id: str | None,
    src: str,
    dest: str,
    year: str = "2026",
    seasons: str | None = "S01",
    episodes: str | None = "E01",
    download_hash: str | None = "hash-1",
    episode_group: str | None = None,
    status: bool = True,
    date: str = "2026-08-27 10:00:00",
    mtype: str = MediaType.TV.value,
) -> TransferHistoryModel:
    """构造隔离用例使用的整理历史行。"""
    return TransferHistoryModel(
        src=src,
        src_storage="local",
        dest=dest,
        type=mtype,
        title=title,
        year=year,
        media_source=TMDB if media_id is not None else None,
        media_id=media_id,
        seasons=seasons,
        episodes=episodes,
        download_hash=download_hash,
        episode_group=episode_group,
        status=status,
        date=date,
    )


def _assert_projected_page(page: QueryPage, dto_type: type[Any], model_type: type[Any], row_id: int) -> None:
    """断言分页结果是稳定 DTO，而不是 ORM 行或延迟加载对象。"""
    assert isinstance(page, QueryPage)
    assert page.total == 1
    assert len(page.items) == 1
    item = page.items[0]
    assert isinstance(item, dto_type)
    assert not isinstance(item, model_type)
    assert item.id == row_id
    assert item.media_source == MediaSource.TMDB


def test_sdk_projects_subscription_and_three_history_domains_to_dtos(db, query_sdk):
    """订阅、订阅完成历史、下载历史、整理历史均只返回 Pydantic 投影。"""
    sdk, _executor = query_sdk
    subscribe = db.add(
        _subscribe(
            "订阅 DTO",
            media_id="dto-sub",
            manual_total_episode=1,
        )
    )
    subscribe_history = db.add(_subscribe_history("订阅历史 DTO", media_id="dto-sub-history"))
    download_history = db.add(_download_history("下载历史 DTO", media_id="dto-download", path="/dto/download"))
    transfer_history = db.add(
        _transfer_history(
            "整理历史 DTO",
            media_id="dto-transfer",
            src="/dto/src",
            dest="/dto/dest",
        )
    )

    _assert_projected_page(
        subscription_page := sdk.list_subscriptions(SubscriptionFilter(media_source=TMDB, media_id="dto-sub")),
        SubscriptionSnapshot,
        SubscribeModel,
        subscribe.id,
    )
    assert subscription_page.items[0].manual_total_episode == 1
    _assert_projected_page(
        sdk.list_subscription_history(
            SubscriptionHistoryFilter(
                media_source=TMDB,
                media_id="dto-sub-history",
            )
        ),
        SubscriptionHistorySnapshot,
        SubscribeHistoryModel,
        subscribe_history.id,
    )
    _assert_projected_page(
        sdk.list_download_history(DownloadHistoryFilter(media_source=TMDB, media_id="dto-download")),
        DownloadHistorySnapshot,
        DownloadHistoryModel,
        download_history.id,
    )
    _assert_projected_page(
        sdk.list_transfer_history(TransferHistoryFilter(media_source=TMDB, media_id="dto-transfer")),
        TransferHistorySnapshot,
        TransferHistoryModel,
        transfer_history.id,
    )


def test_sdk_applies_combined_filters_in_each_query_domain(db, query_sdk):
    """多个筛选条件必须按 AND 组合，不能只应用媒体身份或单个主条件。"""
    sdk, _executor = query_sdk

    db.add(
        _subscribe(
            "订阅命中",
            media_id="combo-sub",
            state="N",
            username="alice",
            season=2,
            episode_group="eg-a",
        ),
        _subscribe(
            "状态不符",
            media_id="combo-sub",
            state="R",
            username="alice",
            season=2,
            episode_group="eg-a",
        ),
        _subscribe(
            "用户不符",
            media_id="combo-sub",
            state="N",
            username="bob",
            season=2,
            episode_group="eg-a",
        ),
        _subscribe(
            "季不符",
            media_id="combo-sub",
            state="N",
            username="alice",
            season=1,
            episode_group="eg-a",
        ),
        _subscribe(
            "剧集组不符",
            media_id="combo-sub",
            state="N",
            username="alice",
            season=2,
            episode_group="eg-b",
        ),
        _subscribe(
            "类型不符",
            media_id="combo-sub",
            state="N",
            username="alice",
            mtype=MediaType.MOVIE.value,
            season=2,
            episode_group="eg-a",
        ),
    )
    subscribe_page = sdk.list_subscriptions(
        SubscriptionFilter(
            media_source=TMDB,
            media_id="combo-sub",
            states=("N",),
            usernames=("alice",),
            media_types=(MediaType.TV,),
            season=2,
            episode_group="eg-a",
        )
    )
    assert [item.name for item in subscribe_page.items] == ["订阅命中"]

    db.add(
        _subscribe_history(
            "历史命中",
            media_id="combo-sub-history",
            username="alice",
            season=2,
            episode_group="eg-a",
        ),
        _subscribe_history(
            "历史用户不符",
            media_id="combo-sub-history",
            username="bob",
            season=2,
            episode_group="eg-a",
        ),
        _subscribe_history(
            "历史季不符",
            media_id="combo-sub-history",
            username="alice",
            season=1,
            episode_group="eg-a",
        ),
        _subscribe_history(
            "历史类型不符",
            media_id="combo-sub-history",
            username="alice",
            mtype=MediaType.MOVIE.value,
            season=2,
            episode_group="eg-a",
        ),
    )
    history_page = sdk.list_subscription_history(
        SubscriptionHistoryFilter(
            media_source=TMDB,
            media_id="combo-sub-history",
            usernames=("alice",),
            media_types=(MediaType.TV,),
            season=2,
            episode_group="eg-a",
        )
    )
    assert [item.name for item in history_page.items] == ["历史命中"]

    db.add(
        _download_history(
            "Combo Film",
            media_id="combo-download",
            path="/combo/match.mkv",
            year="2026",
            seasons="S02",
            episodes="E03",
            username="alice",
            download_hash="combo-hash",
            episode_group="eg-a",
        ),
        _download_history(
            "Combo Film",
            media_id="combo-download",
            path="/combo/wrong-user.mkv",
            year="2026",
            seasons="S02",
            episodes="E03",
            username="bob",
            download_hash="combo-hash",
            episode_group="eg-a",
        ),
        _download_history(
            "Combo Film",
            media_id="combo-download",
            path="/combo/wrong-episode.mkv",
            year="2026",
            seasons="S02",
            episodes="E04",
            username="alice",
            download_hash="combo-hash",
            episode_group="eg-a",
        ),
    )
    download_page = sdk.list_download_history(
        DownloadHistoryFilter(
            media_source=TMDB,
            media_id="combo-download",
            media_types=(MediaType.TV,),
            title="Combo Film",
            text="match",
            year="2026",
            seasons="S02",
            episodes="E03",
            path="/combo/match.mkv",
            download_hash="combo-hash",
            username="alice",
            episode_group="eg-a",
        )
    )
    assert [item.path for item in download_page.items] == ["/combo/match.mkv"]

    db.add(
        _transfer_history(
            "Transfer Combo",
            media_id="combo-transfer",
            src="/combo/src-match.mkv",
            dest="/combo/dest-match.mkv",
            year="2026",
            seasons="S02",
            episodes="E03",
            download_hash="transfer-hash",
            episode_group="eg-a",
        ),
        _transfer_history(
            "Transfer Combo",
            media_id="combo-transfer",
            src="/combo/src-failed.mkv",
            dest="/combo/dest-failed.mkv",
            year="2026",
            seasons="S02",
            episodes="E03",
            download_hash="transfer-hash",
            episode_group="eg-a",
            status=False,
        ),
        _transfer_history(
            "Transfer No Identity",
            media_id=None,
            src="/combo/src-no-id.mkv",
            dest="/combo/dest-no-id.mkv",
            year="2026",
            seasons="S02",
            episodes="E03",
            download_hash="transfer-hash",
            episode_group="eg-a",
        ),
    )
    transfer_page = sdk.list_transfer_history(
        TransferHistoryFilter(
            media_types=(MediaType.TV,),
            media_sources=(MediaSource.TMDB,),
            require_media_identity=True,
            title="Transfer Combo",
            text="dest-match",
            year="2026",
            seasons="S02",
            episodes="E03",
            src="/combo/src-match.mkv",
            dest="/combo/dest-match.mkv",
            status=True,
            download_hash="transfer-hash",
            episode_group="eg-a",
        )
    )
    assert [item.src for item in transfer_page.items] == ["/combo/src-match.mkv"]


def test_sdk_pagination_reports_total_and_stable_date_id_order(db, query_sdk):
    """同日期记录按 ID 打破平局，跨页总数和结果顺序保持稳定。"""
    sdk, _executor = query_sdk
    rows = db.add(
        _download_history(
            "排序一",
            media_id="sort-download",
            path="/sort/one",
            date="2026-08-27 10:00:00",
            download_hash="sort-1",
        ),
        _download_history(
            "排序二",
            media_id="sort-download",
            path="/sort/two",
            date="2026-08-27 10:00:00",
            download_hash="sort-2",
        ),
        _download_history(
            "排序三",
            media_id="sort-download",
            path="/sort/three",
            date="2026-08-26 10:00:00",
            download_hash="sort-3",
        ),
    )
    rows = list(rows)
    expected_desc = sorted(rows, key=lambda row: (row.date, row.id), reverse=True)
    filters = DownloadHistoryFilter(media_source=TMDB, media_id="sort-download")

    page_one = sdk.list_download_history(
        filters,
        QueryPageRequest(page=1, count=2),
    )
    page_two = sdk.list_download_history(
        filters,
        QueryPageRequest(page=2, count=2),
    )

    assert page_one.total == 3
    assert page_one.page == 1
    assert page_one.count == 2
    assert page_one.has_next is True
    assert [item.id for item in page_one.items] == [row.id for row in expected_desc[:2]]
    assert page_two.total == 3
    assert page_two.has_next is False
    assert [item.id for item in page_two.items] == [expected_desc[2].id]

    asc_page = sdk.list_download_history(
        filters,
        QueryPageRequest(
            page=1,
            count=3,
            sort=QuerySort(
                field=QuerySortField.DATE,
                direction=QuerySortDirection.ASC,
            ),
        ),
    )
    expected_asc = sorted(rows, key=lambda row: (row.date, row.id))
    assert [item.id for item in asc_page.items] == [row.id for row in expected_asc]


def test_structured_text_fields_are_exact_and_text_search_is_explicit(db, query_sdk):
    """结构化字段保持精确匹配，只有 text 承担转义后的模糊搜索。"""
    sdk, _executor = query_sdk
    db.add(
        _download_history(
            "Exact Film Extended",
            media_id="exact-download",
            path="/exact/extended.mkv",
        ),
        _download_history(
            "Exact Film",
            media_id="exact-download",
            path="/exact/base.mkv",
        ),
        _transfer_history(
            "Exact Transfer Extended",
            media_id="exact-transfer",
            src="/exact/extended-src.mkv",
            dest="/exact/extended-dest.mkv",
        ),
        _transfer_history(
            "Exact Transfer",
            media_id="exact-transfer",
            src="/exact/base-src.mkv",
            dest="/exact/base-dest.mkv",
        ),
    )

    exact_downloads = sdk.list_download_history(
        DownloadHistoryFilter(
            media_source=TMDB,
            media_id="exact-download",
            title="Exact Film",
        )
    )
    assert [item.path for item in exact_downloads.items] == ["/exact/base.mkv"]
    fuzzy_downloads = sdk.list_download_history(
        DownloadHistoryFilter(
            media_source=TMDB,
            media_id="exact-download",
            text="extended",
        )
    )
    assert [item.path for item in fuzzy_downloads.items] == ["/exact/extended.mkv"]

    exact_transfers = sdk.list_transfer_history(
        TransferHistoryFilter(
            media_source=TMDB,
            media_id="exact-transfer",
            title="Exact Transfer",
        )
    )
    assert [item.src for item in exact_transfers.items] == ["/exact/base-src.mkv"]
    fuzzy_transfers = sdk.list_transfer_history(
        TransferHistoryFilter(
            media_source=TMDB,
            media_id="exact-transfer",
            text="extended-dest",
        )
    )
    assert [item.src for item in fuzzy_transfers.items] == ["/exact/extended-src.mkv"]


def test_snapshots_normalize_legacy_identity_and_transfer_status():
    """旧半对身份和 NULL 整理状态不得使分页投影失败或产生假成功。"""
    dirty_transfer = SimpleNamespace(
        id=1,
        media_source=TMDB,
        media_id=" ",
        status=None,
    )

    snapshot = TransferHistorySnapshot.model_validate(dirty_transfer)

    assert snapshot.media_source is None
    assert snapshot.media_id is None
    assert snapshot.status is False


def test_query_snapshots_are_owned_by_the_sdk_contract_module():
    """公开查询返回值由独立快照定义，不复用宿主写入或 API 响应模型。"""
    snapshots = (
        SubscriptionSnapshot,
        SubscriptionHistorySnapshot,
        DownloadHistorySnapshot,
        TransferHistorySnapshot,
    )

    assert all(snapshot.__module__ == "app.schemas.query" for snapshot in snapshots)


@pytest.mark.parametrize(
    "payload",
    [
        {"media_source": TMDB},
        {"media_id": "half-id"},
        {"media_source": TMDB, "media_id": ""},
        {"media_source": TMDB, "media_id": "   "},
        {"media_source": TMDB, "media_id": "0"},
        {"media_source": "not valid!", "media_id": "id"},
    ],
)
def test_sdk_rejects_invalid_or_half_media_identity_fail_closed(query_sdk, payload):
    """媒体身份不完整、空白、零值或未知来源不得退化成全表查询。"""
    sdk, _executor = query_sdk

    with pytest.raises(ValueError):
        sdk.list_download_history(payload)


def test_sdk_get_returns_none_for_missing_records(query_sdk):
    """四个查询领域的按 ID 读取在未命中时统一返回 None。"""
    sdk, _executor = query_sdk
    missing_id = 2_147_483_647

    assert sdk.get_subscription(missing_id) is None
    assert sdk.get_subscription_history(missing_id) is None
    assert sdk.get_download_history(missing_id) is None
    assert sdk.get_transfer_history(missing_id) is None


def test_sdk_sync_async_semantics_match_and_async_uses_executor(db, query_sdk):
    """四个查询门面的异步结果与同步一致，并交给 executor 线程。"""
    sdk, executor = query_sdk
    subscribe = db.add(_subscribe("订阅同步异步", media_id="async-subscription"))
    subscribe_history = db.add(
        _subscribe_history(
            "订阅历史同步异步",
            media_id="async-subscription-history",
        )
    )
    download = db.add(
        _download_history(
            "同步异步一致",
            media_id="async-download",
            path="/async/download",
        )
    )
    transfer = db.add(
        _transfer_history(
            "整理历史同步异步",
            media_id="async-transfer",
            src="/async/src",
            dest="/async/dest",
        )
    )
    request = QueryPageRequest(page=1, count=10)
    caller_thread_id = threading.get_ident()

    cases = (
        (
            sdk.list_subscriptions,
            sdk.async_list_subscriptions,
            SubscriptionFilter(
                media_source=TMDB,
                media_id="async-subscription",
            ),
            subscribe.id,
        ),
        (
            sdk.list_subscription_history,
            sdk.async_list_subscription_history,
            SubscriptionHistoryFilter(
                media_source=TMDB,
                media_id="async-subscription-history",
            ),
            subscribe_history.id,
        ),
        (
            sdk.list_download_history,
            sdk.async_list_download_history,
            DownloadHistoryFilter(
                media_source=TMDB,
                media_id="async-download",
            ),
            download.id,
        ),
        (
            sdk.list_transfer_history,
            sdk.async_list_transfer_history,
            TransferHistoryFilter(
                media_source=TMDB,
                media_id="async-transfer",
            ),
            transfer.id,
        ),
    )
    for sync_call, async_call, filters, expected_id in cases:
        sync_page = sync_call(filters, request)
        calls_before = executor.calls
        async_page = asyncio.run(async_call(filters, request))

        assert async_page == sync_page
        assert [item.id for item in async_page.items] == [expected_id]
        assert executor.calls == calls_before + 1

    assert executor.worker_thread_ids
    assert all(thread_id != caller_thread_id for thread_id in executor.worker_thread_ids)


def test_sdk_public_exports_do_not_leak_persistence_implementation(query_sdk):
    """SDK 的机器可读公开合同只允许 DTO、筛选模型和查询函数。"""
    sdk, _executor = query_sdk
    forbidden_tokens = ("session", "oper", "provider", "configure")
    forbidden_modules = (
        "app.db.models",
        "app.db.oper",
        "app.db.session",
        "app.application",
    )

    assert sdk.__all__
    for name in sdk.__all__:
        lowered = name.casefold()
        assert not any(token in lowered for token in forbidden_tokens), name
        exported = getattr(sdk, name)
        module_name = getattr(exported, "__module__", "")
        assert not module_name.startswith(forbidden_modules), (
            name,
            module_name,
        )
        if inspect.isfunction(exported):
            assert module_name == "app.sdk.queries"

    assert not hasattr(sdk, "TransactionalDataQueryRepository")
    assert not hasattr(sdk, "DataQueryService")
    assert not hasattr(sdk, "get_configured_data_query_service")


def test_query_tests_use_local_sqlite_backend(query_sdk):
    """查询 focused tests 固定在隔离 SQLite 上，不依赖外部数据库或网络。"""
    from app.db.engine import get_engine

    engine = get_engine()
    assert engine.url.get_backend_name() == "sqlite"
