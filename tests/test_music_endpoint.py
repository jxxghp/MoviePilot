import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException

from app.api.endpoints import media as media_endpoints
from app.api.endpoints.music import (
    explore_music,
    music_album,
    music_album_related,
    music_artist,
    music_artist_albums,
    music_artist_related,
    recognize_music,
)
from app.domain.context import MusicAlbumInfo, MusicArtistInfo, MusicInfo, MusicRelease
from app.schemas.music import MusicRecognizeRequest
from app.schemas.types import MediaSource, MediaType


def test_music_routes_are_registered():
    """V1 API 应注册音乐详情识别、探索及艺术家专辑浏览路由。"""
    from app.api.router_specs import API_V1_ROUTER_SPECS

    routes = {
        (f"{spec.prefix}{route.path}", tuple(route.methods or []))
        for spec in API_V1_ROUTER_SPECS
        for route in spec.router.routes
        if spec.prefix == "/music" or spec.prefix == "/media"
        or spec.prefix == "/recommend"
    }

    assert any(path == "/music/recognize" and "POST" in methods for path, methods in routes)
    assert any(path == "/music/explore" and "GET" in methods for path, methods in routes)
    assert any(path == "/music/album/{album_id}" and "GET" in methods for path, methods in routes)
    assert any(
        path == "/music/album/{album_id}/related" and "GET" in methods
        for path, methods in routes
    )
    assert any(path == "/music/artist/{artist_id}" and "GET" in methods for path, methods in routes)
    assert any(
        path == "/music/artist/{artist_id}/albums" and "GET" in methods
        for path, methods in routes
    )
    assert any(
        path == "/music/artist/{artist_id}/related" and "GET" in methods
        for path, methods in routes
    )
    assert any(
        path == "/media/search" and "GET" in methods for path, methods in routes
    )
    for recommend_path in ("/recommend/music_douban",):
        assert any(
            path == recommend_path and "GET" in methods
            for path, methods in routes
        )
    assert not any(
        path.startswith("/recommend/music_theaudiodb")
        for path, _methods in routes
    )


def test_media_search_routes_music_queries_with_query_kwarg():
    """统一媒体搜索的音乐分支应以关键字参数调用 MediaChain。"""

    chain = Mock()
    chain.async_search_music = AsyncMock(
        return_value=[
            MusicInfo(
                media_source="musicbrainz",
                media_id="recording-1",
                music_type="recording",
                title="晴天",
                artists=["周杰伦"],
                release_date="2003-07-31",
                category="Album / Studio",
            )
        ]
    )

    with (
        patch.object(media_endpoints, "MediaChain", return_value=chain) as media_chain,
    ):
        result = asyncio.run(
            media_endpoints.search(
                title="晴天",
                type="music",
                count=30,
                page=1,
                _=Mock(),
            )
        )

    assert result[0]["media_id"] == "recording-1"
    assert result[0]["music_type"] == "recording"
    assert result[0]["title"] == "晴天"
    chain.async_search_music.assert_awaited_once_with(query="晴天", limit=30)
    media_chain.assert_called_once()


def test_media_search_forwards_explicit_music_source():
    """统一音乐搜索应把显式选择的可扩展音乐源转发给 MediaChain。"""
    chain = Mock()
    chain.async_search_music = AsyncMock(return_value=[])

    with patch.object(media_endpoints, "MediaChain", return_value=chain):
        result = asyncio.run(
            media_endpoints.search(
                title="Coldplay",
                type="music",
                count=20,
                media_source="theaudiodb",
                _=Mock(),
            )
        )

    assert result == []
    chain.async_search_music.assert_awaited_once_with(
        query="Coldplay",
        limit=20,
        media_source=(MediaSource.TheAudioDB,),
    )


def test_recognize_music_returns_detail():
    """音乐识别接口应按来源和 ID 经统一识别入口返回详情。"""
    from app.application.orchestration.media import MediaChain

    chain = Mock()
    chain.async_recognize_media = AsyncMock(
        return_value=MusicInfo(
            media_source="musicbrainz",
            media_id="recording-1",
            title="晴天",
        )
    )

    with patch("app.api.endpoints.music.MediaChain", return_value=chain):
        result = asyncio.run(
            recognize_music(
                    request=MusicRecognizeRequest(
                        media_source="musicbrainz",
                        media_id="recording-1",
                        music_type="recording",
                ),
                _=Mock(),
            )
        )

    assert result.media_id == "recording-1"
    chain.async_recognize_media.assert_awaited_once_with(
        media_source="musicbrainz",
        media_id="recording-1",
        mtype=MediaType.MUSIC,
        music_type="recording",
    )


