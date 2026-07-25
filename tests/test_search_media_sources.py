import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.api.endpoints import media as media_endpoint
from app.api.endpoints import search as search_endpoint
from app.chain import subscribe as subscribe_module
from app.chain.subscribe import SubscribeChain
from app.core.context import MediaInfo
from app.schemas.types import MediaType


def test_resolve_anilist_search_params_preserves_source_identity() -> None:
    """AniList 媒体键应直接解析为统一搜索身份。"""
    params, message = asyncio.run(
        search_endpoint._resolve_media_search_params("anilist:154587")
    )

    assert message == ""
    assert params == {"source": "anilist", "mediaid": "154587"}


def test_resource_search_forwards_custom_plugin_source(monkeypatch) -> None:
    """资源搜索 API 应把自定义插件来源原样传给搜索链。"""
    captured = {}

    class FakeTorrent:
        """提供资源搜索响应需要的最小种子对象。"""

        @staticmethod
        def to_dict() -> dict:
            """返回可序列化的测试种子。"""
            return {"title": "Plugin result"}

    class FakeSearchChain:
        """记录资源搜索链收到的统一身份。"""

        async def async_search_by_id(self, **kwargs):
            """保存搜索参数并返回单条测试结果。"""
            captured.update(kwargs)
            return [FakeTorrent()]

    monkeypatch.setattr(search_endpoint, "SearchChain", FakeSearchChain)

    response = asyncio.run(
        search_endpoint.search_by_id(
            mediaid="plugin_source:custom-1",
            mtype="tv",
            _=None,
        )
    )

    assert response.success
    assert captured["source"] == "plugin_source"
    assert captured["mediaid"] == "custom-1"
    assert captured["mtype"] == MediaType.TV


def test_subtitle_search_forwards_anilist_identity(monkeypatch) -> None:
    """字幕搜索 API 应把 AniList 身份传给字幕搜索链。"""
    captured = {}

    class FakeSearchChain:
        """记录字幕搜索链收到的统一身份。"""

        async def async_search_subtitles_by_id(self, **kwargs):
            """保存字幕搜索参数并返回空结果。"""
            captured.update(kwargs)
            return []

    monkeypatch.setattr(search_endpoint, "SearchChain", FakeSearchChain)

    source, message = asyncio.run(
        search_endpoint._build_subtitle_search_source(
            mediaid="anilist:154587",
            mtype="tv",
        )
    )
    assert message == ""
    assert asyncio.run(source) == []
    assert captured["source"] == "anilist"
    assert captured["mediaid"] == "154587"


def test_media_detail_forwards_custom_plugin_source(monkeypatch) -> None:
    """媒体详情 API 应允许插件自定义来源处理原生 ID。"""
    captured = {}
    media = MediaInfo(
        source="plugin_source",
        type=MediaType.MOVIE,
        title="Plugin movie",
    )

    class FakeMediaChain:
        """记录详情识别链收到的统一身份。"""

        async def async_recognize_media(self, **kwargs):
            """保存识别参数并返回插件媒体信息。"""
            captured.update(kwargs)
            return media

        async def async_obtain_images(self, _media):
            """跳过测试中的真实图片获取。"""
            return None

    monkeypatch.setattr(media_endpoint, "MediaChain", FakeMediaChain)

    result = asyncio.run(
        media_endpoint.detail(
            mediaid="plugin_source:custom-1",
            type_name=MediaType.MOVIE.value,
            _=None,
        )
    )

    assert result["title"] == "Plugin movie"
    assert captured["source"] == "plugin_source"
    assert captured["mediaid"] == "custom-1"


def test_media_seasons_builds_anilist_season_response(monkeypatch) -> None:
    """AniList 详情应能通过统一季信息接口返回剧集季。"""
    captured = {}
    media = MediaInfo(
        source="anilist",
        type=MediaType.TV,
        title="Frieren",
        anilist_id=154587,
        poster_path="https://images.example.com/frieren.jpg",
        season_info=[{
            "season_number": 1,
            "name": "Season 1",
            "episode_count": 28,
        }],
    )

    class FakeMediaChain:
        """记录季信息识别链收到的 AniList 身份。"""

        async def async_recognize_media(self, **kwargs):
            """保存识别参数并返回 AniList 媒体信息。"""
            captured.update(kwargs)
            return media

    monkeypatch.setattr(media_endpoint, "MediaChain", FakeMediaChain)

    result = asyncio.run(
        media_endpoint.seasons(mediaid="anilist:154587", _=None)
    )

    assert len(result) == 1
    assert result[0].season_number == 1
    assert result[0].episode_count == 28
    assert result[0].poster_path == media.poster_path
    assert captured["source"] == "anilist"
    assert captured["mediaid"] == "154587"


