"""音乐媒体服务器统一匹配契约测试。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import schemas
from app.domain.context import MusicInfo
from app.application.mediaserver import MusicMediaServerHelper
from app.modules.emby import EmbyModule
from app.modules.emby.emby import Emby
from app.modules.jellyfin import JellyfinModule
from app.modules.jellyfin.jellyfin import Jellyfin
from app.modules.plex import PlexModule
from app.modules.plex.plex import Plex
from app.modules.trimemedia import TrimeMediaModule  # pylint: disable=no-name-in-module
from app.modules.ugreen import UgreenModule  # pylint: disable=no-name-in-module
from app.modules.zspace import ZSpaceModule
from app.modules.zspace.zspace import ZSpace


def _recording() -> MusicInfo:
    """构造媒体库匹配使用的单曲目标。"""
    return MusicInfo(
        media_source="musicbrainz",
        media_id="recording-1",
        music_type="recording",
        title="晴天",
        artists=["周杰伦"],
        album="叶惠美",
    )


def _album() -> MusicInfo:
    """构造媒体库完整性匹配使用的专辑目标。"""
    return MusicInfo(
        media_source="musicbrainz",
        media_id="release-group-1",
        music_type="album",
        title="叶惠美",
        album="叶惠美",
        artists=["周杰伦"],
        total_tracks=11,
    )


def test_music_media_server_helper_requires_exact_recording_and_artist():
    """同名异艺人的单曲不得误判为已入库，目标艺术家和曲名均匹配才算存在。"""
    wrong_artist = schemas.MediaServerItem(
        item_id="wrong",
        title="晴天",
        note={"artist": "其他艺人", "album": "同名专辑"},
    )
    exact = schemas.MediaServerItem(
        item_id="recording-1",
        title="晴天",
        note={"artist": "周杰伦", "album": "叶惠美"},
    )

    assert MusicMediaServerHelper.item_matches(_recording(), wrong_artist) is False
    assert MusicMediaServerHelper.item_matches(_recording(), exact) is True


def test_music_media_server_helper_requires_complete_album_track_count():
    """专辑名称和艺术家相同但曲目不足时，仍必须保持订阅等待整专。"""
    incomplete = schemas.MediaServerItem(
        item_id="album-10",
        title="叶惠美",
        note={"artist": "周杰伦", "album": "叶惠美", "song_count": 10},
    )
    complete = schemas.MediaServerItem(
        item_id="album-11",
        title="叶惠美",
        note={"artist": "周杰伦", "album": "叶惠美", "song_count": 11},
    )

    assert MusicMediaServerHelper.item_matches(_album(), incomplete) is False
    assert MusicMediaServerHelper.item_matches(_album(), complete) is True


def test_music_media_server_helper_normalizes_emby_music_fields():
    """Emby 系音乐字段应统一提取艺术家、专辑和整专曲目数。"""
    note = MusicMediaServerHelper.build_note({
        "Type": "MusicAlbum",
        "Name": "叶惠美",
        "AlbumArtists": [{"Name": "周杰伦"}],
        "ChildCount": 11,
    })

    assert note == {
        "artist": "周杰伦",
        "artists": ["周杰伦"],
        "album": "叶惠美",
        "song_count": 11,
    }


@pytest.mark.parametrize(
    ("formatter", "server", "server_id"),
    [
        (Emby._Emby__format_item_info, "emby", "server-1"),
        (Jellyfin._Jellyfin__format_item_info, "jellyfin", None),
        (ZSpace._ZSpace__format_item_info, "zspace", None),
    ],
)
def test_emby_family_clients_preserve_item_contract(formatter, server, server_id):
    """Emby 系客户端复用统一转换时必须保留服务身份及音乐匹配字段。"""
    item = formatter({
        "Id": "album-1",
        "ServerId": "server-1",
        "ParentId": "library-1",
        "Type": "MusicAlbum",
        "Name": "叶惠美",
        "AlbumArtists": [{"Name": "周杰伦"}],
        "ChildCount": 11,
        "ProviderIds": {"Tmdb": "123"},
        "UserData": {
            "Played": False,
            "PlaybackPositionTicks": 1,
            "LastPlayedDate": "2026-08-24T12:34:56.123456Z",
            "PlayCount": 2,
            "PlayedPercentage": 25.5,
        },
    })

    assert item is not None
    assert item.server == server
    assert item.server_id == server_id
    assert item.library == "library-1"
    assert item.media_source == "themoviedb"
    assert item.media_id == "123"
    assert item.user_state is not None
    assert item.user_state.resume is True
    assert item.user_state.last_played_date == "2026-08-24 12:34:56"
    assert item.note == {
        "artist": "周杰伦",
        "artists": ["周杰伦"],
        "album": "叶惠美",
        "song_count": 11,
    }


def test_plex_music_client_preserves_album_artist_and_track_count():
    """Plex 专辑搜索结果应保留父级艺术家和 leafCount，供整专完整性判断。"""
    album_item = SimpleNamespace(
        type="album",
        title="叶惠美",
        parentTitle="周杰伦",
        leafCount=11,
        ratingKey="album-1",
        key="/library/metadata/album-1",
        year=2003,
        file=None,
    )
    library = SimpleNamespace(
        type="music",
        key="music-library",
        search=Mock(return_value=[album_item]),
    )
    client = object.__new__(Plex)
    client._plex = SimpleNamespace(
        library=SimpleNamespace(sections=lambda: [library])
    )

    results = client.get_music(album="叶惠美", artist="周杰伦")

    assert MusicMediaServerHelper.find_match(_album(), results).item_id == "album-1"
    library.search.assert_called_once_with(title="叶惠美")


@pytest.mark.parametrize(
    ("module_class", "server_type"),
    [
        (EmbyModule, "emby"),
        (JellyfinModule, "jellyfin"),
        (PlexModule, "plex"),
        (TrimeMediaModule, "trimemedia"),
        (UgreenModule, "ugreen"),
        (ZSpaceModule, "zspace"),
    ],
)
def test_music_media_server_modules_ignore_fuzzy_result_and_select_exact_match(
        monkeypatch,
        module_class,
        server_type,
):
    """所有通用媒体服务器模块都必须在模糊搜索后应用统一音乐精确匹配。"""
    service = Mock()
    service.get_music.return_value = [
        schemas.MediaServerItem(
            item_id="wrong",
            title="晴天 Live",
            note={"artist": "周杰伦"},
        ),
        schemas.MediaServerItem(
            item_id="recording-1",
            title="晴天",
            note={"artist": "周杰伦", "album": "叶惠美"},
        ),
    ]
    module = module_class()
    monkeypatch.setattr(module, "get_instances", lambda: {"music": service})

    exists = module.media_exists(_recording())

    assert exists is not None
    assert exists.server_type == server_type
    assert exists.itemid == "recording-1"
    service.get_music.assert_called_once_with(
        title="晴天",
        artist="周杰伦",
        album=None,
    )


@pytest.mark.parametrize(
    ("module_class", "server_type"),
    [
        (EmbyModule, "emby"),
        (JellyfinModule, "jellyfin"),
        (PlexModule, "plex"),
        (TrimeMediaModule, "trimemedia"),
        (UgreenModule, "ugreen"),
        (ZSpaceModule, "zspace"),
    ],
)
def test_music_media_server_modules_require_complete_album(
        monkeypatch,
        module_class,
        server_type,
):
    """所有通用媒体服务器都只能用完整专辑条目结束整专订阅。"""
    service = Mock()
    service.get_music.return_value = [
        schemas.MediaServerItem(
            item_id="album-10",
            title="叶惠美",
            note={"artist": "周杰伦", "album": "叶惠美", "song_count": 10},
        ),
        schemas.MediaServerItem(
            item_id="album-11",
            title="叶惠美",
            note={"artist": "周杰伦", "album": "叶惠美", "song_count": 11},
        ),
    ]
    module = module_class()
    monkeypatch.setattr(module, "get_instances", lambda: {"music": service})

    exists = module.media_exists(_album())

    assert exists is not None
    assert exists.server_type == server_type
    assert exists.itemid == "album-11"
    service.get_music.assert_called_once_with(
        title=None,
        artist="周杰伦",
        album="叶惠美",
    )
