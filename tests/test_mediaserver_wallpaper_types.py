"""媒体服务器壁纸媒体类型白名单回归测试。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.modules.emby import EmbyModule
from app.modules.jellyfin import JellyfinModule
from app.modules.navidrome import NavidromeModule
from app.modules.plex import PlexModule
from app.modules.trimemedia import api as trimemedia_api
from app.modules.trimemedia.trimemedia import TrimeMedia
from app.modules.ugreen.ugreen import Ugreen
from app.modules.zspace import ZSpaceModule
from app.schemas.mediaserver import MediaServerPlayItem
from app.schemas.types import MediaType


def _mixed_play_items() -> list[MediaServerPlayItem]:
    """构造包含影视、音乐和未知类型的最近入库条目。"""
    return [
        MediaServerPlayItem(
            id="movie",
            type=MediaType.MOVIE.value,
            image="movie.jpg",
            BackdropImageTags=["movie-tag"],
        ),
        MediaServerPlayItem(
            id="tv",
            type=MediaType.TV.value,
            image="tv.jpg",
            BackdropImageTags=["tv-tag"],
        ),
        MediaServerPlayItem(
            id="music",
            type=MediaType.MUSIC.value,
            image="music.jpg",
            BackdropImageTags=["music-tag"],
        ),
        MediaServerPlayItem(
            id="unknown",
            type=MediaType.UNKNOWN.value,
            image="unknown.jpg",
            BackdropImageTags=["unknown-tag"],
        ),
    ]


@pytest.mark.parametrize("module_class", [EmbyModule, JellyfinModule, ZSpaceModule])
def test_emby_family_wallpapers_only_use_movie_and_tv(monkeypatch, module_class):
    """Emby 系壁纸生成即使收到混合条目，也只能处理电影和电视剧。"""
    module = module_class()
    service = Mock()
    service.get_backdrop_url.side_effect = lambda item_id, image_tag, remote: f"{item_id}-{image_tag}-{remote}"
    monkeypatch.setattr(module, "get_instance", lambda _server=None: service)
    monkeypatch.setattr(module, "mediaserver_latest", lambda **_kwargs: _mixed_play_items())

    images = module.mediaserver_latest_images(server="video", remote=True)

    assert images == ["movie-movie-tag-True", "tv-tv-tag-True"]
    assert service.get_backdrop_url.call_count == 2


def test_plex_wallpapers_only_request_movie_and_tv_images(monkeypatch):
    """Plex 混合媒体库中的音乐和未知条目不得触发壁纸图片请求。"""
    module = PlexModule()
    service = Mock()
    service.get_remote_image_by_id.side_effect = lambda item_id, **_kwargs: f"https://plex.example/{item_id}.jpg"
    monkeypatch.setattr(module, "get_instance", lambda _server=None: service)
    monkeypatch.setattr(module, "mediaserver_latest", lambda **_kwargs: _mixed_play_items())

    images = module.mediaserver_latest_images(server="plex")

    assert images == [
        "https://plex.example/movie.jpg",
        "https://plex.example/tv.jpg",
    ]
    assert [call.kwargs["item_id"] for call in service.get_remote_image_by_id.call_args_list] == [
        "movie",
        "tv",
    ]


def test_navidrome_music_covers_are_not_wallpapers(monkeypatch):
    """Navidrome 只提供音乐条目，因此媒体库壁纸接口必须返回空列表。"""
    module = NavidromeModule()
    service = Mock()
    service.get_latest.return_value = [
        MediaServerPlayItem(
            id="album",
            type=MediaType.MUSIC.value,
            image="https://music.example/album.jpg",
        )
    ]
    monkeypatch.setattr(module, "get_instance", lambda _server=None: service)

    assert module.mediaserver_latest_images(server="music") == []


def test_ugreen_wallpapers_ignore_non_movie_tv_payloads(monkeypatch):
    """绿联服务端返回其它视频类型时，不得把其图片当作影视壁纸。"""
    client = Ugreen.__new__(Ugreen)
    client._api = SimpleNamespace(
        recently_updated=Mock(
            return_value={
                "video_arr": [
                    {"video_info": {"type": 1, "backdrop_path": "movie.jpg"}},
                    {"video_info": {"type": 2, "poster_path": "tv.jpg"}},
                    {"video_info": {"type": 3, "backdrop_path": "music.jpg"}},
                ]
            }
        )
    )
    client._sync_libraries = []
    monkeypatch.setattr(client, "is_authenticated", lambda: True)
    monkeypatch.setattr(client, "_Ugreen__resolve_image", lambda path: path)

    assert client.get_latest_backdrops() == ["movie.jpg", "tv.jpg"]


def test_trimemedia_wallpapers_defend_against_ignored_type_filter(monkeypatch):
    """飞牛接口即使忽略查询类型条件，返回的其它视频也不能成为壁纸。"""
    client = TrimeMedia.__new__(TrimeMedia)
    items = [
        SimpleNamespace(guid="movie", ancestor_guid="", type=trimemedia_api.Type.MOVIE),
        SimpleNamespace(guid="tv", ancestor_guid="", type=trimemedia_api.Type.TV),
        SimpleNamespace(guid="video", ancestor_guid="", type=trimemedia_api.Type.VIDEO),
    ]
    details = {
        "movie": SimpleNamespace(backdrops="/movie.jpg", posters=None, poster=None),
        "tv": SimpleNamespace(backdrops=None, posters="/tv.jpg", poster=None),
        "video": SimpleNamespace(backdrops="/video.jpg", posters=None, poster=None),
    }
    client._api = SimpleNamespace(
        host="https://trimemedia.example",
        item_list=Mock(return_value=items),
        item=Mock(side_effect=lambda guid: details[guid]),
    )
    client._playhost = None
    client._libraries = {}
    monkeypatch.setattr(client, "is_authenticated", lambda: True)

    assert client.get_latest_backdrops() == [
        "https://trimemedia.example/movie.jpg",
        "https://trimemedia.example/tv.jpg",
    ]
    assert [call.args[0] for call in client._api.item.call_args_list] == ["movie", "tv"]
