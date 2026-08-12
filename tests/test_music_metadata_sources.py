"""TheAudioDB 与豆瓣音乐识别源的标准化和路由测试。"""

from unittest.mock import AsyncMock, Mock, call

import pytest

from app.chain.media import MediaChain
from app.chain.music import MusicChain
from app.core.context import MUSIC_ENTITY_ALBUM, MusicInfo
from app.core.meta import MetaMusic
from app.modules.douban import DoubanModule
from app.modules.theaudiodb import TheAudioDbModule
from app.schemas.types import MediaRecognizeType, MediaType


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
        media_source="theaudiodb",
    )

    assert results and len(results) == 1
    assert results[0].media_source == "theaudiodb"
    assert results[0].media_id == "32793500"
    assert results[0].album_id == "2109619"
    assert results[0].duration == 269
    assert module.get_subtype() == MediaRecognizeType.TheAudioDB


def test_theaudiodb_module_ignores_other_sources(monkeypatch):
    """显式选择其它来源时 TheAudioDB 不得发起请求或占用识别结果。"""
    module = TheAudioDbModule()
    request = Mock()
    monkeypatch.setattr(module, "_request_json", request)

    searched = module.search_music(MetaMusic(title="Yellow"), media_source="musicbrainz")
    recognized = module.recognize_media(
        meta=MetaMusic(title="Yellow"),
        media_source="doubanmusic",
    )

    assert searched is None
    assert recognized is None
    request.assert_not_called()


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
                "artists": [{"name": "周杰伦"}],
                "year": "2001",
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
        media_source="doubanmusic",
    )
    album = module.music_album("doubanmusic", "1401853")

    assert results and results[0].media_source == "doubanmusic"
    assert results[0].music_type == MUSIC_ENTITY_ALBUM
    assert album and album.media_source == "doubanmusic"
    assert album.year == 2001
    assert album.artists == ["周杰伦"]
    assert album.album_type == "CD"
    assert [track.media_id for track in album.tracks] == ["1401853:1", "1401853:2"]
    assert album.tracks[0].title == "爱在西元前"
    assert album.tracks[0].duration == 221
    assert album.tracks[1].cover_url == "https://img.example/track.jpg"


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
async def test_music_chain_defaults_to_musicbrainz_and_forwards_explicit_source(monkeypatch):
    """音乐搜索默认 MusicBrainz，手动选择时原样转发其它音乐源。"""
    chain = MusicChain()
    run_module = Mock(return_value=[])
    async_run_module = AsyncMock(return_value=[])
    monkeypatch.setattr(chain, "run_module", run_module)
    monkeypatch.setattr(chain, "async_run_module", async_run_module)

    chain.search("Yellow")
    await chain.async_search("范特西", media_source="doubanmusic")

    assert run_module.call_args.kwargs["media_source"] == "musicbrainz"
    assert async_run_module.await_args.kwargs["media_source"] == "doubanmusic"


def test_music_scrape_resolves_with_selected_source(tmp_path, monkeypatch):
    """无显式 ID 的音乐刮削应使用用户选择的来源识别本地音频。"""
    path = tmp_path / "Yellow.flac"
    path.write_bytes(b"audio")
    expected = MusicInfo(media_source="theaudiodb", media_id="32793500", title="Yellow")
    recognize = Mock(return_value=(MetaMusic(title="Yellow"), expected))
    monkeypatch.setattr(MediaChain, "recognize_music_by_path", recognize)

    result = MediaChain._resolve_music_scrape_info(
        path,
        mediainfo=None,
        media_source="theaudiodb",
    )

    assert result is expected
    recognize.assert_called_once_with(path, media_source="theaudiodb")
