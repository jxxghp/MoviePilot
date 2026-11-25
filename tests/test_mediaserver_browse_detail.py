"""
覆盖媒体服务器统一浏览、筛选、三层详情、播前音轨字幕与进度回传链路。
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.endpoints import mediaserver
from app.chain.mediaserver import MediaServerChain
from app.modules._base.embybrowse import (
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
from app.modules._base.plexbrowse import (
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

    # 7. Progress report (API Key 兜底 UserData 与用户令牌实时 Sessions 双模式)
    http.post_res.return_value = Mock(status_code=200)
    assert emby_report_progress(cfg, "ep-1", 120_000, media_source_id="src-4k", state="paused") is True
    assert http.post_res.call_args.kwargs["json"]["PlaybackPositionTicks"] == 1_200_000_000

    session_cfg = EmbyBrowseConfig(
        host=cfg.host,
        api_key="user-access-token",
        user_id=cfg.user_id,
        server_type=cfg.server_type,
        api_prefix=cfg.api_prefix,
        play_host=cfg.play_host,
        server_id=cfg.server_id,
        request_factory=lambda: http,
        is_user_token=True,
    )
    http.post_res.reset_mock()
    http.post_res.return_value = Mock(status_code=204)
    assert emby_report_progress(session_cfg, "ep-1", 120_000, media_source_id="src-4k", state="paused") is True
    assert http.post_res.call_args.kwargs["json"]["PositionTicks"] == 1_200_000_000

    # 8. Emby API Key 兜底：当 POST .../UserData 返回 404/405 时自动回退到 PlaystateService PlayingItems
    http.post_res.reset_mock()
    http.delete_res.reset_mock()
    http.post_res.side_effect = [Mock(status_code=405), Mock(status_code=204)]
    assert emby_report_progress(cfg, "ep-1", 180_000, media_source_id="src-4k", state="paused", is_paused=True) is True
    assert http.post_res.call_count == 2
    assert "/PlayingItems/ep-1/Progress" in http.post_res.call_args.kwargs["url"]
    assert http.post_res.call_args.kwargs["params"]["PositionTicks"] == 1_800_000_000
    assert http.post_res.call_args.kwargs["params"]["IsPaused"] == "true"

    http.post_res.side_effect = None
    http.post_res.return_value = Mock(status_code=404)
    http.delete_res.return_value = Mock(status_code=204)
    assert emby_report_progress(cfg, "ep-1", 240_000, media_source_id="src-4k", state="stopped") is True
    assert "/PlayingItems/ep-1" in http.delete_res.call_args.kwargs["url"]
    assert http.delete_res.call_args.kwargs["params"]["PositionTicks"] == 2_400_000_000


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


def test_emby_and_plex_boxset_collections() -> None:
    """验证 Emby 与 Plex 合集（BoxSet / Collection）列表浏览及按 parent_id 下钻合集子作品。"""
    captured_params: list[dict[str, Any]] = []

    def fake_get_res(_url: str, params: dict[str, Any]) -> Any:
        captured_params.append(dict(params))
        if params.get("ParentId") == "box-1":
            return _mock_response(
                {
                    "Items": [
                        {"Id": "m-1", "Name": "哈利·波特与魔法石", "Type": "Movie", "ProductionYear": 2001}
                    ],
                    "TotalRecordCount": 1,
                }
            )
        return _mock_response(
            {
                "Items": [
                    {"Id": "box-1", "Name": "哈利·波特合集", "Type": "BoxSet", "ChildCount": 8}
                ],
                "TotalRecordCount": 1,
            }
        )

    http = Mock()
    http.get_res.side_effect = fake_get_res
    cfg = _emby_config(http)

    boxset_page = emby_browse_items(
        cfg,
        library_id="lib-box",
        item_type="BoxSet",
    )
    assert boxset_page is not None and len(boxset_page.items) == 1
    assert boxset_page.items[0].item_type == "BoxSet"
    assert boxset_page.items[0].media_type == "合集"
    assert boxset_page.items[0].subtitle == "共 8 部"
    assert captured_params[0]["IncludeItemTypes"] == "BoxSet"

    children_page = emby_browse_items(
        cfg,
        parent_id="box-1",
    )
    assert children_page is not None and len(children_page.items) == 1
    assert children_page.items[0].item_id == "m-1"
    assert captured_params[1]["Recursive"] == "false"

    plex_movie = SimpleNamespace(
        ratingKey="501",
        type="movie",
        title="指环王：护戒使者",
        year=2001,
        thumb="/t/501",
    )
    plex_collection = SimpleNamespace(
        ratingKey="900",
        type="collection",
        title="指环王合集",
        childCount=3,
        thumb="/t/900",
        items=Mock(return_value=[plex_movie]),
    )
    plex = SimpleNamespace(
        fetchItem=Mock(return_value=plex_collection),
        library=SimpleNamespace(sections=Mock(return_value=[])),
    )
    plex_children = plex_browse_items(plex, "http://plex.test", "tok", parent_id="900")
    assert plex_children is not None and len(plex_children.items) == 1
    assert plex_children.items[0].item_id == "501"


def test_vyo_browse_detail_seasons_episodes_and_search() -> None:
    """
    验证 Vyo 媒体服务器浏览、筛选、详情、季集展开与库内搜索转换行为。
    """
    from app.modules.vyo.vyo import Vyo
    from app.schemas.types import MediaType

    vyo = Vyo.__new__(Vyo)
    vyo._host = "http://vyo.test"
    vyo._apikey = "secret"
    fake_api = SimpleNamespace(
        is_configured=Mock(return_value=True),
        image_url=lambda item_id, kind: f"http://vyo.test/img/{item_id}/{kind}",
        play_url=lambda item_id: f"http://vyo.test/play/{item_id}",
        request=Mock(),
    )
    vyo._api = fake_api

    movie_row = {
        "id": "v-1",
        "title": "沙丘",
        "kind": "Movie",
        "year": 2021,
        "has_backdrop": True,
        "duration_ticks": 9_300_000_000,
        "user_data": {"played": False, "position_ticks": 3_000_000_000},
        "tmdb_id": 438631,
        "parent_id": "lib-1",
        "metadata_info": {
            "original_title": "Dune",
            "overview": "沙漠星球史诗",
            "rating": 8.0,
            "genres": ["科幻"],
            "external_ids": {"imdb_id": "tt1160419", "tvdb_id": "999"},
        },
        "sources": [{"id": "src-1", "path": "/media/dune.mkv", "size": 1024}],
    }
    season_row = {"id": "vs-1", "title": "第 1 季", "kind": "Season", "season": 1}
    episode_row = {
        "id": "ve-1",
        "title": "第 1 集",
        "kind": "Episode",
        "season": 1,
        "episode": 1,
        "series_id": "v-show",
        "series_name": "测试剧",
        "season_id": "vs-1",
    }

    def fake_request(path: str, **_kwargs: Any) -> Any:
        """
        按接口路径返回模拟的 Vyo 响应。
        """
        if path == "/items/v-1":
            return SimpleNamespace(success=True, data=movie_row)
        return SimpleNamespace(success=False, data=None)

    fake_api.request.side_effect = fake_request

    vyo._Vyo__query_items = Mock(
        side_effect=[
            {"items": [movie_row], "total": 2},
            {"items": [season_row]},
            {"items": [episode_row]},
        ]
    )
    vyo._Vyo__find_season_id = Mock(return_value="vs-1")
    vyo._Vyo__search_rows = Mock(return_value=[movie_row])
    vyo._Vyo__format_item_info = Mock(
        return_value=SimpleNamespace(item_id="v-1", title="沙丘", year=2021, item_type="Movie")
    )
    vyo.get_play_url = Mock(return_value="http://vyo.test/play/v-1")

    page = vyo.browse_items(library_id="lib-1", item_type="movie", sort_by="rating", limit=10)
    assert page is not None and page.has_more is True and page.items[0].title == "沙丘"

    opts = vyo.get_filter_options(library_id="lib-1")
    assert "Movie" in opts.item_types

    detail = vyo.get_item_detail("v-1")
    assert detail is not None and detail.can_play is True and detail.tmdb_id == "438631"
    assert len(detail.media_sources) == 1 and detail.media_sources[0].name == "dune.mkv"

    seasons = vyo.get_seasons("v-show")
    assert seasons is not None and seasons[0].season_number == 1

    episodes = vyo.get_season_episodes("v-show", season=1)
    assert episodes is not None and episodes[0].subtitle == "S01E01"

    search_results = vyo.search_library("沙丘", [MediaType.MOVIE.value, MediaType.TV.value], 5)
    assert len(search_results) == 1 and search_results[0].item_id == "v-1"


def test_module_wrappers_chain_and_webhook_schema_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    验证 Navidrome、Plex、TrimeMedia 模块封装、MediaServerChain 调度及 WebhookEventInfo 身份迁移。
    """
    from app.modules.navidrome import NavidromeModule
    from app.modules.plex import PlexModule
    from app.modules.plex.plex import Plex
    from app.modules.trimemedia.module import TrimeMediaModule
    from app.schemas.mediaserver import MediaServerPlayItem, WebhookEventInfo
    from app.schemas.types import MediaSource, MediaType

    # NavidromeModule 浏览、筛选项、详情、曲目与定向搜索
    nav_item = MediaServerBrowseItem(item_id="al-1", title="专辑A", item_type="MusicAlbum")
    nav_page = MediaServerBrowsePage(items=[nav_item], total_count=1, start_index=0, limit=10, has_more=False)
    nav_detail = MediaServerItemDetail(item_id="al-1", title="专辑A", item_type="MusicAlbum")
    nav_play = MediaServerPlayItem(id="tr-1", item_id="tr-1", title="歌曲A", type=MediaType.MUSIC.value)
    nav_service = SimpleNamespace(
        browse_items=Mock(return_value=nav_page),
        get_filter_options=Mock(return_value=MediaServerFilterOptions(genres=["Pop"])),
        get_item_detail=Mock(return_value=nav_detail),
        get_season_episodes=Mock(return_value=[nav_item]),
        search_library=Mock(return_value=[nav_play]),
    )
    nav_mod = NavidromeModule()
    monkeypatch.setattr(nav_mod, "get_instance", lambda name=None: nav_service if name == "nav" else None)
    monkeypatch.setattr(nav_mod, "get_instances", lambda: {"nav": nav_service})

    assert nav_mod.mediaserver_browse_items("missing") is None
    assert nav_mod.mediaserver_browse_items("nav").items[0].server == "nav"
    assert nav_mod.mediaserver_filter_options("nav").genres == ["Pop"]
    assert nav_mod.mediaserver_item_detail("missing", "al-1") is None
    assert nav_mod.mediaserver_item_detail("nav", "al-1").server == "nav"
    assert nav_mod.mediaserver_season_episodes("missing", "al-1") is None
    assert nav_mod.mediaserver_season_episodes("nav", "al-1")[0].server == "nav"
    assert nav_mod.mediaserver_search("歌", [MediaType.MOVIE.value], 5) == []
    assert nav_mod.mediaserver_search("歌", [MediaType.MUSIC.value], 5, server="nav", library_id="1")[0].server == "nav"

    # PlexModule / Plex 按库搜索、流媒体源透传与音乐轨道搜索
    plex_play = MediaServerPlayItem(id="10", item_id="10", title="电影A", type=MediaType.MOVIE.value)
    plex_service = SimpleNamespace(
        search_library_items=Mock(return_value=[plex_play]),
        get_stream_url=Mock(return_value="http://plex.test/part/1"),
    )
    plex_mod = PlexModule()
    monkeypatch.setattr(plex_mod, "get_instance", lambda name=None: plex_service if name == "plex" else None)
    monkeypatch.setattr(plex_mod, "get_instances", lambda: {"plex": plex_service})
    assert plex_mod.mediaserver_search("电", [MediaType.MOVIE.value], 5, server="plex", library_id="2")[0].server == "plex"
    assert plex_mod.mediaserver_stream_url("plex", "10", "series") is None
    assert plex_mod.mediaserver_stream_url("plex", "10", "movie", media_source_id="p1") == "http://plex.test/part/1"

    plex_client = Plex.__new__(Plex)
    plex_client._host = "http://plex.test"
    plex_client._token = "tok"
    track_obj = SimpleNamespace(
        type="track",
        ratingKey="t-1",
        title="曲目一",
        grandparentTitle="歌手",
        parentTitle="专辑",
        thumbUrl="/thumb/1",
        year=2022,
    )
    music_lib = SimpleNamespace(type="artist")
    plex_client._plex = SimpleNamespace(
        library=SimpleNamespace(
            sectionByID=Mock(return_value=SimpleNamespace(search=Mock(return_value=[]))),
            sections=Mock(return_value=[music_lib]),
        )
    )
    plex_client._disable_auto_reload = Mock()
    plex_client._search_music_library = Mock(return_value=[track_obj])
    music_found = plex_client.search_library_items("曲", [MediaType.MOVIE.value, MediaType.MUSIC.value], 5, library_id="2")
    assert len(music_found) == 1 and "X-Plex-Token=tok" in (music_found[0].image or "")

    # TrimeMediaModule 定向实例搜索
    trime_service = SimpleNamespace(search_library=Mock(return_value=[plex_play]))
    trime_mod = TrimeMediaModule()
    monkeypatch.setattr(trime_mod, "get_instance", lambda name=None: trime_service if name == "fn" else None)
    monkeypatch.setattr(trime_mod, "get_instances", lambda: {"fn": trime_service})
    assert trime_mod.mediaserver_search("电", [MediaType.MOVIE.value], 5, server="fn", library_id="3")[0].server == "fn"

    # MediaServerChain 新增调度与 WebhookEventInfo 兼容身份验证
    chain = MediaServerChain()
    called: list[str] = []

    def fake_run_module(method: str, **_kwargs: Any) -> Any:
        """
        记录被调度的模块方法名并返回对应最小结果。
        """
        called.append(method)
        if method == "mediaserver_report_progress":
            return True
        if method == "mediaserver_subtitle_url":
            return "http://emby.test/sub.vtt"
        return []

    monkeypatch.setattr(chain, "run_module", fake_run_module)
    assert chain.browse_items(server="emby") == []
    assert chain.filter_options(server="emby") == []
    assert chain.item_detail(server="emby", item_id="1") == []
    assert chain.seasons(server="emby", item_id="1") == []
    assert chain.season_episodes(server="emby", item_id="1") == []
    assert chain.report_progress(server="emby", item_id="1", position_ms=1000) is True
    assert chain.get_subtitle_url(server="emby", item_id="1", subtitle_id="ms:1") == "http://emby.test/sub.vtt"
    assert "mediaserver_browse_items" in called and "mediaserver_subtitle_url" in called

    legacy_evt = WebhookEventInfo.model_validate({"tmdb_id": 12345})
    assert legacy_evt.media_source == MediaSource.TMDB and legacy_evt.tmdb_id == "12345"
    legacy_evt.tmdb_id = 67890
    assert legacy_evt.media_id == "67890"
    legacy_evt.tmdb_id = None
    assert legacy_evt.media_source is None and legacy_evt.tmdb_id is None
    with pytest.raises(ValueError):
        WebhookEventInfo.model_validate({"media_source": MediaSource.TMDB})
    with pytest.raises(ValueError):
        WebhookEventInfo.model_validate({"media_source": MediaSource.TMDB, "media_id": "0"})


