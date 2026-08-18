import asyncio
from typing import Optional
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.api.endpoints import media as media_endpoint
from app.api.endpoints import search as search_endpoint
from app.application.orchestration import subscribe as subscribe_module
from app.application.orchestration.subscribe import SubscribeChain
from app.domain.context import MediaInfo
from app.schemas.types import MediaSource, MediaType
from app.schemas.media import normalize_media_source
from app.schemas.workflow import MediaInfo as SchemaMediaInfo


def test_media_source_normalization_accepts_plugin_source() -> None:
    """来源规范化应同时支持内置别名和插件扩展标识。"""
    assert normalize_media_source(" Plugin_Source ") == MediaSource("plugin_source")
    assert normalize_media_source("tmdb") == MediaSource.TMDB
    assert normalize_media_source("plugin source:invalid") is None


def test_iqiyi_media_source_aliases_are_normalized() -> None:
    """爱奇艺探索来源的历史前缀和规范前缀应归一到同一媒体来源。"""
    assert normalize_media_source("iqiyi") is MediaSource.Iqiyi
    assert normalize_media_source("iqiyidiscover") is MediaSource.Iqiyi
    assert MediaSource("iqiyi") is MediaSource.Iqiyi


def test_resolve_anilist_search_params_preserves_identity() -> None:
    """精确搜索参数应保留枚举来源和原生 ID。"""
    params, message = asyncio.run(
        search_endpoint._resolve_media_search_params(
            MediaSource.AniList,
            "154587",
            media_type=MediaType.TV,
        )
    )

    assert message == ""
    assert params == {
        "media_source": MediaSource.AniList,
        "media_id": "154587",
    }


@pytest.mark.parametrize(
    ("media_source", "media_id"),
    [
        (None, "154587"),
        (MediaSource.TMDB, ""),
        (MediaSource.TMDB, "   "),
        (MediaSource.TMDB, "0"),
    ],
)
def test_resource_search_rejects_invalid_identity_before_chain(
        monkeypatch,
        media_source: Optional[MediaSource],
        media_id: str,
) -> None:
    """半身份、空白或零值 ID 应在进入搜索链及其本地缓存前被拒绝。"""
    search_chain = Mock(side_effect=AssertionError("无效身份不应创建搜索链"))
    monkeypatch.setattr(search_endpoint, "SearchChain", search_chain)

    response = asyncio.run(
        search_endpoint.search_by_id(
            media_id=media_id,
            media_source=media_source,
            mtype=MediaType.MOVIE.value,
            _=None,
        )
    )

    assert not response.success
    assert response.message == "媒体ID格式无效"
    search_chain.assert_not_called()


@pytest.mark.parametrize(
    ("media_source", "media_id"),
    [
        (MediaSource.MusicBrainz, "release-group-1"),
        (MediaSource.TheAudioDB, "2109619"),
        (MediaSource.DoubanMusic, "1401853"),
    ],
)
def test_resource_search_forwards_music_identity(
        monkeypatch,
        media_source: MediaSource,
        media_id: str,
) -> None:
    """音乐资源搜索应传递统一身份及音乐实体类型。"""
    captured = {}

    class FakeTorrent:
        """提供资源搜索响应需要的最小种子对象。"""

        @staticmethod
        def to_dict() -> dict:
            """返回可序列化的测试种子。"""
            return {"title": "Album result"}

    class FakeSearchChain:
        """记录精确资源搜索收到的参数。"""

        async def async_search_by_id(self, **kwargs):
            """保存搜索参数并返回单条测试结果。"""
            captured.update(kwargs)
            return [FakeTorrent()]

    monkeypatch.setattr(search_endpoint, "SearchChain", FakeSearchChain)

    response = asyncio.run(
        search_endpoint.search_by_id(
            media_id=media_id,
            media_source=media_source,
            mtype="music",
            music_type="album",
            _=None,
        )
    )

    assert response.success
    assert captured["media_source"] == media_source
    assert captured["media_id"] == media_id
    assert captured["mtype"] == MediaType.MUSIC
    assert captured["music_type"] == "album"