def test_recognize_music_returns_404_for_unknown_item():
    """音乐详情不存在时接口应返回 404。"""
    from app.application.orchestration.media import MediaChain

    chain = Mock()
    chain.async_recognize_media = AsyncMock(return_value=None)

    with (
        patch("app.api.endpoints.music.MediaChain", return_value=chain),
        pytest.raises(HTTPException) as error,
    ):
        asyncio.run(
            recognize_music(
                request=MusicRecognizeRequest(media_source="musicbrainz", media_id="missing"),
                _=Mock(),
            )
        )

    assert error.value.status_code == 404


def test_explore_music_forwards_filters_and_serializes_chart():
    """音乐探索接口应传递实体、周期、排序、热度和封面筛选条件。"""
    chain = Mock()
    chain.async_music_chart = AsyncMock(
        return_value=[
            MusicInfo(
                media_source="musicbrainz",
                media_id="recording-1",
                title="晴天",
                artists=["周杰伦"],
                listen_count=123,
            )
        ]
    )

    with patch("app.api.endpoints.music.RecommendChain", return_value=chain):
        result = asyncio.run(
            explore_music(
                page=2,
                count=20,
                range_name="this_week",
                sort_by="listen_count.asc",
                min_listen_count=100,
                with_cover=True,
                _=Mock(),
            )
        )

    assert result[0].listen_count == 123
    chain.async_music_chart.assert_awaited_once_with(
        range_name="this_week",
        page=2,
        count=20,
        sort_by="listen_count.asc",
        min_listen_count=100,
        with_cover=True,
        entity="recording",
    )


def test_explore_music_supports_official_fresh_release_mode():
    """新发行模式应按 ListenBrainz 官方排序和时间窗口请求探索数据。"""
    chain = Mock()
    chain.async_music_fresh_releases = AsyncMock(
        return_value=[
            MusicInfo(
                media_source="musicbrainz",
                media_id="release-group-1",
                music_type="album",
                title="ARIRANG",
                artists=["BTS"],
            )
        ]
    )

    with patch("app.api.endpoints.music.RecommendChain", return_value=chain):
        result = asyncio.run(
            explore_music(
                page=1,
                count=30,
                mode="fresh",
                sort="artist_credit_name",
                days=30,
                past=True,
                future=False,
                with_cover=True,
                _=Mock(),
            )
        )

    assert result[0].music_type == "album"
    chain.async_music_fresh_releases.assert_awaited_once_with(
        days=30,
        sort="artist_credit_name",
        past=True,
        future=False,
        page=1,
        count=30,
        with_cover=True,
    )


def test_explore_music_forces_douban_music_to_tag_browsing():
    """豆瓣音乐探索即使收到榜单模式也应固定分类浏览，不与推荐页重复。"""
    chain = Mock()
    chain.async_music_discover = AsyncMock(
        return_value=[
            MusicInfo(
                media_source="doubanmusic",
                media_id="album-1",
                music_type="album",
                title="范特西",
            )
        ]
    )

    with patch("app.api.endpoints.music.RecommendChain", return_value=chain):
        result = asyncio.run(
            explore_music(
                media_source="doubanmusic",
                entity="album",
                mode="chart",
                tags="流行,华语",
                douban_sort="S",
                page=2,
                count=20,
                _=Mock(),
            )
        )

    assert result[0].media_source == "doubanmusic"
    chain.async_music_discover.assert_awaited_once_with(
        media_source=MediaSource.DoubanMusic,
        page=2,
        count=20,
        entity="album",
        mode="tag",
        tags="流行,华语",
        sort="S",
    )


def test_explore_music_filters_missing_covers_for_external_sources():
    """外部音乐源选择仅有封面时应在统一响应层过滤无图条目。"""
    chain = Mock()
    chain.async_music_discover = AsyncMock(
        return_value=[
            MusicInfo(media_source="doubanmusic", media_id="album-1", title="No Cover"),
            MusicInfo(
                media_source="doubanmusic",
                media_id="album-2",
                title="With Cover",
                cover_url="https://img.example/album-2.jpg",
            ),
        ]
    )

    with patch("app.api.endpoints.music.RecommendChain", return_value=chain):
        result = asyncio.run(
            explore_music(
                media_source="doubanmusic",
                with_cover=True,
                _=Mock(),
            )
        )

    assert [item.media_id for item in result] == ["album-2"]