def test_mediaserver_browse_endpoints_error_and_subtitle_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    验证媒体服务器浏览、详情、季集与服务端字幕代理接口的异常状态码处理。
    """
    app = FastAPI()
    app.include_router(mediaserver.router, prefix="/mediaserver")
    app.dependency_overrides[mediaserver.verify_token] = lambda: SimpleNamespace(sub="1", username="alice")
    client = TestClient(app)

    monkeypatch.setattr(MediaServerChain, "browse_items", lambda self, **kwargs: None)
    monkeypatch.setattr(MediaServerChain, "item_detail", lambda self, **kwargs: None)
    monkeypatch.setattr(MediaServerChain, "seasons", lambda self, **kwargs: None)
    monkeypatch.setattr(MediaServerChain, "season_episodes", lambda self, **kwargs: None)
    monkeypatch.setattr(MediaServerChain, "get_subtitle_url", lambda self, **kwargs: None)
    monkeypatch.setattr(
        mediaserver,
        "_resolve_stream_item",
        lambda itemid, user_id, server=None: ("emby", SimpleNamespace(item_id=itemid), None),
    )

    assert client.get("/mediaserver/browse", params={"server": "emby"}).status_code == 502
    assert client.get("/mediaserver/detail", params={"itemid": "missing", "server": "emby"}).status_code == 404
    assert client.get("/mediaserver/seasons", params={"itemid": "s1", "server": "emby"}).status_code == 502
    assert client.get("/mediaserver/season-episodes", params={"itemid": "s1", "server": "emby"}).status_code == 502
    assert client.get(
        "/mediaserver/stream-subtitle",
        params={"itemid": "m1", "subtitle_id": "ms:1", "server": "emby"},
    ).status_code == 404
    assert client.get(
        "/mediaserver/stream-subtitle",
        params={"itemid": "m1", "subtitle_id": "local.srt", "server": "emby"},
    ).status_code == 404

    # 覆盖服务端托管字幕代理成功路径、省略 server 的详情遍历与无配置播放返回
    from fastapi.responses import StreamingResponse

    monkeypatch.setattr(
        MediaServerChain,
        "get_subtitle_url",
        lambda self, **kwargs: "http://emby.test/sub.vtt",
    )
    monkeypatch.setattr(mediaserver, "_configured_stream_hosts", lambda *_a, **_kw: {"emby.test"})
    monkeypatch.setattr(mediaserver, "_validate_stream_url", lambda url, **_kw: url)
    monkeypatch.setattr(
        mediaserver,
        "_remote_stream_response",
        lambda *_a, **_kw: StreamingResponse(iter([b"WEBVTT"]), media_type="text/vtt"),
    )
    sub_resp = client.get(
        "/mediaserver/stream-subtitle",
        params={"itemid": "m1", "subtitle_id": "ms:1", "server": "emby"},
    )
    assert sub_resp.status_code == 200 and sub_resp.text == "WEBVTT"

    monkeypatch.setattr(mediaserver, "get_mediaserver_configs", lambda: [])
    play_empty = client.get("/mediaserver/play/m1")
    assert play_empty.status_code == 200 and play_empty.json()["success"] is False

    monkeypatch.setattr(
        mediaserver,
        "get_mediaserver_configs",
        lambda: [SimpleNamespace(name=""), SimpleNamespace(name="emby")],
    )
    monkeypatch.setattr(
        MediaServerChain,
        "iteminfo",
        lambda self, **kwargs: SimpleNamespace(item_id="m1", server_id="s1", server="emby"),
    )
    monkeypatch.setattr(MediaServerChain, "get_play_url", lambda self, **kwargs: "http://emby.test/web/m1")
    play_ok = client.get("/mediaserver/play/m1")
    assert play_ok.status_code == 200 and play_ok.json()["data"]["url"] == "http://emby.test/web/m1"

    monkeypatch.setattr(MediaServerChain, "get_play_url", lambda self, **kwargs: None)
    play_miss = client.get("/mediaserver/play/m1")
    assert play_miss.status_code == 200 and play_miss.json()["success"] is False

    monkeypatch.setattr(
        MediaServerChain,
        "item_detail",
        lambda self, **kwargs: MediaServerItemDetail(item_id="m1", title="详情", item_type="Movie"),
    )
    detail_auto = client.get("/mediaserver/detail", params={"itemid": "m1"})
    assert detail_auto.status_code == 200 and detail_auto.json()["data"]["item_id"] == "m1"

