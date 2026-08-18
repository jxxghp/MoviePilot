"""TheAudioDB 与豆瓣音乐识别源的标准化和路由测试。"""

from unittest.mock import AsyncMock, Mock, call

import pytest

from app.application.orchestration.media import MediaChain
from app.application.orchestration.scraping import ScrapingChain
from app.domain.context import MUSIC_ENTITY_ALBUM, MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.modules.douban import DoubanModule
from app.modules.theaudiodb import TheAudioDbModule
from app.schemas.types import MediaSource, MediaType


def test_theaudiodb_module_maps_track_and_album(monkeypatch):
    """TheAudioDB 原生响应应保留来源 ID，并换算毫秒时长。"""
    module = TheAudioDbModule()
    request = Mock(side_effect=[
        {
            "track": [{
                "idTrack": "32793500",
                "strTrack": "Yellow",
                "strArtist": "Coldplay",
                "idArtist": "111239",
                "idAlbum": "2109619",
                "strAlbum": "Parachutes",
                "intTrackNumber": "5",
                "intDuration": "269000",
                "strTrackThumb": "https://www.theaudiodb.com/images/track.jpg",
            }]
        },
        {"album": []},
        {"artists": []},
    ])
    monkeypatch.setattr(module, "_request_json", request)

    results = module.search_music(
        MetaMusic(title="Yellow", artists=["Coldplay"]),
        media_source=MediaSource.TheAudioDB,
    )

    assert results and len(results) == 1
    assert results[0].media_source == "theaudiodb"
    assert results[0].media_id == "32793500"
    assert results[0].album_id == "2109619"
    assert results[0].duration == 269


def test_theaudiodb_module_ignores_other_sources(monkeypatch):
    """显式选择其它来源时 TheAudioDB 不得发起请求或占用识别结果。"""
    module = TheAudioDbModule()
    request = Mock()
    monkeypatch.setattr(module, "_request_json", request)

    searched = module.search_music(
        MetaMusic(title="Yellow"), media_source=MediaSource.MusicBrainz
    )
    recognized = module.recognize_media(
        meta=MetaMusic(title="Yellow"),
        media_source="doubanmusic",
    )

    assert searched is None
    assert recognized is None
    request.assert_not_called()


def test_theaudiodb_title_only_search_skips_incomplete_track_and_album_requests(monkeypatch):
    """缺少艺术家时只搜索艺术家，避免请求会返回空正文的曲目和专辑接口。"""
    module = TheAudioDbModule()
    request = Mock(return_value={"artists": []})
    monkeypatch.setattr(module, "_request_json", request)

    results = module.search_music(
        MetaMusic(title="Yellow"),
        media_source=MediaSource.TheAudioDB,
    )

    assert results == []
    request.assert_called_once_with("search.php", {"s": "Yellow"})


def test_theaudiodb_search_uses_album_artist_for_required_artist_parameter(monkeypatch):
    """音轨没有独立艺术家时应使用专辑艺术家补齐 TheAudioDB 必填参数。"""
    module = TheAudioDbModule()
    request = Mock(side_effect=[{"track": []}, {"album": []}])
    monkeypatch.setattr(module, "_request_json", request)
    meta = MetaMusic(
        title="Yellow",
        album="Parachutes",
        album_artist="Coldplay",
    )

    assert module._search_tracks(meta) == []
    assert module._search_albums(meta) == []
    assert request.call_args_list == [
        call("searchtrack.php", {"t": "Yellow", "s": "Coldplay"}),
        call("searchalbum.php", {"a": "Parachutes", "s": "Coldplay"}),
    ]


