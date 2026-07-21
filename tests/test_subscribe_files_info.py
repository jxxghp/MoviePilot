"""订阅文件统计相关测试"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.chain.subscribe import SubscribeChain
from app.modules.filemanager import FileManagerModule
from app.schemas.mediaserver import ExistMediaInfo
from app.schemas.types import MediaType


def _build_subscribe(**overrides):
    data = {
        "id": 1,
        "name": "Test Show",
        "year": "2026",
        "type": MediaType.TV.value,
        "season": 1,
        "tmdbid": None,
        "doubanid": None,
        "imdbid": None,
        "tvdbid": None,
        "bangumiid": None,
        "anilistid": None,
        "media_source": None,
        "media_id": None,
        "episode_group": None,
        "start_episode": 1,
        "total_episode": 2,
    }
    data.update(overrides)
    subscribe = SimpleNamespace(**data)
    subscribe.to_dict = lambda: dict(data)
    return subscribe


def _build_mediainfo():
    return SimpleNamespace(
        type=MediaType.TV,
        title="Test Show",
        title_year="Test Show (2026)",
        year="2026",
        tmdb_id=None,
        douban_id=None,
        bangumi_id=None,
        anilist_id=None,
        source=None,
        media_id=None,
    )


def test_filemanager_media_exists_skips_local_when_server_specified():
    module = FileManagerModule()
    mediainfo = _build_mediainfo()

    with patch.object(module, "media_files", return_value=[SimpleNamespace(path="/media/test.mkv")]) as media_files:
        result = module.media_exists(mediainfo, server="Emby1")

    assert result is None
    media_files.assert_not_called()


def test_subscribe_files_info_merges_multiple_mediaservers():
    subscribe = _build_subscribe(season=1, total_episode=2)
    mediainfo = _build_mediainfo()

    def _media_exists_side_effect(*, mediainfo, server=None, **kwargs):
        if server == "Emby1":
            return ExistMediaInfo(
                type=MediaType.TV,
                seasons={1: [1]},
                server_type="emby",
                server="Emby1",
                itemid="emby-series",
            )
        if server == "Jellyfin1":
            return ExistMediaInfo(
                type=MediaType.TV,
                seasons={1: [1]},
                server_type="jellyfin",
                server="Jellyfin1",
                itemid="jf-series",
            )
        return None

    helper = MagicMock()
    helper.get_services.return_value = {"Emby1": object(), "Jellyfin1": object()}

    mediaserver_chain = MagicMock()
    mediaserver_chain.get_play_url.side_effect = lambda server, item_id: f"https://{server}/item/{item_id}"
    mediaserver_chain.get_season_episode_ids.side_effect = lambda server, item_id, season: {1: f"{item_id}-ep1"}

    chain = SubscribeChain()
    with patch("app.chain.subscribe.DownloadHistoryOper") as download_oper, \
            patch.object(chain, "recognize_media", return_value=mediainfo), \
            patch.object(chain, "media_files", return_value=None), \
            patch.object(chain, "media_exists", side_effect=_media_exists_side_effect), \
            patch("app.chain.subscribe.MediaServerHelper", return_value=helper), \
            patch("app.chain.subscribe.MediaServerChain", return_value=mediaserver_chain), \
            patch("app.chain.subscribe.Subscribe", side_effect=lambda **kwargs: SimpleNamespace(**kwargs)):
        download_oper.return_value.get_by_mediaid.return_value = []
        result = chain.subscribe_files_info(subscribe)

    library = result.episodes[1].library
    servers = {item.server for item in library}
    assert servers == {"Emby1", "Jellyfin1"}
    assert all(str(item.file_path).startswith("https://") for item in library)


def test_subscribe_files_info_uses_season_zero_for_tv():
    subscribe = _build_subscribe(season=0, total_episode=1, start_episode=1)
    mediainfo = _build_mediainfo()
    captured_seasons = []

    def _media_exists_side_effect(*, mediainfo, server=None, **kwargs):
        if server == "Emby1":
            return ExistMediaInfo(
                type=MediaType.TV,
                seasons={0: [1]},
                server_type="emby",
                server="Emby1",
                itemid="emby-special",
            )
        return None

    def _get_season_episode_ids(server, item_id, season):
        captured_seasons.append(season)
        return {1: f"{item_id}-ep1"}

    helper = MagicMock()
    helper.get_services.return_value = {"Emby1": object()}

    mediaserver_chain = MagicMock()
    mediaserver_chain.get_play_url.return_value = "https://emby/item/1"
    mediaserver_chain.get_season_episode_ids.side_effect = _get_season_episode_ids

    chain = SubscribeChain()
    with patch("app.chain.subscribe.DownloadHistoryOper") as download_oper, \
            patch.object(chain, "recognize_media", return_value=mediainfo), \
            patch.object(chain, "media_files", return_value=None), \
            patch.object(chain, "media_exists", side_effect=_media_exists_side_effect), \
            patch("app.chain.subscribe.MediaServerHelper", return_value=helper), \
            patch("app.chain.subscribe.MediaServerChain", return_value=mediaserver_chain), \
            patch("app.chain.subscribe.Subscribe", side_effect=lambda **kwargs: SimpleNamespace(**kwargs)):
        download_oper.return_value.get_by_mediaid.return_value = []
        result = chain.subscribe_files_info(subscribe)

    assert captured_seasons == [0]
    assert len(result.episodes[1].library) == 1
    assert result.episodes[1].library[0].server == "Emby1"
