import asyncio

from app.api.endpoints import media as media_endpoint
from app.api.endpoints import search as search_endpoint
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
    assert captured["source"] == "anilist"
    assert captured["mediaid"] == "154587"