def test_theaudiodb_empty_response_is_soft_failure_and_closes_response(monkeypatch):
    """TheAudioDB 返回 HTTP 200 空正文时应软失败并及时关闭同步连接。"""
    response = Mock(
        status_code=200,
        content=b"",
        text="",
        headers={"Content-Type": "text/html; charset=UTF-8"},
    )
    get_res = Mock(return_value=response)
    monkeypatch.setattr("app.modules.theaudiodb.RequestUtils.get_res", get_res)
    TheAudioDbModule._request_json.cache_clear()

    result = TheAudioDbModule._request_json("searchtrack.php", {"t": "Yellow"})

    assert result is None
    response.json.assert_not_called()
    response.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_theaudiodb_async_empty_response_is_soft_failure_and_closes_response(monkeypatch):
    """TheAudioDB 返回 HTTP 200 空正文时应软失败并及时关闭异步连接。"""
    response = Mock(
        status_code=200,
        content=b"",
        text="",
        headers={"Content-Type": "text/html; charset=UTF-8"},
    )
    response.aclose = AsyncMock()
    get_res = AsyncMock(return_value=response)
    monkeypatch.setattr("app.modules.theaudiodb.AsyncRequestUtils.get_res", get_res)
    await TheAudioDbModule._async_request_json.cache_clear()

    result = await TheAudioDbModule._async_request_json(
        "searchalbum.php",
        {"a": "Parachutes"},
    )

    assert result is None
    response.json.assert_not_called()
    response.aclose.assert_awaited_once_with()


def test_theaudiodb_detail_respects_requested_entity(monkeypatch):
    """TheAudioDB 数值 ID 必须按显式实体调用对应接口，不能跨表探测。"""
    module = TheAudioDbModule()
    request = Mock(return_value={"album": []})
    monkeypatch.setattr(module, "_request_json", request)

    result = module.recognize_music(
        "theaudiodb",
        "2109619",
        music_type=MUSIC_ENTITY_ALBUM,
    )

    assert result is None
    request.assert_called_once_with("album.php", {"m": "2109619"})


def test_theaudiodb_album_related_excludes_current_album(monkeypatch):
    """TheAudioDB 关联专辑应按当前专辑艺术家查询并排除自身。"""
    module = TheAudioDbModule()
    request = Mock(side_effect=[
        {"album": [{"idAlbum": "album-1", "idArtist": "artist-1"}]},
        {
            "album": [
                {"idAlbum": "album-1", "strAlbum": "Current"},
                {"idAlbum": "album-2", "strAlbum": "Related"},
            ]
        },
    ])
    monkeypatch.setattr(module, "_request_json", request)

    results = module.music_album_related("theaudiodb", "album-1", count=10)

    assert results and [item.media_id for item in results] == ["album-2"]
    assert request.call_args_list[1].args == ("album.php", {"i": "artist-1"})


def test_douban_detail_rejects_album_id_as_recording(monkeypatch):
    """豆瓣单曲使用专辑加曲序复合 ID，纯专辑 ID 不能作为 Recording。"""
    module = DoubanModule()
    module.doubanapi = Mock()

    result = module.recognize_music(
        "doubanmusic",
        "1401853",
        music_type="recording",
    )

    assert result is None
    module.doubanapi.music_detail.assert_not_called()


def test_douban_music_search_and_album_mapping(monkeypatch):
    """豆瓣模块应把音乐条目映射为专辑，并生成可用于曲目识别的复合 ID。"""
    module = DoubanModule()
    module.doubanapi = Mock()
    module.doubanapi.music_search.return_value = {
        "items": [{
            "target_type": "music",
            "target": {
                "id": "1401853",
                "title": "范特西",
                # 豆瓣真实搜索响应只在卡片副标题提供艺术家和年份。
                "card_subtitle": "周杰伦 / 2001",
            },
        }]
    }
    module.doubanapi.music_detail.return_value = {
        "id": "1401853",
        "title": "范特西",
        "singer": [{"id": "1050015", "name": "周杰伦"}],
        "pubdate": ["2001-09-14"],
        "media": ["CD"],
        "publisher": ["阿尔发音乐"],
        "songs": [
            {
                "title": "爱在西元前",
                "track_number": 1,
                "artist_names": ["周杰伦"],
                "duration": 221,
            },
            {
                "title": "爸我回来了",
                "track_number": 2,
                "artist_names": ["周杰伦"],
                "cover_url": "https://img.example/track.jpg",
            },
        ],
        "rating": {"average": "9.4", "numRaters": "12345"},
    }

    results = module.search_music(
        MetaMusic(title="范特西", artists=["周杰伦"]),
        media_source=MediaSource.DoubanMusic,
    )
    album = module.music_album("doubanmusic", "1401853")

    assert results and results[0].media_source == "doubanmusic"
    assert results[0].music_type == MUSIC_ENTITY_ALBUM
    assert results[0].artists == ["周杰伦"]
    assert results[0].artist == "周杰伦"
    assert results[0].album_artist == "周杰伦"
    assert album and album.media_source == "doubanmusic"
    assert album.year == 2001
    assert album.artists == ["周杰伦"]
    assert album.album_type == "CD"
    assert [track.media_id for track in album.tracks] == ["1401853:1", "1401853:2"]
    assert album.tracks[0].title == "爱在西元前"
    assert album.tracks[0].duration == 221
    assert album.tracks[1].cover_url == "https://img.example/track.jpg"


