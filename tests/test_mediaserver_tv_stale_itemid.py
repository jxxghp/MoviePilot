"""
验证媒体服务器缓存条目标识失效时的剧集查询回退。
"""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from plexapi.exceptions import NotFound

from app.modules.emby.emby import Emby
from app.modules.jellyfin.jellyfin import Jellyfin
from app.modules.plex.plex import Plex
from app.modules.trimemedia.trimemedia import TrimeMedia
from app.modules.ugreen.ugreen import Ugreen
from app.modules.zspace.zspace import ZSpace
from app.schemas.types import MediaSource


class _FakeResponse:
    """
    提供媒体服务器接口响应的最小json封装。
    """

    def __init__(self, payload: dict):
        """
        保存模拟接口的响应内容与状态，避免实际网络请求。
        """
        self._payload = payload

    def json(self):
        """
        返回测试预置的响应体。
        """
        return self._payload


def _build_plex():
    """
    构造绕过真实连接的Plex实例，便于单测直接注入plexapi mock。
    """
    plex = Plex.__new__(Plex)
    plex._host = "http://192.168.8.254:32400/"
    plex._playhost = None
    plex._token = "plex-token"
    plex._plex = Mock()
    return plex


def test_plex_tv_episodes_fallback_when_cached_item_id_not_found() -> None:
    """
    Plex缓存ID失效时，应按标题重新搜索并返回新条目的季集。
    """
    plex = _build_plex()
    plex._plex.fetchItem.side_effect = NotFound("not found")

    show = Mock()
    show.key = "/library/metadata/200"
    show.guids = [{"id": "tmdb://12345"}]
    show.episodes.return_value = [
        Mock(seasonNumber=1, index=1),
        Mock(seasonNumber=1, index=2),
        Mock(seasonNumber=2, index=1),
    ]
    plex._plex.library.search.return_value = [show]

    item_id, episodes = plex.get_tv_episodes(
        item_id="107797",
        title="测试剧集",
        original_title="Test Show",
        year="2026",
        media_source=MediaSource.TMDB,
        media_id="12345",
        season=1,
    )

    assert item_id == "/library/metadata/200"
    assert episodes == {1: [1, 2]}
    plex._plex.fetchItem.assert_called_once_with(107797)
    plex._plex.library.search.assert_called_once_with(title="测试剧集", libtype="show", year="2026")


def test_plex_tv_episodes_returns_empty_when_stale_item_id_search_misses() -> None:
    """
    Plex缓存ID失效且标题搜索不到时，应返回未入库结果而不是继续抛出404。
    """
    plex = _build_plex()
    plex._plex.fetchItem.side_effect = NotFound("not found")
    plex._plex.library.search.side_effect = [[], []]

    item_id, episodes = plex.get_tv_episodes(
        item_id="107797",
        title="测试剧集",
        original_title="Test Show",
        year="2026",
    )

    assert item_id is None
    assert episodes == {}
    assert plex._plex.library.search.call_count == 2


def test_plex_tv_episodes_uses_valid_item_id_without_search() -> None:
    """
    Plex缓存ID仍有效时，应保持直接查询路径，避免额外模糊搜索。
    """
    plex = _build_plex()

    show = Mock()
    show.key = "/library/metadata/107797"
    show.guids = [{"id": "tmdb://12345"}]
    show.episodes.return_value = [Mock(seasonNumber=1, index=1)]
    plex._plex.fetchItem.return_value = show

    item_id, episodes = plex.get_tv_episodes(
        item_id="107797",
        title="测试剧集",
        media_source=MediaSource.TMDB,
        media_id="12345",
    )

    assert item_id == "/library/metadata/107797"
    assert episodes == {1: [1]}
    plex._plex.fetchItem.assert_called_once_with(107797)
    plex._plex.library.search.assert_not_called()


def test_emby_tv_episodes_fallback_when_cached_item_id_missing() -> None:
    """
    Emby缓存ID失效时，应重新搜索剧集ID后再查询集信息。
    """
    client = Emby.__new__(Emby)
    client._host = "http://emby.local/"
    client._apikey = "api-key"
    client.user = "user-id"
    client.get_iteminfo = Mock(
        side_effect=[
            None,
            SimpleNamespace(media_source=MediaSource.TMDB, media_id="12345"),
        ]
    )
    client._Emby__get_emby_series_id_by_name = Mock(return_value="new-series-id")

    with patch("app.modules.emby.emby.RequestUtils") as request_utils_cls:
        request_utils_cls.return_value.get_res.return_value = _FakeResponse(
            {"Items": [{"ParentIndexNumber": 1, "IndexNumber": 1}]}
        )

        item_id, episodes = client.get_tv_episodes(
            item_id="old-series-id",
            title="测试剧集",
            year="2026",
            media_source=MediaSource.TMDB,
            media_id="12345",
        )

    assert item_id == "new-series-id"
    assert episodes == {1: [1]}
    client._Emby__get_emby_series_id_by_name.assert_called_once_with("测试剧集", "2026")


