import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from plexapi.exceptions import NotFound

from app.modules.emby.emby import Emby
from app.modules.jellyfin.jellyfin import Jellyfin
from app.modules.plex.plex import Plex
from app.modules.trimemedia.trimemedia import TrimeMedia
from app.modules.ugreen.ugreen import Ugreen
from app.modules.zspace.zspace import ZSpace


class _FakeResponse:
    """提供媒体服务器接口响应的最小json封装。"""

    def __init__(self, payload: dict):
        self._payload = payload

    def json(self):
        """返回测试预置的响应体。"""
        return self._payload


class MediaServerTvStaleItemIdTest(unittest.TestCase):
    """验证电视剧缓存ID失效后，各媒体服务器适配器会回退到标题搜索。"""

    @staticmethod
    def _build_plex():
        """构造绕过真实连接的Plex实例，便于单测直接注入plexapi mock。"""
        plex = Plex.__new__(Plex)
        plex._host = "http://192.168.8.254:32400/"
        plex._playhost = None
        plex._token = "plex-token"
        plex._plex = Mock()
        return plex

    def test_plex_tv_episodes_fallback_when_cached_item_id_not_found(self):
        """Plex缓存ID失效时，应按标题重新搜索并返回新条目的季集。"""
        plex = self._build_plex()
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
            tmdb_id=12345,
            season=1,
        )

        self.assertEqual(item_id, "/library/metadata/200")
        self.assertEqual(episodes, {1: [1, 2]})
        plex._plex.fetchItem.assert_called_once_with(107797)
        plex._plex.library.search.assert_called_once_with(title="测试剧集", libtype="show", year="2026")

    def test_plex_tv_episodes_returns_empty_when_stale_item_id_search_misses(self):
        """Plex缓存ID失效且标题搜索不到时，应返回未入库结果而不是继续抛出404。"""
        plex = self._build_plex()
        plex._plex.fetchItem.side_effect = NotFound("not found")
        plex._plex.library.search.side_effect = [[], []]

        item_id, episodes = plex.get_tv_episodes(
            item_id="107797",
            title="测试剧集",
            original_title="Test Show",
            year="2026",
        )

        self.assertIsNone(item_id)
        self.assertEqual(episodes, {})
        self.assertEqual(plex._plex.library.search.call_count, 2)

    def test_plex_tv_episodes_uses_valid_item_id_without_search(self):
        """Plex缓存ID仍有效时，应保持直接查询路径，避免额外模糊搜索。"""
        plex = self._build_plex()

        show = Mock()
        show.key = "/library/metadata/107797"
        show.guids = [{"id": "tmdb://12345"}]
        show.episodes.return_value = [Mock(seasonNumber=1, index=1)]
        plex._plex.fetchItem.return_value = show

        item_id, episodes = plex.get_tv_episodes(
            item_id="107797",
            title="测试剧集",
            tmdb_id=12345,
        )

        self.assertEqual(item_id, "/library/metadata/107797")
        self.assertEqual(episodes, {1: [1]})
        plex._plex.fetchItem.assert_called_once_with(107797)
        plex._plex.library.search.assert_not_called()

    def test_emby_tv_episodes_fallback_when_cached_item_id_missing(self):
        """Emby缓存ID失效时，应重新搜索剧集ID后再查询集信息。"""
        client = Emby.__new__(Emby)
        client._host = "http://emby.local/"
        client._apikey = "api-key"
        client.user = "user-id"
        client.get_iteminfo = Mock(side_effect=[None, SimpleNamespace(tmdbid=12345)])
        client._Emby__get_emby_series_id_by_name = Mock(return_value="new-series-id")

        with patch("app.modules.emby.emby.RequestUtils") as request_utils_cls:
            request_utils_cls.return_value.get_res.return_value = _FakeResponse({
                "Items": [{"ParentIndexNumber": 1, "IndexNumber": 1}]
            })

            item_id, episodes = client.get_tv_episodes(
                item_id="old-series-id",
                title="测试剧集",
                year="2026",
                tmdb_id=12345,
            )

        self.assertEqual(item_id, "new-series-id")
        self.assertEqual(episodes, {1: [1]})
        client._Emby__get_emby_series_id_by_name.assert_called_once_with("测试剧集", "2026")

    def test_jellyfin_tv_episodes_fallback_when_cached_item_id_missing(self):
        """Jellyfin缓存ID失效时，应重新搜索剧集ID后再查询集信息。"""
        client = Jellyfin.__new__(Jellyfin)
        client._host = "http://jellyfin.local/"
        client._apikey = "api-key"
        client.user = "user-id"
        client.get_iteminfo = Mock(side_effect=[None, SimpleNamespace(tmdbid=12345)])
        client._Jellyfin__get_jellyfin_series_id_by_name = Mock(return_value="new-series-id")

        with patch("app.modules.jellyfin.jellyfin.RequestUtils") as request_utils_cls:
            request_utils_cls.return_value.get_res.return_value = _FakeResponse({
                "Items": [{"ParentIndexNumber": 1, "IndexNumber": 1}]
            })

            item_id, episodes = client.get_tv_episodes(
                item_id="old-series-id",
                title="测试剧集",
                year="2026",
                tmdb_id=12345,
            )

        self.assertEqual(item_id, "new-series-id")
        self.assertEqual(episodes, {1: [1]})
        client._Jellyfin__get_jellyfin_series_id_by_name.assert_called_once_with("测试剧集", "2026")

    def test_zspace_tv_episodes_fallback_when_cached_item_id_missing(self):
        """极影视缓存ID失效时，应重新搜索剧集ID后再查询集信息。"""
        client = ZSpace.__new__(ZSpace)
        client._host = "http://zspace.local/"
        client._apikey = "api-key"
        client.user = "user-id"
        client.get_iteminfo = Mock(side_effect=[None, SimpleNamespace(tmdbid=12345)])
        client._ZSpace__get_series_id_by_name = Mock(return_value="new-series-id")

        with patch("app.modules.zspace.zspace.RequestUtils") as request_utils_cls:
            request_utils_cls.return_value.get_res.return_value = _FakeResponse({
                "Items": [{"ParentIndexNumber": 1, "IndexNumber": 1}]
            })

            item_id, episodes = client.get_tv_episodes(
                item_id="old-series-id",
                title="测试剧集",
                year="2026",
                tmdb_id=12345,
            )

        self.assertEqual(item_id, "new-series-id")
        self.assertEqual(episodes, {1: [1]})
        client._ZSpace__get_series_id_by_name.assert_called_once_with("测试剧集", "2026")

    def test_ugreen_tv_episodes_fallback_when_cached_item_id_missing(self):
        """绿联缓存ID失效时，应重新搜索剧集ID后再查询集信息。"""
        client = Ugreen.__new__(Ugreen)
        client._api = Mock()
        client.is_authenticated = Mock(return_value=True)
        client.get_iteminfo = Mock(side_effect=[None, SimpleNamespace(tmdbid=12345)])
        client._Ugreen__search_tv_item = Mock(return_value={"ug_video_info_id": "new-series-id"})
        client._api.get_tv.return_value = {
            "season_info": [{"category_id": "season-1", "season_num": 1}],
            "tv_info": [{"category_id": "season-1", "episode": 1}],
        }

        item_id, episodes = client.get_tv_episodes(
            item_id="old-series-id",
            title="测试剧集",
            year="2026",
            tmdb_id=12345,
        )

        self.assertEqual(item_id, "new-series-id")
        self.assertEqual(episodes, {1: [1]})
        client._Ugreen__search_tv_item.assert_called_once_with("测试剧集", "2026", 12345)

    def test_trime_media_tv_episodes_fallback_when_cached_item_id_missing(self):
        """飞牛影视缓存ID失效时，应重新搜索剧集ID后再查询集信息。"""
        client = TrimeMedia.__new__(TrimeMedia)
        client._api = Mock()
        client.is_authenticated = Mock(return_value=True)
        client.get_iteminfo = Mock(side_effect=[None, SimpleNamespace(tmdbid=12345)])
        client._TrimeMedia__get_series_id_by_name = Mock(return_value="new-series-id")
        client._api.season_list.return_value = [SimpleNamespace(season_number=1, guid="season-1")]
        client._api.episode_list.return_value = [
            SimpleNamespace(season_number=1, episode_number=1)
        ]

        item_id, episodes = client.get_tv_episodes(
            item_id="old-series-id",
            title="测试剧集",
            year="2026",
            tmdb_id=12345,
        )

        self.assertEqual(item_id, "new-series-id")
        self.assertEqual(episodes, {1: [1]})
        client._TrimeMedia__get_series_id_by_name.assert_called_once_with("测试剧集", "2026")