@pytest.mark.parametrize(
    ("mediaid", "media_kwargs", "episode_count"),
    [
        (
            "douban:db-7301",
            {
                "douban_info": {
                    "episodes_count": 12,
                    "id": "db-7301",
                    "subtype": "tv",
                    "title": "豆瓣剧集",
                }
            },
            12,
        ),
        (
            "bangumi:7302",
            {
                "bangumi_info": {
                    "id": 7302,
                    "name_cn": "Bangumi 剧集",
                    "platform": "TV",
                    "total_episodes": 13,
                }
            },
            13,
        ),
        (
            "anilist:7303",
            {
                "anilist_info": {
                    "episodes": 14,
                    "format": "TV",
                    "id": 7303,
                    "title": {"native": "AniList 剧集"},
                }
            },
            14,
        ),
    ],
)
def test_media_seasons_uses_source_episode_count_and_defaults_to_first_season(
        monkeypatch, mediaid: str, media_kwargs: dict, episode_count: int,
) -> None:
    """非 TMDB 来源应使用自身总集数构造第 1 季，不依赖 TMDB。"""
    media = MediaInfo(**media_kwargs)
    media_chain = Mock()
    media_chain.async_recognize_media = AsyncMock(return_value=media)
    monkeypatch.setattr(media_endpoint, "MediaChain", Mock(return_value=media_chain))

    result = asyncio.run(
        media_endpoint.seasons(mediaid=mediaid, season=None, _=None)
    )

    assert media.season is None
    assert media.seasons[1] == list(range(1, episode_count + 1))
    assert len(result) == 1
    assert result[0].season_number == 1
    assert result[0].episode_count == episode_count


@pytest.mark.parametrize(
    "mediaid",
    ["douban:db-7401", "bangumi:7402", "anilist:7403"],
)
def test_media_seasons_does_not_fallback_to_default_source_for_explicit_identity(
        monkeypatch, mediaid: str,
) -> None:
    """明确来源查询失败时应直接返回空列表，不能按标题切换到默认源。"""
    media_chain = Mock()
    media_chain.async_recognize_media = AsyncMock(return_value=None)
    media_chain.async_recognize_by_meta = AsyncMock(
        side_effect=AssertionError("不应按标题切换识别源")
    )
    monkeypatch.setattr(media_endpoint, "MediaChain", Mock(return_value=media_chain))

    result = asyncio.run(
        media_endpoint.seasons(
            mediaid=mediaid,
            title="来源查询失败剧集",
            year="2026",
            _=None,
        )
    )

    assert result == []
    media_chain.async_recognize_media.assert_awaited_once()
    media_chain.async_recognize_by_meta.assert_not_awaited()


def test_subscribe_add_keeps_inferred_anilist_source_during_title_fallback() -> None:
    """同步新增订阅按兼容 ID 推导来源后，标题兜底仍应限定 AniList。"""
    media_chain = Mock()
    media_chain.recognize_by_meta.return_value = None
    chain = object.__new__(SubscribeChain)

    with patch.object(SubscribeChain, "recognize_media", return_value=None) as recognize, \
            patch.object(subscribe_module, "MediaChain", return_value=media_chain):
        sid, message = chain.add(
            title="AniList 同步订阅",
            year="2026",
            mtype=MediaType.TV,
            anilistid=154587,
            tmdbid=209867,
            media_source="anilist",
        )

    assert sid is None
    assert message == "未识别到媒体信息"
    assert recognize.call_args.kwargs["source"] == "anilist"
    assert recognize.call_args.kwargs["mediaid"] == "154587"
    assert media_chain.recognize_by_meta.call_args.kwargs["source"] == "anilist"


def test_subscribe_async_add_keeps_inferred_anilist_source_during_title_fallback() -> None:
    """异步新增订阅按兼容 ID 推导来源后，标题兜底仍应限定 AniList。"""
    media_chain = Mock()
    media_chain.async_recognize_by_meta = AsyncMock(return_value=None)
    chain = object.__new__(SubscribeChain)

    with patch.object(
        SubscribeChain, "async_recognize_media", new=AsyncMock(return_value=None)
    ) as recognize, patch.object(
        subscribe_module, "MediaChain", return_value=media_chain
    ):
        sid, message = asyncio.run(
            chain.async_add(
                title="AniList 异步订阅",
                year="2026",
                mtype=MediaType.TV,
                anilistid=154587,
                tmdbid=209867,
                media_source="anilist",
            )
        )

    assert sid is None
    assert message == "未识别到媒体信息"
    assert recognize.await_args.kwargs["source"] == "anilist"
    assert recognize.await_args.kwargs["mediaid"] == "154587"
    assert media_chain.async_recognize_by_meta.await_args.kwargs["source"] == "anilist"