@pytest.mark.parametrize(
    ("subtitle", "expected"),
    [
        ("Mr Hudson Vic Mensa / 2017", ["Mr Hudson Vic Mensa"]),
        ("周杰伦 / 2001 / CD / 阿尔发音乐", ["周杰伦"]),
        ("周杰伦", ["周杰伦"]),
        ("2007", []),
    ],
)
def test_douban_music_card_subtitle_artist_fallback(subtitle, expected):
    """搜索副标题回退应保留完整署名，且不能把纯年份误判为艺术家。"""
    assert DoubanModule._douban_music_search_artists({"card_subtitle": subtitle}) == expected


def test_douban_music_search_prefers_structured_artists_over_card_subtitle():
    """搜索响应已有结构化艺术家时不能被卡片副标题覆盖。"""
    assert DoubanModule._douban_music_search_artists({
        "artists": [{"name": "结构化艺术家"}],
        "card_subtitle": "回退艺术家 / 2024",
    }) == ["结构化艺术家"]


def test_douban_music_discover_and_related_accept_collection_wrappers(monkeypatch):
    """豆瓣新碟榜与相关推荐应兼容 subject 包装并保留专辑身份。"""
    module = DoubanModule()
    module.doubanapi = Mock()
    wrapped_item = {
        "type": "subject_collection_item",
        "subject": {
            "id": "1401853",
            "type": "music",
            "title": "范特西",
            "artists": [{"name": "周杰伦"}],
            "cover": {"url": "https://img.example/fantasy.jpg"},
        },
    }
    module.doubanapi.music_chart.return_value = {
        "subject_collection_items": [wrapped_item]
    }
    module.doubanapi.music_recommendations.return_value = [wrapped_item["subject"]]

    discovered = module.music_discover("doubanmusic", page=1, count=10)
    related = module.music_album_related("doubanmusic", "album-1", count=6)

    assert discovered and discovered[0].media_id == "1401853"
    assert discovered[0].cover_url == "https://img.example/fantasy.jpg"
    assert related and related[0].media_source == "doubanmusic"
    module.doubanapi.music_chart.assert_called_once_with()
    module.doubanapi.music_recommendations.assert_called_once_with(
        subject_id="album-1",
        start=0,
        count=6,
    )


def test_douban_music_tag_discover_intersects_official_tag_results():
    """豆瓣音乐组合筛选应按原生条目 ID 求交集，并保持主风格排序。"""
    module = DoubanModule()
    module.doubanapi = Mock()
    module.doubanapi.music_tag.side_effect = [
        {"items": [
            {"id": "1", "type": "music", "title": "流行华语一"},
            {"id": "2", "type": "music", "title": "仅流行"},
            {"id": "3", "type": "music", "title": "流行华语二"},
        ]},
        {"items": [
            {"id": "3", "type": "music", "title": "流行华语二"},
            {"id": "1", "type": "music", "title": "流行华语一"},
        ]},
    ]

    results = module.music_discover(
        "doubanmusic", mode="tag", tags="流行,华语", sort="S", count=20
    )

    assert [item.media_id for item in results] == ["1", "3"]
    assert module.doubanapi.music_tag.call_args_list == [
        call(tag="流行", start=0, count=100, sort="S"),
        call(tag="华语", start=0, count=100, sort="S"),
    ]


