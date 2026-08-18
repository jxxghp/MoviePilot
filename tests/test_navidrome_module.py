"""Navidrome 媒体服务器模块接入测试。"""
from unittest.mock import Mock

from app import schemas
from app.runtime.extensions.module_manager import ModuleManager
from app.domain.context import MusicInfo
from app.modules.navidrome import NavidromeModule
from app.modules.navidrome.navidrome import Navidrome


def test_navidrome_module_declares_media_server_identity():
    """Navidrome 应以媒体服务器身份注册，供统一媒体服务器链调用。"""
    assert NavidromeModule.get_name() == "Navidrome"


def test_navidrome_module_has_no_system_switch():
    """Navidrome 由服务配置控制启用，不能返回无效的系统开关名。"""
    assert NavidromeModule().init_setting() is None


def test_navidrome_module_is_discovered_without_unconfigured_activation():
    """Navidrome 始终可发现，但没有启用配置时不应创建服务资源。"""
    manager = ModuleManager()

    assert "NavidromeModule" in manager.get_module_ids()
    assert manager.get_running_module("NavidromeModule") is None


def test_navidrome_module_ignores_non_music_media():
    """Navidrome 只管理音乐，影视存在性检查应交给其它媒体服务器。"""
    from app.domain.context import MediaInfo
    from app.schemas.types import MediaType

    mediainfo = MediaInfo()
    mediainfo.type = MediaType.MOVIE

    assert NavidromeModule().media_exists(mediainfo) is None


def test_navidrome_refresh_requests_incremental_scan(monkeypatch):
    """音乐入库完成后应通过 Subsonic startScan 触发 Navidrome 增量扫描。"""
    client = object.__new__(Navidrome)
    requested = []

    def fake_call(method, **kwargs):
        """记录媒体库刷新使用的 Subsonic 方法和参数。"""
        requested.append((method, kwargs))
        return {"status": "ok"}

    monkeypatch.setattr(client, "_call", fake_call)

    assert client.refresh_root_library() is True
    assert requested == [("startScan", {"fullScan": False})]


def test_navidrome_search_filters_exact_album_and_song(monkeypatch):
    """Navidrome 模糊搜索结果必须按实体名称和艺术家精确过滤。"""
    client = object.__new__(Navidrome)
    monkeypatch.setattr(
        client,
        "_call",
        lambda *_args, **_kwargs: {
            "searchResult3": {
                "album": [
                    {"id": "wrong", "name": "叶惠美 演唱会", "artist": "周杰伦", "songCount": 12},
                    {"id": "album-1", "name": "叶惠美", "artist": "周杰伦", "songCount": 11},
                ],
                "song": [
                    {"id": "song-wrong", "title": "晴天 Live", "artist": "周杰伦"},
                    {"id": "song-1", "title": "晴天", "artist": "周杰伦"},
                ],
            }
        },
    )

    albums = client.search_music(album="叶惠美", artist="周杰伦")
    songs = client.search_music(title="晴天", artist="周杰伦")

    assert [item.item_id for item in albums] == ["album-1"]
    assert albums[0].note["song_count"] == 11
    assert [item.item_id for item in songs] == ["song-1"]
    assert songs[0].title == "晴天"


def test_navidrome_now_playing_uses_song_title_instead_of_album(monkeypatch):
    """正在播放接口返回单曲时，仪表盘标题必须显示曲名而不是所属专辑名。"""
    client = object.__new__(Navidrome)
    client._play_host = "https://music.example.com"
    monkeypatch.setattr(
        client,
        "_call",
        lambda *_args, **_kwargs: {
            "nowPlaying": {
                "entry": [
                    {
                        "id": "song-1",
                        "title": "晴天",
                        "album": "叶惠美",
                        "artist": "周杰伦",
                        "coverArt": "cover-1",
                    }
                ]
            }
        },
    )
    monkeypatch.setattr(client, "_album_cover", lambda _item: "cover-url")

    items = client.get_resume()

    assert len(items) == 1
    assert items[0].item_id == "song-1"
    assert items[0].title == "晴天"
    assert items[0].subtitle == "周杰伦"
    assert items[0].image == "cover-url"


def test_navidrome_album_exists_requires_complete_track_count(monkeypatch):
    """同名专辑曲目不足时不得把整专订阅判定为已完整入库。"""
    module = NavidromeModule()
    service = Mock()
    service.get_iteminfo.return_value = schemas.MediaServerItem(
        item_id="album-1",
        title="叶惠美",
        item_type="音乐",
        note={"artist": "周杰伦", "song_count": 10},
    )
    service.search_music.return_value = []
    monkeypatch.setattr(module, "get_instances", lambda: {"music": service})
    album = MusicInfo(
        media_source="musicbrainz",
        media_id="release-group-1",
        music_type="album",
        title="叶惠美",
        artists=["周杰伦"],
        total_tracks=11,
    )

    assert module.media_exists(album, itemid="album-1") is None

    service.get_iteminfo.return_value.note["song_count"] = 11
    exists = module.media_exists(album, itemid="album-1")

    assert exists is not None
    assert exists.itemid == "album-1"