def test_subtitle_search_forwards_anilist_identity(monkeypatch) -> None:
    """字幕精确搜索应把 AniList 统一身份传给搜索链。"""
    captured = {}

    class FakeSearchChain:
        """记录字幕搜索收到的参数。"""

        async def async_search_subtitles_by_id(self, **kwargs):
            """保存搜索参数并返回空结果。"""
            captured.update(kwargs)
            return []

    monkeypatch.setattr(search_endpoint, "SearchChain", FakeSearchChain)

    source, message = asyncio.run(
        search_endpoint._build_subtitle_search_source(
            media_source=MediaSource.AniList,
            media_id="154587",
            mtype="tv",
        )
    )
    assert message == ""
    assert asyncio.run(source) == []
    assert captured["media_source"] == MediaSource.AniList
    assert captured["media_id"] == "154587"


def test_media_detail_forwards_unified_identity(monkeypatch) -> None:
    """媒体详情应只向识别链传递来源和原生 ID。"""
    media = MediaInfo(
        media_source=MediaSource.AniList,
        media_id="154587",
        type=MediaType.TV,
        title="Frieren",
    )
    media_chain = Mock()
    media_chain.async_recognize_media = AsyncMock(return_value=media)
    media_chain.async_obtain_images = AsyncMock(return_value=None)
    monkeypatch.setattr(media_endpoint, "MediaChain", Mock(return_value=media_chain))

    result = asyncio.run(
        media_endpoint.detail(
            media_id="154587",
            media_source=MediaSource.AniList,
            type_name=MediaType.TV.value,
            _=None,
        )
    )

    assert result["media_source"] == MediaSource.AniList.value
    assert result["media_id"] == "154587"
    media_chain.async_recognize_media.assert_awaited_once_with(
        media_source=MediaSource.AniList,
        media_id="154587",
        mtype=MediaType.TV,
    )


def test_media_detail_does_not_fallback_for_explicit_identity(monkeypatch) -> None:
    """明确身份识别失败时不能按标题切换到其他来源。"""
    media_chain = Mock()
    media_chain.async_recognize_media = AsyncMock(return_value=None)
    media_chain.async_recognize_by_meta = AsyncMock(
        side_effect=AssertionError("不应按标题切换识别源")
    )
    monkeypatch.setattr(media_endpoint, "MediaChain", Mock(return_value=media_chain))

    result = asyncio.run(
        media_endpoint.detail(
            media_id="999999",
            media_source=MediaSource.TMDB,
            type_name=MediaType.MOVIE.value,
            _=None,
        )
    )

    assert isinstance(result, SchemaMediaInfo)
    media_chain.async_recognize_by_meta.assert_not_awaited()


def test_media_detail_rejects_zero_identity_before_chain(monkeypatch) -> None:
    """媒体详情的零值原生 ID 不得进入识别链。"""
    media_chain = Mock(side_effect=AssertionError("零值身份不应创建媒体链"))
    monkeypatch.setattr(media_endpoint, "MediaChain", media_chain)

    result = asyncio.run(
        media_endpoint.detail(
            media_id="0",
            media_source=MediaSource.TMDB,
            type_name=MediaType.MOVIE.value,
            _=None,
        )
    )

    assert isinstance(result, SchemaMediaInfo)
    media_chain.assert_not_called()


def test_media_seasons_rejects_zero_identity_before_external_chain(monkeypatch) -> None:
    """季信息接口收到零值身份时不得调用 TMDB 或通用识别链。"""
    tmdb_chain = Mock(side_effect=AssertionError("零值身份不应创建 TMDB 链"))
    media_chain = Mock(side_effect=AssertionError("零值身份不应创建媒体链"))
    monkeypatch.setattr(media_endpoint, "TmdbChain", tmdb_chain)
    monkeypatch.setattr(media_endpoint, "MediaChain", media_chain)

    result = asyncio.run(
        media_endpoint.seasons(
            media_source=MediaSource.TMDB,
            media_id="0",
            title="不应回退标题",
            _=None,
        )
    )

    assert result == []
    tmdb_chain.assert_not_called()
    media_chain.assert_not_called()


@pytest.mark.parametrize(
    ("endpoint", "kwargs"),
    [
        (media_endpoint.groups, {"tmdbid": 0}),
        (media_endpoint.group_seasons, {"episode_group": "0"}),
    ],
)
def test_explicit_tmdb_endpoints_reject_zero_before_external_chain(
        monkeypatch,
        endpoint,
        kwargs: dict,
) -> None:
    """本文件内的明确 TMDB 辅助入口不得把零值 ID 传给外部链。"""
    tmdb_chain = Mock(side_effect=AssertionError("零值身份不应创建 TMDB 链"))
    media_chain = Mock(side_effect=AssertionError("零值身份不应创建媒体链"))
    monkeypatch.setattr(media_endpoint, "TmdbChain", tmdb_chain)
    monkeypatch.setattr(media_endpoint, "MediaChain", media_chain)

    result = asyncio.run(endpoint(**kwargs, _=None))

    assert result == []
    tmdb_chain.assert_not_called()
    media_chain.assert_not_called()