def test_music_album_returns_tracks_and_releases():
    """专辑接口应返回专辑详情、曲目和发行版本。"""
    chain = Mock()
    chain.async_get_music_album = AsyncMock(
        return_value=MusicAlbumInfo(
            media_source="musicbrainz",
            media_id="release-group-1",
            title="A Night at the Opera",
            artists=["Queen"],
            artist_ids=["artist-1"],
            album_type="Album",
            release_date="1975-11-21",
            tracks=[MusicInfo(media_source="musicbrainz", media_id="recording-1", title="Love of My Life")],
            releases=[MusicRelease(media_id="release-1", title="A Night at the Opera", date="1975")],
        )
    )

    with patch("app.api.endpoints.music.MediaChain", return_value=chain):
        result = asyncio.run(music_album(album_id="release-group-1", _=Mock()))

    assert result.music_type == "album"
    assert result.year == 1975
    assert result.total_tracks == 1
    assert result.tracks[0].media_id == "recording-1"
    assert result.releases[0].media_id == "release-1"
    chain.async_get_music_album.assert_awaited_once_with(
        media_source=MediaSource.MusicBrainz, media_id="release-group-1"
    )


def test_music_album_returns_404_for_unknown_album():
    """专辑不存在时接口应返回 404。"""
    chain = Mock()
    chain.async_get_music_album = AsyncMock(return_value=None)

    with (
        patch("app.api.endpoints.music.MediaChain", return_value=chain),
        pytest.raises(HTTPException) as error,
    ):
        asyncio.run(music_album(album_id="missing", _=Mock()))

    assert error.value.status_code == 404


def test_music_album_related_returns_source_results():
    """专辑关联浏览接口应传递来源和数量并序列化结果。"""
    chain = Mock()
    chain.async_get_music_album_related = AsyncMock(
        return_value=[
            MusicInfo(
                media_source="doubanmusic",
                media_id="album-2",
                music_type="album",
                title="依然范特西",
            )
        ]
    )

    with patch("app.api.endpoints.music.MediaChain", return_value=chain):
        result = asyncio.run(
            music_album_related(
                album_id="album-1",
                count=12,
                media_source="doubanmusic",
                _=Mock(),
            )
        )

    assert result[0].media_id == "album-2"
    chain.async_get_music_album_related.assert_awaited_once_with(
        media_source=MediaSource.DoubanMusic,
        media_id="album-1",
        count=12,
    )


def test_music_artist_returns_detail():
    """艺术家接口应返回名称、类型和活跃时间。"""
    chain = Mock()
    chain.async_get_music_artist = AsyncMock(
        return_value=MusicArtistInfo(
            media_source="musicbrainz",
            media_id="artist-1",
            name="Queen",
            artist_type="Group",
            begin_date="1970-06-27",
        )
    )

    with patch("app.api.endpoints.music.MediaChain", return_value=chain):
        result = asyncio.run(music_artist(artist_id="artist-1", _=Mock()))

    assert result.name == "Queen"
    assert result.title == "Queen"
    assert result.music_type == "artist"
    chain.async_get_music_artist.assert_awaited_once_with(
        media_source=MediaSource.MusicBrainz, media_id="artist-1"
    )


def test_music_artist_albums_forwards_pagination_and_type():
    """艺术家专辑接口应传递分页和专辑类型筛选。"""
    chain = Mock()
    chain.async_get_music_artist_albums = AsyncMock(
        return_value=[
            MusicInfo(
                media_source="musicbrainz",
                media_id="release-group-1",
                music_type="album",
                title="News of the World",
            )
        ]
    )

    with patch("app.api.endpoints.music.MediaChain", return_value=chain):
        result = asyncio.run(
            music_artist_albums(artist_id="artist-1", page=2, count=10, album_type="ep", _=Mock())
        )

    assert result[0].media_id == "release-group-1"
    chain.async_get_music_artist_albums.assert_awaited_once_with(
        media_source=MediaSource.MusicBrainz,
        media_id="artist-1",
        page=2,
        count=10,
        album_type="ep",
    )


def test_music_artist_related_returns_relationship_text():
    """关联艺术家接口应返回关系说明，供详情页展示。"""
    chain = Mock()
    chain.async_get_music_artist_related = AsyncMock(
        return_value=[
            MusicArtistInfo(
                media_source="musicbrainz",
                media_id="artist-2",
                name="Freddie Mercury",
                relation="member of band",
            )
        ]
    )

    with patch("app.api.endpoints.music.MediaChain", return_value=chain):
        result = asyncio.run(music_artist_related(artist_id="artist-1", count=5, _=Mock()))

    assert result[0].relation == "member of band"
    chain.async_get_music_artist_related.assert_awaited_once_with(
        media_source=MediaSource.MusicBrainz,
        media_id="artist-1",
        count=5,
    )
