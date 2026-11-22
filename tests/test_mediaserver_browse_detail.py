"""覆盖媒体服务器统一浏览、筛选、三层详情、播前音轨字幕与进度回传链路。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.endpoints import mediaserver
from app.chain.mediaserver import MediaServerChain
from app.modules._base.emby_browse import (
    EmbyBrowseConfig,
    emby_browse_items,
    emby_filter_options,
    emby_item_detail,
    emby_report_progress,
    emby_search_library,
    emby_season_episodes,
    emby_seasons,
    emby_stream_url,
    emby_subtitle_url,
)
from app.modules._base.plex_browse import (
    plex_browse_items,
    plex_filter_options,
    plex_item_detail,
    plex_report_progress,
    plex_season_episodes,
    plex_seasons,
    plex_subtitle_url,
)
from app.schemas.mediaserver import (
    MediaServerBrowseItem,
    MediaServerBrowsePage,
    MediaServerFilterOptions,
    MediaServerItem,
    MediaServerItemDetail,
    MediaServerMediaSource,
    MediaServerMediaStream,
    MediaServerPerson,
)
from app.schemas.token import TokenPayload
from app.schemas.types import MediaSource, MediaType


def _mock_response(payload: object, status_code: int = 200) -> Mock:
    """构造简易 HTTP 响应桩对象。"""
    resp = Mock(status_code=status_code)
    resp.json.return_value = payload
    return resp


def _emby_config(http_mock: Mock) -> EmbyBrowseConfig:
    """构造用于单测的 Emby 协议配置。"""
    return EmbyBrowseConfig(
        host="http://emby.test/",
        api_key="test-key",
        user_id="user-1",
        server_type="emby",
        api_prefix="emby/",
        play_host="http://emby.test/",
        server_id="srv-1",
        request_factory=lambda: http_mock,
        play_url_builder=lambda item_id: f"http://emby.test/web/index.html#!/item?id={item_id}",
    )


def test_emby_browse_filters_detail_seasons_episodes_and_subtitles() -> None:
    """验证 Emby 系浏览、筛选、三层详情、音轨字幕提取、流地址和进度回传。"""
    http = Mock()
    cfg = _emby_config(http)

    # 1. Browse
    http.get_res.return_value = _mock_response(
        {
            "TotalRecordCount": 2,
            "Items": [
                {
                    "Id": "m-1",
                    "Name": "星际穿越",
                    "OriginalTitle": "Interstellar",
                    "Type": "Movie",
                    "ProductionYear": 2014,
                    "PremiereDate": "2014-11-07T00:00:00Z",
                    "CommunityRating": 8.7,
                    "RunTimeTicks": 101_400_000_000,
                    "ImageTags": {"Primary": "p1"},
                    "BackdropImageTags": ["b1"],
                    "UserData": {"Played": False, "PlaybackPositionTicks": 50_700_000_000},
                }
            ],
        }
    )
    page = emby_browse_items(
        cfg,
        library_id="lib-1",
        genre="科幻",
        year=2014,
        played_filter="unplayed",
        sort_by="rating",
        sort_order="desc",
        keyword="星际",
        start_index=0,
        limit=20,
    )
    assert page is not None
    assert page.total_count == 2
    assert page.has_more is True
    assert len(page.items) == 1
    assert page.items[0].item_id == "m-1"
    assert page.items[0].played_percentage == 50.0
    assert page.items[0].resume_position_ms == 5_070_000

    # 2. Filter options
    http.get_res.return_value = _mock_response({"Genres": ["科幻", "动作"], "Years": [2024, 2014]})
    filters = emby_filter_options(cfg, library_id="lib-1")
    assert filters.genres == ["科幻", "动作"]
    assert filters.years == [2024, 2014]
    assert "date_created" in filters.sort_options

    # 3. Item detail with multi-source & audio/subtitle streams
    http.get_res.return_value = _mock_response(
        {
            "Id": "ep-1",
            "Name": "第一集",
            "Type": "Episode",
            "SeriesId": "series-1",
            "SeriesName": "测试剧集",
            "SeasonId": "season-1",
            "SeasonName": "第 1 季",
            "ParentIndexNumber": 1,
            "IndexNumber": 1,
            "Overview": "分集简介",
            "Taglines": ["精彩开篇"],
            "Genres": ["剧情"],
            "Studios": [{"Name": "HBO"}],
            "People": [{"Id": "actor-1", "Name": "张三", "Role": "主角", "Type": "Actor", "PrimaryImageTag": "a1"}],
            "ProviderIds": {"Tmdb": "1399", "Imdb": "tt0944947"},
            "RunTimeTicks": 36_000_000_000,
            "ImageTags": {"Primary": "still-1"},
            "ParentBackdropItemId": "series-1",
            "ParentBackdropImageTags": ["sb-1"],
            "MediaSources": [
                {
                    "Id": "src-4k",
                    "Name": "4K HDR",
                    "Container": "mkv",
                    "Size": 4_000_000_000,
                    "Bitrate": 15_000_000,
                    "RunTimeTicks": 36_000_000_000,
                    "MediaStreams": [
                        {"Index": 0, "Type": "Video", "Codec": "hevc", "Width": 3840, "Height": 2160, "VideoRange": "HDR"},
                        {"Index": 1, "Type": "Audio", "Codec": "eac3", "Language": "eng", "DisplayTitle": "英语 EAC3 5.1", "Channels": 6, "ChannelLayout": "5.1", "IsDefault": True},
                        {"Index": 2, "Type": "Subtitle", "Codec": "ass", "Language": "chi", "DisplayTitle": "简体中文 (内嵌)", "IsExternal": False},
                        {"Index": 3, "Type": "Subtitle", "Codec": "subrip", "Language": "chi", "DisplayTitle": "简体中文 (外挂)", "IsExternal": True},
                    ],
                }
            ],
        }
    )
    detail = emby_item_detail(cfg, "ep-1")
    assert detail is not None
    assert detail.item_type == "Episode"
    assert detail.series_id == "series-1"
    assert detail.season_id == "season-1"
    assert detail.tmdb_id == "1399"
    assert detail.media_source == MediaSource.TMDB
    assert detail.can_play is True
    assert len(detail.media_sources) == 1
    streams = detail.media_sources[0].streams
    assert [s.type for s in streams] == ["video", "audio", "subtitle", "subtitle"]
    assert streams[2].subtitle_id is None
    assert streams[3].subtitle_id == "ms:src-4k:3.srt"

    # 4. Subtitle URL & Stream URL with explicit media_source_id
    sub_url = emby_subtitle_url(cfg, "ep-1", "ms:src-4k:3.srt")
    assert sub_url == "http://emby.test/emby/Videos/ep-1/src-4k/Subtitles/3/Stream.srt?api_key=test-key"
    assert emby_subtitle_url(cfg, "ep-1", "invalid.srt") is None

    stream_url = emby_stream_url(cfg, "ep-1", "Episode", media_source_id="src-4k")
    assert stream_url is not None
    assert "MediaSourceId=src-4k" in stream_url

    # 5. Seasons & Season Episodes
    http.get_res.return_value = _mock_response(
        {"Items": [{"Id": "season-2", "Name": "第 2 季", "Type": "Season", "IndexNumber": 2}, {"Id": "season-1", "Name": "第 1 季", "Type": "Season", "IndexNumber": 1}]}
    )
    seasons = emby_seasons(cfg, "series-1")
    assert seasons is not None
    assert [s.season_number for s in seasons] == [1, 2]

    http.get_res.return_value = _mock_response(
        {"Items": [{"Id": "ep-2", "Name": "第二集", "Type": "Episode", "ParentIndexNumber": 1, "IndexNumber": 2}]}
    )
    episodes = emby_season_episodes(cfg, "series-1", season_id="season-1")
    assert episodes is not None
    assert episodes[0].subtitle == "S01E02"

    # 6. Search including Movie
    http.get_res.return_value = _mock_response(
        {"Items": [{"Id": "m-1", "Name": "星际穿越", "Type": "Movie", "ProductionYear": 2014}]}
    )
    search_res = emby_search_library(cfg, "星际", [MediaType.MOVIE.value, MediaType.TV.value], 10)
    assert len(search_res) == 1
    assert search_res[0].type == MediaType.MOVIE.value

    # 7. Progress report
    http.post_res.return_value = Mock(status_code=204)
    assert emby_report_progress(cfg, "ep-1", 120_000, media_source_id="src-4k", state="paused") is True
    assert http.post_res.call_args.kwargs["json"]["PositionTicks"] == 1_200_000_000


def test_plex_browse_detail_seasons_episodes_and_progress() -> None:
    """验证 Plex 浏览、筛选、详情轨道解析、三层剧集、字幕流及进度回传。"""
    audio_stream = SimpleNamespace(streamType=2, index=1, codec="ac3", languageCode="eng", displayTitle="English 5.1", channels=6, default=True)
    ext_sub_stream = SimpleNamespace(streamType=3, id=901, index=2, codec="srt", languageCode="zho", displayTitle="中文", key="/library/streams/901")
    part = SimpleNamespace(id=55, container="mkv", size=123456, duration=3600000, streams=[audio_stream, ext_sub_stream])
    media = SimpleNamespace(videoResolution="4k", container="mkv", bitrate=10000, duration=3600000, parts=[part])
    movie = SimpleNamespace(
        ratingKey=101,
        key="/library/metadata/101",
        type="movie",
        title="黑客帝国",
        originalTitle="The Matrix",
        year=1999,
        summary="经典科幻",
        rating=8.7,
        duration=3600000,
        viewOffset=600000,
        isPlayed=False,
        thumb="/library/metadata/101/thumb",
        art="/library/metadata/101/art",
        genres=[SimpleNamespace(tag="科幻")],
        roles=[SimpleNamespace(id=1, tag="基努·里维斯", role="Neo", thumb="/thumb/1")],
        directors=[SimpleNamespace(tag="沃卓斯基")],
        guids=[SimpleNamespace(id="tmdb://603"), SimpleNamespace(id="imdb://tt0133093")],
        media=[media],
        reload=Mock(),
        updateTimeline=Mock(),
    )
    season_obj = SimpleNamespace(
        ratingKey=201, key="/library/metadata/201", type="season", title="第 1 季", index=1, parentRatingKey=200, parentTitle="测试剧", childCount=8
    )
    ep_obj = SimpleNamespace(
        ratingKey=202, key="/library/metadata/202", type="episode", title="试播集", index=1, parentIndex=1, parentRatingKey=201, grandparentRatingKey=200, grandparentTitle="测试剧"
    )
    season_obj.episodes = Mock(return_value=[ep_obj])
    show_obj = SimpleNamespace(seasons=Mock(return_value=[season_obj]), season=Mock(return_value=season_obj))

    section = SimpleNamespace(
        TYPE="movie",
        totalSize=1,
        search=Mock(return_value=[movie]),
        listFilterChoices=Mock(side_effect=lambda kind: [SimpleNamespace(title="科幻")] if kind == "genre" else [SimpleNamespace(title="1999")]),
    )
    plex = SimpleNamespace(
        library=SimpleNamespace(sectionByID=Mock(return_value=section), sections=Mock(return_value=[section])),
        fetchItem=Mock(side_effect=lambda key: movie if str(key) == "101" else (season_obj if str(key) == "201" else show_obj)),
    )

    page = plex_browse_items(plex, "http://plex.test", "tok", library_id="1", genre="科幻", year=1999)
    assert page is not None and len(page.items) == 1
    assert page.items[0].item_id == "101"

    filters = plex_filter_options(plex, library_id="1")
    assert filters.genres == ["科幻"] and filters.years == [1999]

    detail = plex_item_detail(plex, "http://plex.test", "tok", "101")
    assert detail is not None
    assert detail.tmdb_id == "603"
    assert detail.media_sources[0].streams[1].subtitle_id == "ms:plex:901.srt"
    assert plex_subtitle_url("http://plex.test", "tok", "ms:plex:901.srt") == "http://plex.test/library/streams/901?X-Plex-Token=tok"

    assert plex_seasons(plex, "http://plex.test", "tok", "200")[0].season_number == 1
    assert plex_season_episodes(plex, "http://plex.test", "tok", "200", season_id="201")[0].subtitle == "S01E01"
    assert plex_report_progress(plex, "101", 600000, state="paused") is True
    movie.updateTimeline.assert_called_once()


def test_chain_signs_browse_and_detail_images(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证 MediaServerChain 对浏览、详情、演职员、季与分集图片统一签名。"""
    chain = object.__new__(MediaServerChain)
    monkeypatch.setattr(chain, "_sign_image_url", lambda url: f"signed:{url}" if url else url)
    detail = MediaServerItemDetail(
        item_id="1",
        title="演示",
        poster="http://img/p",
        backdrop="http://img/b",
        thumb="http://img/t",
        people=[MediaServerPerson(name="演员", image="http://img/a")],
    )
    page = MediaServerBrowsePage(
        items=[MediaServerBrowseItem(item_id="1", title="演示", poster="http://img/p")],
        total_count=1,
    )
    monkeypatch.setattr(chain, "run_module", Mock(side_effect=[page, detail]))

    signed_page = chain.browse_items(server="Emby")
    assert signed_page is not None
    assert signed_page.items[0].server == "Emby"
    assert signed_page.items[0].poster == "signed:http://img/p"

    signed_detail = chain.item_detail(server="Emby", item_id="1")
    assert signed_detail is not None
    assert signed_detail.backdrop == "signed:http://img/b"
    assert signed_detail.people[0].image == "signed:http://img/a"


