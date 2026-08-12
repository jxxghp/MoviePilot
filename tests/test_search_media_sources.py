import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.api.endpoints import media as media_endpoint
from app.api.endpoints import search as search_endpoint
from app.chain import subscribe as subscribe_module
from app.chain.subscribe import SubscribeChain
from app.core.context import MediaInfo
from app.schemas.types import MediaSource, MediaType
from app.utils.media import normalize_media_source


def test_media_source_normalization_rejects_unknown_source() -> None:
    """固定枚举之外的来源不能进入统一身份链路。"""
    assert normalize_media_source("plugin_source") is None
    assert normalize_media_source("tmdb") == MediaSource.TMDB


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

    assert isinstance(result, media_endpoint.schemas.MediaInfo)
    media_chain.async_recognize_by_meta.assert_not_awaited()


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
