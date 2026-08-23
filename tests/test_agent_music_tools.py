"""Agent 音乐工具的实体语义与跨工具上下文契约测试。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.agent.tools.impl._torrent_search_utils import simplify_search_result
from app.agent.tools.impl.add_download_tasks import AddDownloadTasksTool
from app.agent.tools.impl.add_subscribe import AddSubscribeTool
from app.agent.tools.impl.get_recommendations import GetRecommendationsTool
from app.agent.tools.impl.query_library_exists import (
    QueryLibraryExistsInput,
    QueryLibraryExistsTool,
)
from app.agent.tools.impl.query_media_detail import (
    QueryMediaDetailInput,
    QueryMediaDetailTool,
)
from app.agent.tools.impl.query_subscribe_shares import QuerySubscribeSharesTool
from app.agent.tools.impl.query_subscribe_history import QuerySubscribeHistoryTool
from app.agent.tools.impl.recognize_media import RecognizeMediaTool
from app.agent.tools.impl.scrape_metadata import ScrapeMetadataTool
from app.agent.tools.impl.search_media import SearchMediaTool
from app.agent.tools.impl.search_torrents import SearchTorrentsTool
from app.domain.context import (
    MUSIC_ENTITY_ALBUM,
    MUSIC_ENTITY_ARTIST,
    Context,
    MusicAlbumInfo,
    MusicArtistInfo,
    MusicInfo,
    TorrentInfo,
)
from app.domain.meta.metamusic import MetaMusic
from app.schemas.types import MediaSource, MediaType


def _recording() -> MusicInfo:
    """构造 Agent 工具测试使用的单曲信息。"""
    return MusicInfo(
        media_source=MediaSource.MusicBrainz,
        media_id="recording-1",
        title="晴天",
        artists=["周杰伦"],
        album="叶惠美",
        album_id="release-group-1",
        year=2003,
        track_number=3,
        total_tracks=11,
    )


def test_search_torrents_forwards_album_namespace_before_recognition():
    """Agent 精确搜专辑资源时应在识别阶段绑定 album 命名空间。"""
    async_search = AsyncMock(return_value=[])
    async_sites = AsyncMock(return_value=[])
    tool = SearchTorrentsTool(session_id="session-1", user_id="10001")

    with patch(
        "app.agent.tools.impl.search_torrents.SearchChain.async_search_by_id",
        new=async_search,
    ), patch(
        "app.agent.tools.impl.search_torrents.SitesHelper",
        return_value=SimpleNamespace(async_get_indexers=async_sites),
    ):
        asyncio.run(
            tool.run(
                media_type="music",
                music_type="album",
                media_source="musicbrainz",
                media_id="release-group-1",
            )
        )

    assert async_search.await_args.kwargs["music_type"] == "album"


def _album() -> MusicInfo:
    """构造 Agent 工具测试使用的整张专辑信息。"""
    return MusicInfo(
        media_source=MediaSource.MusicBrainz,
        media_id="release-group-1",
        music_type=MUSIC_ENTITY_ALBUM,
        title="叶惠美",
        artists=["周杰伦"],
        album="叶惠美",
        album_id="release-group-1",
        year=2003,
        total_tracks=11,
    )


def test_recognize_music_title_uses_media_chain_primary_source():
    """Agent 音乐标题识别应由 MediaChain 自动选择 MusicBrainz 主数据源。"""
    expected = _recording()
    recognize = AsyncMock(return_value=expected)
    tool = RecognizeMediaTool(session_id="session-1", user_id="10001")

    with patch(
        "app.agent.tools.impl.recognize_media.MediaChain.async_recognize_by_meta",
        new=recognize,
    ):
        result = asyncio.run(
            tool.run(
                title="晴天",
                media_type="music",
                artist="周杰伦",
                album="叶惠美",
            )
        )

    payload = json.loads(result)
    assert payload["media_info"]["media_source"] == "musicbrainz"
    recognized_meta = recognize.await_args.args[0]
    assert recognized_meta.artists == ["周杰伦"]
    assert recognized_meta.album == "叶惠美"
    assert "source" not in recognize.await_args.kwargs


def test_search_media_filters_music_entities_and_returns_stable_identity():
    """音乐搜索应区分单曲和专辑，并返回后续工具可复用的来源 ID。"""
    async_search = AsyncMock(return_value=[_recording(), _album()])
    tool = SearchMediaTool(session_id="session-1", user_id="10001")

    with patch(
        "app.agent.tools.impl.search_media.MediaChain.async_search_music",
        new=async_search,
    ):
        result = asyncio.run(
            tool.run(
                title="叶惠美",
                media_type="music",
                music_type="album",
            )
        )

    payload = json.loads(result)
    assert len(payload) == 1
    assert payload[0]["music_type"] == "album"
    assert payload[0]["media_source"] == "musicbrainz"
    assert payload[0]["media_id"] == "release-group-1"
    assert payload[0]["total_tracks"] == 11


@pytest.mark.parametrize(
    ("music_type", "expected_label"),
    [("recording", "单曲"), ("album", "专辑")],
)
def test_add_subscribe_preserves_track_and_album_modes(music_type, expected_label):
    """单曲与整专订阅应使用同一稳定身份，但保留不同实体模式。"""
    async_add = AsyncMock(return_value=(1, ""))
    tool = AddSubscribeTool(session_id="session-1", user_id="10001")

    with patch(
        "app.agent.tools.impl.add_subscribe.SubscribeChain.async_add",
        new=async_add,
    ):
        result = asyncio.run(
            tool.run(
                title="叶惠美" if music_type == "album" else "晴天",
                media_type="music",
                music_type=music_type,
                media_source="musicbrainz",
                media_id=(
                    "release-group-1" if music_type == "album" else "recording-1"
                ),
            )
        )

    kwargs = async_add.await_args.kwargs
    assert kwargs["mtype"] == MediaType.MUSIC
    assert kwargs["music_type"] == music_type
    assert kwargs["media_source"] == "musicbrainz"
    assert kwargs["season"] is None
    assert expected_label in result


def test_add_subscribe_rejects_artist_as_browse_only_entity():
    """艺术家只能用于浏览，不能误建成无法完成的订阅。"""
    async_add = AsyncMock(return_value=(1, ""))
    tool = AddSubscribeTool(session_id="session-1", user_id="10001")

    with patch(
        "app.agent.tools.impl.add_subscribe.SubscribeChain.async_add",
        new=async_add,
    ):
        result = asyncio.run(
            tool.run(
                title="周杰伦",
                media_type="music",
                music_type="artist",
                media_source="musicbrainz",
                media_id="artist-1",
            )
        )

    assert "艺术家不能订阅" in result
    async_add.assert_not_awaited()


def test_query_album_detail_exposes_complete_track_contract():
    """专辑详情应返回预期曲目总数和曲目身份，供整包搜索与校验使用。"""
    album = MusicAlbumInfo(
        media_source=MediaSource.MusicBrainz,
        media_id="release-group-1",
        title="叶惠美",
        artists=["周杰伦"],
        release_date="2003-07-31",
        tracks=[_recording(), MusicInfo(title="以父之名", track_number=1)],
    )
    async_album = AsyncMock(return_value=album)
    tool = QueryMediaDetailTool(session_id="session-1", user_id="10001")

    with patch(
        "app.agent.tools.impl.query_media_detail.MediaChain.async_get_music_album",
        new=async_album,
    ):
        result = asyncio.run(
            tool.run(
                media_type="music",
                music_type="album",
                media_source="musicbrainz",
                media_id="release-group-1",
            )
        )

    payload = json.loads(result)
    assert payload["music_type"] == "album"
    assert payload["total_tracks"] == 2
    assert payload["tracks_total"] == 2
    assert payload["tracks"][0]["media_id"] == "recording-1"


def test_query_recording_detail_forwards_recording_namespace():
    """Agent 查询单曲详情时必须把 Recording 实体传给统一识别入口。"""
    async_recognize = AsyncMock(return_value=_recording())
    media_chain = Mock()
    media_chain.async_recognize_media = async_recognize
    tool = QueryMediaDetailTool(session_id="session-1", user_id="10001")

    with patch(
        "app.agent.tools.impl.query_media_detail.MediaChain",
        return_value=media_chain,
    ):
        result = asyncio.run(tool.run(
            media_type="music",
            music_type="recording",
            media_source="musicbrainz",
            media_id="recording-1",
        ))

    payload = json.loads(result)
    assert payload["music_type"] == "recording"
    assert async_recognize.await_args.kwargs["music_type"] == "recording"


def test_scrape_album_uses_unified_entity_recognition(tmp_path):
    """Agent 专辑刮削应通过 MediaChain 识别，再交给 ScrapingChain 执行。"""
    album_dir = tmp_path / "叶惠美"
    album_dir.mkdir()
    async_recognize = AsyncMock(return_value=_album())
    media_chain = Mock()
    media_chain.async_recognize_media = async_recognize
    scraping_chain = Mock()
    scraping_chain.scrape_music_metadata.return_value = (True, "已刮削专辑")
    tool = ScrapeMetadataTool(session_id="session-1", user_id="10001")

    with patch(
        "app.agent.tools.impl.scrape_metadata.MediaChain",
        return_value=media_chain,
    ), patch(
        "app.agent.tools.impl.scrape_metadata.ScrapingChain",
        return_value=scraping_chain,
    ):
        result = asyncio.run(tool.run(
            path=str(album_dir),
            media_type="music",
            music_type="album",
            media_source="musicbrainz",
            media_id="release-group-1",
        ))

    assert json.loads(result)["success"] is True
    assert async_recognize.await_args.kwargs["media_source"] == MediaSource.MusicBrainz
    assert async_recognize.await_args.kwargs["media_id"] == "release-group-1"
    assert "source" not in async_recognize.await_args.kwargs
    assert "mediaid" not in async_recognize.await_args.kwargs
    assert async_recognize.await_args.kwargs["music_type"] == "album"


def test_scrape_metadata_rejects_invalid_media_source_before_file_access(tmp_path):
    """Agent 直接调用工具时也必须拒绝格式非法的媒体来源。"""
    audio_file = tmp_path / "unknown-source.flac"
    audio_file.write_bytes(b"audio")
    tool = ScrapeMetadataTool(session_id="session-1", user_id="10001")

    result = asyncio.run(tool.run(
        path=str(audio_file),
        media_type="music",
        media_source="plugin source:invalid",
        media_id="recording-1",
    ))

    payload = json.loads(result)
    assert payload["success"] is False
    assert "media_source" in payload["message"]


def test_scrape_metadata_checks_local_path_in_agent_worker(tmp_path, monkeypatch):
    """Agent 刮削的本地路径检查应通过受控存储线程执行。"""
    calls = []

    async def fake_run_agent_blocking(bucket, func, *args, **kwargs):
        calls.append((bucket, func, args, kwargs))
        return False, False

    monkeypatch.setattr("app.agent.tools.base.run_agent_blocking", fake_run_agent_blocking)
    tool = ScrapeMetadataTool(session_id="session-1", user_id="10001")

    result = asyncio.run(
        tool.run(path=str(tmp_path / "missing"), storage="local")
    )

    payload = json.loads(result)
    assert payload == {
        "success": False,
        "message": f"刮削路径不存在: {tmp_path / 'missing'}",
    }
    assert len(calls) == 1
    assert calls[0][0] == "storage"
    assert calls[0][2] == (tmp_path / "missing",)


def test_query_artist_detail_marks_entity_as_non_subscribable():
    """艺术家详情应明确标记为不可订阅，避免 Agent 混入获取流程。"""
    artist = MusicArtistInfo(
        media_source=MediaSource.MusicBrainz,
        media_id="artist-1",
        name="周杰伦",
        artist_type="Person",
    )
    async_artist = AsyncMock(return_value=artist)
    tool = QueryMediaDetailTool(session_id="session-1", user_id="10001")

    with patch(
        "app.agent.tools.impl.query_media_detail.MediaChain.async_get_music_artist",
        new=async_artist,
    ):
        result = asyncio.run(
            tool.run(
                media_type="music",
                music_type=MUSIC_ENTITY_ARTIST,
                media_source="musicbrainz",
                media_id="artist-1",
            )
        )

    payload = json.loads(result)
    assert payload["music_type"] == "artist"
    assert payload["subscribable"] is False


def test_torrent_result_serializes_music_without_video_only_attributes():
    """音乐种子结果不得访问季号、视频编码等影视专属属性而崩溃。"""
    context = Context(
        meta_info=MetaMusic(
            title="叶惠美",
            artists=["周杰伦"],
            album="叶惠美",
            total_tracks=11,
        ),
        media_info=_album(),
        torrent_info=TorrentInfo(
            title="周杰伦 - 叶惠美 FLAC",
            enclosure="https://example.invalid/download/1",
            site_name="Demo",
            category=MediaType.MUSIC.value,
        ),
    )

    payload = simplify_search_result(context, index=1)

    assert payload["media_info"]["music_type"] == "album"
    assert payload["meta_info"]["type"] == "music"
    assert payload["meta_info"]["total_tracks"] == 11
    assert payload["torrent_info"]["torrent_url"].endswith(":1")


def test_add_download_preserves_album_context_and_full_coverage_marker():
    """Agent 从搜索引用下载整专时必须保留 MetaMusic 和完整覆盖事实。"""
    meta = MetaMusic(
        title="叶惠美",
        artists=["周杰伦"],
        album="叶惠美",
        total_tracks=11,
    )
    cached_context = Context(
        meta_info=meta,
        media_info=_album(),
        torrent_info=TorrentInfo(
            title="周杰伦 - 叶惠美 FLAC",
            enclosure="https://example.invalid/download/1",
            site_name="Demo",
            category=MediaType.MUSIC.value,
        ),
        confirmed_full_coverage=True,
    )
    site = SimpleNamespace(
        ua="ua",
        cookie="cookie",
        proxy=False,
        pri=1,
        downloader="qb",
    )
    submitted_contexts = []

    def fake_download(context, _downloader, _save_path, _labels):
        """记录提交给下载链的上下文。"""
        submitted_contexts.append(context)
        return "download-1", None

    tool = AddDownloadTasksTool(session_id="session-1", user_id="10001")
    with patch.object(
        AddDownloadTasksTool,
        "_async_resolve_cached_context",
        new=AsyncMock(return_value=cached_context),
    ), patch(
        "app.agent.tools.impl.add_download_tasks.SiteOper.async_get_by_name",
        new=AsyncMock(return_value=site),
    ), patch.object(
        AddDownloadTasksTool,
        "_download_single_sync",
        side_effect=fake_download,
    ):
        result = asyncio.run(tool.run(torrent_url=["abcdef0:1"]))

    assert result == "任务添加成功"
    assert len(submitted_contexts) == 1
    submitted = submitted_contexts[0]
    assert submitted is not cached_context
    assert isinstance(submitted.meta_info, MetaMusic)
    assert submitted.meta_info.total_tracks == 11
    assert submitted.media_info.music_type == "album"
    assert submitted.confirmed_full_coverage is True


def test_query_subscribe_history_uses_database_media_values_and_music_fields(monkeypatch):
    """历史查询应使用数据库中文枚举值，并返回音乐实体身份。"""
    calls = []
    record = SimpleNamespace(
        id=1,
        name="叶惠美",
        year="2003",
        type=MediaType.MUSIC.value,
        season=None,
        media_source="musicbrainz",
        media_id="release-group-1",
        music_type="album",
        total_tracks=11,
        poster=None,
        vote=0,
        total_episode=None,
        date="2026-08-10 07:00:00",
        username="tester",
        filter=None,
        quality=None,
        resolution=None,
    )

    class FakeHistoryOper:
        """记录订阅历史查询参数的最小测试替身。"""

        async def async_list_by_type(self, mtype, page, count):
            """仅为音乐类型返回一条历史。"""
            calls.append((mtype, page, count))
            return [record] if mtype == MediaType.MUSIC.value else []

    monkeypatch.setattr(
        "app.agent.tools.impl.query_subscribe_history.SubscribeHistoryOper",
        FakeHistoryOper,
    )
    tool = QuerySubscribeHistoryTool(session_id="session-1", user_id="10001")

    result = asyncio.run(tool.run(media_type="all"))

    payload = json.loads(result.split("\n\n", 1)[1])
    assert [call[0] for call in calls] == [
        MediaType.MOVIE.value,
        MediaType.TV.value,
        MediaType.MUSIC.value,
    ]
    assert payload[0]["type"] == "music"
    assert payload[0]["music_type"] == "album"
    assert payload[0]["total_tracks"] == 11


def test_music_history_filter_excludes_video_records(monkeypatch):
    """all + recording 过滤不能把缺少 music_type 的影视历史当成旧单曲。"""
    movie_record = SimpleNamespace(
        type=MediaType.MOVIE.value,
        music_type=None,
        date="2026-08-10 08:00:00",
    )

    class FakeHistoryOper:
        """返回一条电影历史的最小测试替身。"""

        async def async_list_by_type(self, mtype, page, count):
            """仅为电影类型返回记录。"""
            return [movie_record] if mtype == MediaType.MOVIE.value else []

    monkeypatch.setattr(
        "app.agent.tools.impl.query_subscribe_history.SubscribeHistoryOper",
        FakeHistoryOper,
    )
    tool = QuerySubscribeHistoryTool(session_id="session-1", user_id="10001")

    result = asyncio.run(
        tool.run(media_type="all", music_type="recording")
    )

    assert result == "未找到相关订阅历史记录"


def test_music_scrape_routes_audio_to_tag_cover_and_lyrics_pipeline(tmp_path):
    """音频刮削应进入音乐流程，并原样返回歌词处理统计消息。"""
    audio_file = tmp_path / "晴天.flac"
    audio_file.write_bytes(b"audio")
    tool = ScrapeMetadataTool(session_id="session-1", user_id="10001")

    with patch(
        "app.agent.tools.impl.scrape_metadata.ScrapingChain.scrape_music_metadata",
        return_value=(True, "音乐刮削完成，歌词新增 1 首"),
    ) as scrape_music:
        result = asyncio.run(
            tool.run(path=str(audio_file), media_type="music")
        )

    payload = json.loads(result)
    assert payload["success"] is True
    assert payload["type"] == "music"
    assert "歌词新增 1 首" in payload["message"]
    scrape_music.assert_called_once()


def test_query_library_exists_treats_album_as_atomic_complete_entity():
    """整专媒体库查询应返回曲目完整性，且不进入电视剧 seasons 分支。"""
    async_recognize = AsyncMock(return_value=_album())
    exists = SimpleNamespace(
        type=MediaType.MUSIC,
        server="Navidrome",
    )
    tool = QueryLibraryExistsTool(session_id="session-1", user_id="10001")

    with patch(
        "app.agent.tools.impl.query_library_exists."
        "MediaChain.async_recognize_media",
        new=async_recognize,
    ), patch.object(
        QueryLibraryExistsTool,
        "_get_media_server_names",
        return_value=[],
    ), patch.object(
        QueryLibraryExistsTool,
        "_query_media_exists",
        return_value=exists,
    ):
        result = asyncio.run(
            tool.run(
                media_type="music",
                music_type="album",
                media_source="musicbrainz",
                media_id="release-group-1",
            )
        )

    payload = json.loads(result)[0]
    assert payload["music_type"] == "album"
    assert payload["total_tracks"] == 11
    assert payload["servers"]["Navidrome"] == {
        "exists": True,
        "complete": True,
        "expected_tracks": 11,
    }


def test_agent_identity_schemas_only_expose_media_source_and_media_id():
    """Agent 精确媒体工具不得继续暴露任一数据源专用 ID 输入字段。"""
    legacy_fields = {"tmdb_id", "douban_id", "bangumi_id", "anilist_id"}
    for schema in (QueryMediaDetailInput, QueryLibraryExistsInput):
        assert legacy_fields.isdisjoint(schema.model_fields)
        assert schema.model_fields["media_source"].annotation is MediaSource


def test_listenbrainz_album_chart_preserves_entity_and_bounded_page_size():
    """音乐榜单应把专辑实体与有界分页参数传递给推荐链。"""
    async_chart = AsyncMock(return_value=[_album()])
    tool = GetRecommendationsTool(session_id="session-1", user_id="10001")

    with patch(
        "app.agent.tools.impl.get_recommendations.RecommendChain.async_music_chart",
        new=async_chart,
    ):
        result = asyncio.run(
            tool.run(
                source="listenbrainz_chart",
                media_type="music",
                music_type="album",
                page=2,
            )
        )

    payload = json.loads(result)
    assert payload[0]["music_type"] == "album"
    assert payload[0]["media_source"] == "musicbrainz"
    assert payload[0]["media_id"] == "release-group-1"
    assert async_chart.await_args.kwargs["entity"] == "album"
    assert async_chart.await_args.kwargs["page"] == 2
    assert async_chart.await_args.kwargs["count"] == 20


def test_subscribe_shares_normalize_legacy_music_type():
    """旧音乐分享应输出 Agent 类型并按单曲语义兼容空实体字段。"""
    async_shares = AsyncMock(return_value=[{
        "id": 1,
        "name": "晴天",
        "type": MediaType.MUSIC.value,
        "music_type": None,
        "media_source": "musicbrainz",
        "media_id": "recording-1",
    }])
    tool = QuerySubscribeSharesTool(session_id="session-1", user_id="10001")

    with patch(
        "app.agent.tools.impl.query_subscribe_shares."
        "MoviePilotServerHelper.async_get_subscribe_shares",
        new=async_shares,
    ):
        result = asyncio.run(tool.run())

    payload = json.loads(result.split("\n\n", 1)[1])
    assert payload[0]["type"] == "music"
    assert payload[0]["music_type"] == "recording"


def test_subscribe_shares_hide_server_legacy_identity_fields():
    """V3 Agent 只输出统一身份，中心服务为旧客户端合成的字段不得继续传播。"""
    async_shares = AsyncMock(return_value=[{
        "id": 2,
        "name": "测试电影",
        "type": MediaType.MOVIE.value,
        "media_source": "themoviedb",
        "media_id": "123",
        "tmdbid": 123,
        "doubanid": "legacy-douban",
    }])
    tool = QuerySubscribeSharesTool(session_id="session-1", user_id="10001")

    with patch(
        "app.agent.tools.impl.query_subscribe_shares."
        "MoviePilotServerHelper.async_get_subscribe_shares",
        new=async_shares,
    ):
        result = asyncio.run(tool.run())

    payload = json.loads(result.split("\n\n", 1)[1])
    assert payload[0]["media_source"] == "themoviedb"
    assert payload[0]["media_id"] == "123"
    assert "tmdbid" not in payload[0]
    assert "doubanid" not in payload[0]