def test_douban_music_recognize_expands_album_to_matching_track(monkeypatch):
    """自动文件识别有专辑线索时，豆瓣应返回专辑内音轨而不是专辑实体。"""
    module = DoubanModule()
    module.doubanapi = Mock()
    module.doubanapi.music_search.return_value = {
        "items": [{
            "target_type": "music",
            "target": {
                "id": "1401853",
                "title": "范特西",
                "artists": [{"name": "周杰伦"}],
            },
        }]
    }
    module.doubanapi.music_detail.return_value = {
        "id": "1401853",
        "title": "范特西",
        "singer": [{"name": "周杰伦"}],
        "songs": [
            {"title": "爱在西元前", "track_number": 1},
            {"title": "爸我回来了", "track_number": 2},
        ],
    }

    result = module.recognize_media(
        meta=MetaMusic(
            title="爸我回来了",
            artists=["周杰伦"],
            album="范特西",
            track_number=2,
        ),
        media_source="doubanmusic",
    )

    assert result and result.music_type == "recording"
    assert result.media_id == "1401853:2"
    assert result.album == "范特西"


def test_douban_music_mapping_keeps_legacy_attrs_tracks():
    """豆瓣旧响应中的 attrs.singer 与 attrs.tracks 仍应保持兼容。"""
    album = DoubanModule._douban_music_to_album({
        "id": "1401853",
        "title": "范特西",
        "attrs": {
            "singer": ["周杰伦"],
            "tracks": ["01. 爱在西元前", "02. 爸我回来了"],
        },
    })

    assert album and album.artists == ["周杰伦"]
    assert [track.title for track in album.tracks] == ["爱在西元前", "爸我回来了"]


@pytest.mark.asyncio
async def test_douban_music_async_recognize_maps_real_songs(monkeypatch):
    """异步豆瓣自动识别应从真实 songs 字段返回具体音轨。"""
    module = DoubanModule()
    module.doubanapi = Mock()
    module.doubanapi.async_music_search = AsyncMock(return_value={
        "items": [{
            "target_type": "music",
            "target": {"id": "1401853", "title": "范特西"},
        }],
    })
    module.doubanapi.async_music_detail = AsyncMock(return_value={
        "id": "1401853",
        "title": "范特西",
        "singer": [{"name": "周杰伦"}],
        "songs": [
            {"title": "爱在西元前", "track_number": 1},
            {"title": "爸我回来了", "track_number": 2},
        ],
    })

    result = await module.async_recognize_media(
        meta=MetaMusic(
            title="爱在西元前",
            artists=["周杰伦"],
            album="范特西",
        ),
        media_source="doubanmusic",
    )

    assert result and result.media_id == "1401853:1"
    assert result.title == "爱在西元前"
    assert result.artists == ["周杰伦"]


@pytest.mark.asyncio
async def test_douban_recognize_media_routes_only_douban_music(monkeypatch):
    """豆瓣音乐使用独立数据源，不能与影视豆瓣入口或其它音乐源串线。"""
    module = DoubanModule()
    expected = MusicInfo(media_source="doubanmusic", media_id="1401853", title="范特西")
    recognize_music = Mock(return_value=expected)
    recognize_video = Mock()
    async_recognize_music = AsyncMock(return_value=expected)
    monkeypatch.setattr(module, "_recognize_music_media", recognize_music)
    monkeypatch.setattr(module, "_recognize_media_core", recognize_video)
    monkeypatch.setattr(module, "_async_recognize_music_media", async_recognize_music)

    recognized = module.recognize_media(
        meta=MetaMusic(title="范特西"),
        media_source="doubanmusic",
        media_id="1401853",
    )
    ignored = module.recognize_media(
        meta=MetaMusic(title="范特西"),
        media_source="theaudiodb",
    )
    async_recognized = await module.async_recognize_media(
        mtype=MediaType.MUSIC,
        media_source="doubanmusic",
        media_id="1401853",
    )

    assert recognized is expected
    assert ignored is None
    assert async_recognized is expected
    recognize_video.assert_not_called()