def test_mediaserver_browse_detail_and_server_subtitle_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证新 REST 端点及 stream-info / stream-subtitle 对服务端外挂字幕的代理支持。"""
    chain = Mock()
    detail = MediaServerItemDetail(
        item_id="ep-1",
        server="Emby",
        item_type="Episode",
        title="第一集",
        resume_position_ms=30000,
        duration_ms=2400000,
        media_sources=[
            MediaServerMediaSource(
                id="src-1",
                name="1080P",
                streams=[
                    MediaServerMediaStream(
                        index=3,
                        type="subtitle",
                        language="chi",
                        display_title="简体中文",
                        is_external=True,
                        subtitle_id="ms:src-1:3.srt",
                    )
                ],
            )
        ],
    )
    chain.browse_items.return_value = MediaServerBrowsePage(
        items=[MediaServerBrowseItem(item_id="series-1", server="Emby", item_type="Series", title="测试剧集")],
        total_count=1,
    )
    chain.filter_options.return_value = MediaServerFilterOptions(genres=["科幻"], years=[2025], sort_options=["date_created"])
    chain.item_detail.return_value = detail
    chain.seasons.return_value = [MediaServerBrowseItem(item_id="season-1", server="Emby", item_type="Season", title="第 1 季", season_number=1)]
    chain.season_episodes.return_value = [MediaServerBrowseItem(item_id="ep-1", server="Emby", item_type="Episode", title="第一集", season_number=1, episode_number=1)]
    chain.report_progress.return_value = True
    chain.find_item.return_value = ("Emby", MediaServerItem(item_id="ep-1", title="第一集", item_type="Episode"))
    chain.get_stream_url.return_value = "http://emby.local/emby/Videos/ep-1/stream?static=true"
    chain.get_subtitle_url.return_value = "http://emby.local/emby/Videos/ep-1/src-1/Subtitles/3/Stream.srt?api_key=k"

    monkeypatch.setattr(mediaserver, "MediaServerChain", lambda: chain)
    monkeypatch.setattr(mediaserver, "_configured_stream_hosts", lambda *_args, **_kwargs: {"emby.local"})
    monkeypatch.setattr(mediaserver, "_remote_stream_response", lambda url, _r, _h: SimpleNamespace(status_code=200, headers={}, body=url))

    app = FastAPI()
    app.include_router(mediaserver.router, prefix="/api/v1/mediaserver")
    app.dependency_overrides[mediaserver.verify_token] = lambda: TokenPayload(sub=1, username="admin")

    with TestClient(app) as client:
        assert client.get("/api/v1/mediaserver/browse", params={"server": "Emby"}).json()["data"]["total_count"] == 1
        assert client.get("/api/v1/mediaserver/filters", params={"server": "Emby"}).json()["data"]["genres"] == ["科幻"]
        assert client.get("/api/v1/mediaserver/detail", params={"server": "Emby", "itemid": "ep-1"}).json()["data"]["item_id"] == "ep-1"
        assert len(client.get("/api/v1/mediaserver/seasons", params={"server": "Emby", "itemid": "series-1"}).json()["data"]) == 1
        assert len(client.get("/api/v1/mediaserver/season-episodes", params={"server": "Emby", "itemid": "series-1", "season": 1}).json()["data"]) == 1
        assert client.post(
            "/api/v1/mediaserver/progress",
            json={"item_id": "ep-1", "server": "Emby", "position_ms": 45000, "state": "paused"},
        ).json()["success"] is True

        manifest = client.get(
            "/api/v1/mediaserver/stream-info",
            params={"itemid": "ep-1", "server": "Emby", "media_source_id": "src-1"},
        ).json()["data"]
        assert manifest["media_source_id"] == "src-1"
        assert manifest["resume_position_ms"] == 30000
        assert len(manifest["subtitles"]) == 1
        assert manifest["subtitles"][0]["subtitle_id"] == "ms:src-1:3.srt"