def test_jellyfin_tv_episodes_fallback_when_cached_item_id_missing() -> None:
    """
    Jellyfin缓存ID失效时，应重新搜索剧集ID后再查询集信息。
    """
    client = Jellyfin.__new__(Jellyfin)
    client._host = "http://jellyfin.local/"
    client._apikey = "api-key"
    client.user = "user-id"
    client.get_iteminfo = Mock(
        side_effect=[
            None,
            SimpleNamespace(media_source=MediaSource.TMDB, media_id="12345"),
        ]
    )
    client._Jellyfin__get_jellyfin_series_id_by_name = Mock(return_value="new-series-id")

    with patch("app.modules.jellyfin.jellyfin.RequestUtils") as request_utils_cls:
        request_utils_cls.return_value.get_res.return_value = _FakeResponse(
            {"Items": [{"ParentIndexNumber": 1, "IndexNumber": 1}]}
        )

        item_id, episodes = client.get_tv_episodes(
            item_id="old-series-id",
            title="测试剧集",
            year="2026",
            media_source=MediaSource.TMDB,
            media_id="12345",
        )

    assert item_id == "new-series-id"
    assert episodes == {1: [1]}
    client._Jellyfin__get_jellyfin_series_id_by_name.assert_called_once_with("测试剧集", "2026")


def test_zspace_tv_episodes_fallback_when_cached_item_id_missing() -> None:
    """
    极影视缓存ID失效时，应重新搜索剧集ID后再查询集信息。
    """
    client = ZSpace.__new__(ZSpace)
    client._host = "http://zspace.local/"
    client._apikey = "api-key"
    client.user = "user-id"
    client.get_iteminfo = Mock(
        side_effect=[
            None,
            SimpleNamespace(media_source=MediaSource.TMDB, media_id="12345"),
        ]
    )
    client._ZSpace__get_series_id_by_name = Mock(return_value="new-series-id")

    with patch("app.modules.zspace.zspace.RequestUtils") as request_utils_cls:
        request_utils_cls.return_value.get_res.return_value = _FakeResponse(
            {"Items": [{"ParentIndexNumber": 1, "IndexNumber": 1}]}
        )

        item_id, episodes = client.get_tv_episodes(
            item_id="old-series-id",
            title="测试剧集",
            year="2026",
            media_source=MediaSource.TMDB,
            media_id="12345",
        )

    assert item_id == "new-series-id"
    assert episodes == {1: [1]}
    client._ZSpace__get_series_id_by_name.assert_called_once_with("测试剧集", "2026")


def test_ugreen_tv_episodes_fallback_when_cached_item_id_missing() -> None:
    """
    绿联缓存ID失效时，应重新搜索剧集ID后再查询集信息。
    """
    client = Ugreen.__new__(Ugreen)
    client._api = Mock()
    client.is_authenticated = Mock(return_value=True)
    client.get_iteminfo = Mock(
        side_effect=[
            None,
            SimpleNamespace(media_source=MediaSource.TMDB, media_id="12345"),
        ]
    )
    client._Ugreen__search_tv_item = Mock(return_value={"ug_video_info_id": "new-series-id"})
    client._api.get_tv.return_value = {
        "season_info": [{"category_id": "season-1", "season_num": 1}],
        "tv_info": [{"category_id": "season-1", "episode": 1}],
    }

    item_id, episodes = client.get_tv_episodes(
        item_id="old-series-id",
        title="测试剧集",
        year="2026",
        media_source=MediaSource.TMDB,
        media_id="12345",
    )

    assert item_id == "new-series-id"
    assert episodes == {1: [1]}
    client._Ugreen__search_tv_item.assert_called_once_with("测试剧集", "2026", MediaSource.TMDB, "12345")


def test_trime_media_tv_episodes_fallback_when_cached_item_id_missing() -> None:
    """
    飞牛影视缓存ID失效时，应重新搜索剧集ID后再查询集信息。
    """
    client = TrimeMedia.__new__(TrimeMedia)
    client._api = Mock()
    client.is_authenticated = Mock(return_value=True)
    client.get_iteminfo = Mock(
        side_effect=[
            None,
            SimpleNamespace(media_source=MediaSource.TMDB, media_id="12345"),
        ]
    )
    client._TrimeMedia__get_series_id_by_name = Mock(return_value="new-series-id")
    client._api.season_list.return_value = [SimpleNamespace(season_number=1, guid="season-1")]
    client._api.episode_list.return_value = [SimpleNamespace(season_number=1, episode_number=1)]

    item_id, episodes = client.get_tv_episodes(
        item_id="old-series-id",
        title="测试剧集",
        year="2026",
        media_source=MediaSource.TMDB,
        media_id="12345",
    )

    assert item_id == "new-series-id"
    assert episodes == {1: [1]}
    client._TrimeMedia__get_series_id_by_name.assert_called_once_with("测试剧集", "2026")