@pytest.mark.asyncio
async def test_media_chain_defaults_to_musicbrainz_and_forwards_explicit_source(monkeypatch):
    """音乐搜索默认 MusicBrainz，手动选择时原样转发其它音乐源。"""
    chain = MediaChain()
    musicbrainz = Mock()
    musicbrainz.search_music.return_value = []
    douban = Mock()
    douban.async_search_music = AsyncMock(return_value=[])
    monkeypatch.setattr(
        chain,
        "_music_source_chain",
        Mock(side_effect=[musicbrainz, douban]),
    )

    chain.search_music("Yellow")
    await chain.async_search_music(
        "范特西", media_source=MediaSource.DoubanMusic
    )

    assert chain._music_source_chain.call_args_list[0].args[0] == "musicbrainz"
    assert chain._music_source_chain.call_args_list[1].args[0] == "doubanmusic"
    assert musicbrainz.search_music.call_args.args[0].title == "Yellow"
    assert douban.async_search_music.await_args.args[0].title == "范特西"


@pytest.mark.asyncio
async def test_media_chain_aggregates_music_sources_and_isolates_source_failure(monkeypatch):
    """多音乐来源应按选择顺序聚合，单一来源失败不能清空其它来源结果。"""
    chain = MediaChain()
    musicbrainz = Mock()
    musicbrainz.async_search_music = AsyncMock(return_value=[
        MusicInfo(
            media_source="musicbrainz",
            media_id="recording-1",
            title="Yellow",
        )
    ])
    theaudiodb = Mock()
    theaudiodb.async_search_music = AsyncMock(side_effect=ValueError("empty response"))
    douban = Mock()
    douban.async_search_music = AsyncMock(return_value=[
        MusicInfo(
            media_source="doubanmusic",
            media_id="album-1",
            music_type="album",
            title="Yellow",
        )
    ])
    source_chains = {
        "musicbrainz": musicbrainz,
        "acme.music": Mock(
            async_search_music=AsyncMock(return_value=[
                MusicInfo(
                    media_source="acme.music",
                    media_id="plugin-1",
                    title="Yellow",
                )
            ])
        ),
        "theaudiodb": theaudiodb,
        "doubanmusic": douban,
    }
    select_chain = Mock(side_effect=lambda source: source_chains[str(source)])
    monkeypatch.setattr(chain, "_music_source_chain", select_chain)

    results = await chain.async_search_music(
        "Yellow",
        limit=30,
        media_source=(
            MediaSource.MusicBrainz,
            MediaSource("acme.music"),
            MediaSource.TheAudioDB,
            MediaSource.DoubanMusic,
            MediaSource.MusicBrainz,
        ),
    )

    assert [(str(item.media_source), item.media_id) for item in results] == [
        ("musicbrainz", "recording-1"),
        ("acme.music", "plugin-1"),
        ("doubanmusic", "album-1"),
    ]
    assert [str(item.args[0]) for item in select_chain.call_args_list] == [
        "musicbrainz",
        "acme.music",
        "theaudiodb",
        "doubanmusic",
    ]
    theaudiodb.async_search_music.assert_awaited_once()


def test_music_scrape_resolves_with_selected_source(tmp_path, monkeypatch):
    """无显式 ID 的音乐刮削应使用用户选择的来源识别本地音频。"""
    path = tmp_path / "Yellow.flac"
    path.write_bytes(b"audio")
    expected = MusicInfo(media_source="theaudiodb", media_id="32793500", title="Yellow")
    recognize = Mock(return_value=(MetaMusic(title="Yellow"), expected))
    media_chain = Mock()
    media_chain.recognize_music_by_path = recognize
    monkeypatch.setattr("app.application.orchestration.scraping.MediaChain", Mock(return_value=media_chain))

    result = ScrapingChain._resolve_music_scrape_info(
        path,
        mediainfo=None,
        media_source=MediaSource.TheAudioDB,
    )

    assert result is expected
    recognize.assert_called_once_with(
        path, media_source=MediaSource.TheAudioDB
    )