@pytest.mark.parametrize(
    ("media_source", "media_id", "media_kwargs", "episode_count"),
    [
        (
            MediaSource.Douban,
            "7301",
            {"douban_info": {"episodes_count": 12, "id": "7301", "subtype": "tv"}},
            12,
        ),
        (
            MediaSource.Bangumi,
            "7302",
            {"bangumi_info": {"id": 7302, "platform": "TV", "total_episodes": 13}},
            13,
        ),
        (
            MediaSource.AniList,
            "7303",
            {"anilist_info": {"episodes": 14, "format": "TV", "id": 7303}},
            14,
        ),
    ],
)
def test_media_seasons_uses_source_episode_count(
        monkeypatch,
        media_source: MediaSource,
        media_id: str,
        media_kwargs: dict,
        episode_count: int,
) -> None:
    """非 TMDB 来源应使用自身集数构造季信息。"""
    media = MediaInfo(**media_kwargs)
    media_chain = Mock()
    media_chain.async_recognize_media = AsyncMock(return_value=media)
    monkeypatch.setattr(media_endpoint, "MediaChain", Mock(return_value=media_chain))

    result = asyncio.run(
        media_endpoint.seasons(
            media_source=media_source,
            media_id=media_id,
            season=None,
            _=None,
        )
    )

    assert len(result) == 1
    assert result[0].season_number == 1
    assert result[0].episode_count == episode_count
    media_chain.async_recognize_media.assert_awaited_once_with(
        media_source=media_source,
        media_id=media_id,
        mtype=MediaType.TV,
        cache=False,
    )


def test_media_seasons_does_not_fallback_for_explicit_identity(monkeypatch) -> None:
    """明确身份查询失败时季信息接口应直接返回空列表。"""
    media_chain = Mock()
    media_chain.async_recognize_media = AsyncMock(return_value=None)
    media_chain.async_recognize_by_meta = AsyncMock(
        side_effect=AssertionError("不应按标题切换识别源")
    )
    monkeypatch.setattr(media_endpoint, "MediaChain", Mock(return_value=media_chain))

    result = asyncio.run(
        media_endpoint.seasons(
            media_source=MediaSource.AniList,
            media_id="7403",
            title="来源查询失败剧集",
            year="2026",
            _=None,
        )
    )

    assert result == []
    media_chain.async_recognize_by_meta.assert_not_awaited()


def test_subscribe_add_does_not_fallback_for_explicit_identity() -> None:
    """同步新增订阅的显式身份识别失败后不能按标题换源。"""
    media_chain = Mock()
    media_chain.recognize_media.return_value = None
    media_chain.recognize_by_meta.return_value = None
    chain = object.__new__(SubscribeChain)

    with patch.object(subscribe_module, "MediaChain", return_value=media_chain):
        sid, message = chain.add(
            title="AniList 同步订阅",
            year="2026",
            mtype=MediaType.TV,
            media_source=MediaSource.AniList,
            media_id="154587",
        )

    assert sid is None
    assert message == "未识别到媒体信息"
    media_chain.recognize_media.assert_called_once()
    media_chain.recognize_by_meta.assert_not_called()


def test_subscribe_async_add_does_not_fallback_for_explicit_identity() -> None:
    """异步新增订阅的显式身份识别失败后不能按标题换源。"""
    media_chain = Mock()
    media_chain.async_recognize_media = AsyncMock(return_value=None)
    media_chain.async_recognize_by_meta = AsyncMock(return_value=None)
    chain = object.__new__(SubscribeChain)

    with patch.object(subscribe_module, "MediaChain", return_value=media_chain):
        sid, message = asyncio.run(
            chain.async_add(
                title="AniList 异步订阅",
                year="2026",
                mtype=MediaType.TV,
                media_source=MediaSource.AniList,
                media_id="154587",
            )
        )

    assert sid is None
    assert message == "未识别到媒体信息"
    media_chain.async_recognize_media.assert_awaited_once()
    media_chain.async_recognize_by_meta.assert_not_awaited()
