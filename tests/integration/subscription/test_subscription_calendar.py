"""
订阅日历的日期优先窗口、精简投影、并发和用户权限回归。
"""

import asyncio
from datetime import date
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_active_user_async
from app.api.dependencies.subscription import get_subscription_query_service
from app.api.endpoints import subscribe as endpoint
from app.chain.subscribe.calendar import SubscribeCalendarChain
from app.runtime.cache import AsyncCacheControls
from app.schemas.calendar import CalendarDate, SubscribeCalendarPage
from app.schemas.subscribe import Subscribe
from app.schemas.tmdb import TmdbEpisode
from app.schemas.types import MediaSource, MediaType


@pytest.mark.asyncio
async def test_calendar_schedule_projects_and_reuses_episode_dates() -> None:
    """
    同日分集合并且丢弃大简介，翻月复用精简缓存，不再重复调用分集接口。
    """
    loader = SubscribeCalendarChain._schedule
    controls = cast(AsyncCacheControls, loader)
    await controls.cache_clear()
    tmdb = Mock()
    tmdb.async_tmdb_episodes = AsyncMock(
        return_value=[
            TmdbEpisode(air_date="2026-10-25", episode_number=1, name="第一集", runtime=24, overview="长简介" * 1000),
            TmdbEpisode(air_date="2026-10-25", episode_number=2, name="第二集"),
            TmdbEpisode(air_date=None, episode_number=3),
            TmdbEpisode(air_date="2026-11-01", episode_number=4),
        ]
    )
    try:
        with patch("app.chain.subscribe.calendar.TmdbChain", return_value=tmdb):
            result = await loader(MediaSource.TMDB, "874", MediaType.TV, 2, "group-a")
            repeated = await loader(MediaSource.TMDB, "874", MediaType.TV, 2, "group-a")
        tmdb.async_tmdb_episodes.assert_awaited_once_with(tmdbid=874, season=2, episode_group="group-a")
        assert repeated == result
        assert result[0].model_dump() == {
            "date": "2026-10-25",
            "runtime": 24,
            "episodes": [{"episode_number": 1, "name": "第一集"}, {"episode_number": 2, "name": "第二集"}],
        }
        assert [item.date for item in result] == ["2026-10-25", "2026-11-01"]
        assert "overview" not in result[0].model_dump_json()
    finally:
        await controls.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("mtype", [MediaType.MOVIE, MediaType.TV])
async def test_calendar_recognizes_movie_dates_and_converts_external_tv_identity(mtype) -> None:
    """
    电影使用上映日期，非 TMDB 剧集先转换媒体身份后查询订阅季，均不透传完整媒体详情。
    """
    loader = SubscribeCalendarChain._schedule
    controls = cast(AsyncCacheControls, loader)
    await controls.cache_clear()
    media = Mock()
    media.async_recognize_media = AsyncMock(
        return_value=SimpleNamespace(tmdb_id=874, release_date="2026-10-27", runtime=120)
    )
    tmdb = Mock()
    tmdb.async_tmdb_episodes = AsyncMock(
        return_value=[TmdbEpisode(air_date="2026-10-27", episode_number=1)]
    )
    try:
        with (
            patch("app.chain.subscribe.calendar.MediaChain", return_value=media),
            patch("app.chain.subscribe.calendar.TmdbChain", return_value=tmdb),
        ):
            result = await loader(MediaSource.Douban, "external-874", mtype, 2, None)
        media.async_recognize_media.assert_awaited_once_with(
            media_source=MediaSource.Douban, media_id="external-874", mtype=mtype, episode_group=None
        )
        assert [item.date for item in result] == ["2026-10-27"]
        if mtype == MediaType.MOVIE:
            assert result[0].runtime == 120
            assert result[0].episodes == []
            tmdb.async_tmdb_episodes.assert_not_awaited()
        else:
            tmdb.async_tmdb_episodes.assert_awaited_once_with(tmdbid=874, season=2, episode_group=None)
            assert result[0].episodes[0].episode_number == 1
    finally:
        await controls.cache_clear()


