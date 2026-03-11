import sys
from types import ModuleType, SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

sys.modules.setdefault("app.helper.sites", ModuleType("app.helper.sites"))
setattr(sys.modules["app.helper.sites"], "SitesHelper", object)

from app.chain.search import SearchChain
from app.core.context import MediaInfo, TorrentInfo
from app.schemas.types import MediaType


class SearchChainTest(TestCase):
    def test_parse_result_uses_class_call_for_season_episode_match(self):
        chain = SearchChain.__new__(SearchChain)
        torrent = TorrentInfo(
            site_name="TestSite",
            title="Example.Show.S01E01.1080p",
            description="",
            imdbid="tt1234567"
        )
        mediainfo = MediaInfo(
            title="Example Show",
            original_title="Example Show",
            names=[],
            imdb_id="tt1234567",
            type=MediaType.TV
        )

        class FakeProgressHelper:
            def __init__(self, *_args, **_kwargs):
                pass

            def start(self):
                pass

            def update(self, **_kwargs):
                pass

            def end(self):
                pass

        def fake_match_season_episodes(torrent, meta, season_episodes):
            self.assertIs(torrent, torrent_info)
            self.assertIs(meta, fake_meta)
            self.assertEqual(season_episodes, {1: [1]})
            return True

        fake_meta = SimpleNamespace(
            org_string=torrent.title,
            season_list=[1],
            episode_list=[1]
        )
        torrent_info = torrent

        with patch("app.chain.search.ProgressHelper", FakeProgressHelper), \
                patch("app.chain.search.MetaInfo", return_value=fake_meta), \
                patch("app.chain.search.TorrentHelper.__init__", return_value=None), \
                patch("app.chain.search.TorrentHelper.sort_torrents", side_effect=lambda contexts: contexts), \
                patch("app.chain.search.TorrentHelper.match_season_episodes", new=fake_match_season_episodes):
            contexts = chain._SearchChain__parse_result(
                torrents=[torrent],
                mediainfo=mediainfo,
                rule_groups=[],
                season_episodes={1: [1]}
            )

        self.assertEqual(len(contexts), 1)
        self.assertIs(contexts[0].torrent_info, torrent)
        self.assertIs(contexts[0].meta_info, fake_meta)