@pytest.mark.asyncio
async def test_calendar_filters_real_dates_before_sorting_and_pagination(monkeypatch) -> None:
    """
    订阅创建顺序与播出日期相反时仍先筛选窗口，最多四路查询且返回日期有序的小页。
    """
    active = peak = 0

    async def schedule(_source, media_id, _mtype, _season, _group):
        """
        用乱序媒体播出日期与窗口外事件模拟已缓存的媒体源。
        """
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        await asyncio.sleep(0.001)
        active -= 1
        return [
            CalendarDate(date="2020-01-01"),
            CalendarDate(date=f"2026-10-{11 - int(media_id):02d}"),
            CalendarDate(date="2026-11-01"),
        ]

    monkeypatch.setattr(SubscribeCalendarChain, "_schedule", staticmethod(schedule))
    subscribes = [
        Subscribe(
            id=index,
            name=f"订阅 {index}",
            type="电视剧",
            media_source=MediaSource.TMDB,
            media_id=str(index),
            season=1,
            date=f"2026-10-{index:02d}",
            description="不需要的订阅描述" * 1000,
        )
        for index in range(1, 11)
    ]
    result = await SubscribeCalendarChain().window(subscribes, date(2026, 10, 1), date(2026, 11, 1), 2, 3)
    assert peak == 4
    assert result.total == 10
    assert [item.date for item in result.items] == ["2026-10-04", "2026-10-05", "2026-10-06"]
    assert [item.subscribe.id for item in result.items] == [7, 6, 5]
    assert "description" not in result.items[0].subscribe.model_dump()


@pytest.mark.asyncio
async def test_calendar_stops_dispatching_when_old_window_disconnects(monkeypatch) -> None:
    """
    翻月取消的请求不再为剩余订阅派发媒体详情查询。
    """
    schedule = AsyncMock()
    monkeypatch.setattr(SubscribeCalendarChain, "_schedule", schedule)
    result = await SubscribeCalendarChain().window(
        [Subscribe(id=1, type="电影", media_source=MediaSource.TMDB, media_id="1")],
        date(2026, 10, 1),
        date(2026, 11, 1),
        1,
        50,
        AsyncMock(return_value=True),
    )
    assert result.total == 0
    schedule.assert_not_awaited()


@pytest.mark.parametrize("admin", [False, True])
def test_calendar_http_window_keeps_owner_scope_and_validates_bounds(admin) -> None:
    """
    日期查询复用普通用户的 owner 限制，HTTP 边界拒绝过大日期和分页窗口。
    """
    app = FastAPI()
    app.include_router(endpoint.router, prefix="/subscribe")
    query = Mock()
    query.list_public = AsyncMock(return_value=[])
    app.dependency_overrides[get_subscription_query_service] = lambda: query
    app.dependency_overrides[get_current_active_user_async] = lambda: SimpleNamespace(name="alice", is_superuser=admin)
    chain = Mock()
    chain.window = AsyncMock(return_value=SubscribeCalendarPage(total=0))
    with patch.object(endpoint, "SubscribeCalendarChain", return_value=chain), TestClient(app) as client:
        for suffix in ["&page=0", "&count=201", "&count=0"]:
            assert client.get(f"/subscribe/calendar?start=2026-10-01&end=2026-11-01{suffix}").status_code == 422
        assert client.get("/subscribe/calendar?start=2026-10-01&end=2027-01-03").status_code == 422
        assert client.get("/subscribe/calendar?start=2026-10-01&end=2026-10-01").status_code == 422
        assert client.get("/subscribe/calendar?start=invalid&end=2026-11-01").status_code == 422
        response = client.get("/subscribe/calendar?start=2026-10-01&end=2026-11-01&page=2&count=10")
    assert response.status_code == 200
    assert response.json()["data"] == {"items": [], "total": 0}
    query.list_public.assert_awaited_once_with(None if admin else "alice")
    assert chain.window.await_args.args[:5] == ([], date(2026, 10, 1), date(2026, 11, 1), 2, 10)
