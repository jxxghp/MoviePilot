import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from app.core.context import MediaInfo, TorrentInfo
from app.helper.torrent import TorrentHelper
from app.modules.filter import FilterModule


class _RuleHelper:
    """
    过滤模块测试用的轻量规则仓库，避免依赖真实系统配置。
    """

    def __init__(self, groups):
        self._groups = groups

    def get_rule_group_by_media(self, media=None, group_names=None):  # noqa: ARG002
        if not group_names:
            return self._groups
        return [group for group in self._groups if group.name in group_names]


def _build_filter_module(rule_string: str, rule_set: dict) -> FilterModule:
    module = FilterModule()
    module.rulehelper = _RuleHelper(
        [SimpleNamespace(name="test", rule_string=rule_string)]
    )
    module.rule_set = rule_set
    return module


class TorrentFilterTest(unittest.TestCase):

    def test_filter_torrents_keeps_priority_and_boolean_rule_semantics(self):
        module = _build_filter_module(
            rule_string="HDR & !BLU > DV",
            rule_set={
                "HDR": {"include": "HDR"},
                "DV": {"include": "DOVI"},
                "BLU": {"include": "BluRay"},
            },
        )
        torrents = [
            TorrentInfo(title="Movie HDR WEB-DL", description=""),
            TorrentInfo(title="Movie DOVI", description=""),
            TorrentInfo(title="Movie HDR BluRay", description=""),
        ]

        filtered = module.filter_torrents(rule_groups=["test"], torrent_list=torrents)

        self.assertEqual(torrents[:2], filtered)
        self.assertEqual(100, filtered[0].pri_order)
        self.assertEqual(99, filtered[1].pri_order)

    def test_filter_torrents_keeps_lazy_priority_level_parsing(self):
        module = _build_filter_module(
            rule_string="KEEP > (",
            rule_set={"KEEP": {"include": "Movie"}},
        )
        torrent = TorrentInfo(title="Movie", description="")

        filtered = module.filter_torrents(rule_groups=["test"], torrent_list=[torrent])

        self.assertEqual([torrent], filtered)
        self.assertEqual(100, torrent.pri_order)

    def test_filter_torrents_keeps_sequential_rule_group_semantics(self):
        module = FilterModule()
        module.rulehelper = _RuleHelper(
            [
                SimpleNamespace(name="first", rule_string="HDR"),
                SimpleNamespace(name="second", rule_string="FREE"),
            ]
        )
        module.rule_set = {
            "HDR": {"include": "HDR"},
            "FREE": {"downloadvolumefactor": 0},
        }
        keep = TorrentInfo(title="Movie HDR WEB-DL", description="", downloadvolumefactor=0)
        drop = TorrentInfo(title="Movie HDR WEB-DL", description="", downloadvolumefactor=1)

        filtered = module.filter_torrents(rule_groups=["first", "second"], torrent_list=[keep, drop])

        self.assertEqual([keep], filtered)
        self.assertEqual(100, keep.pri_order)

    def test_filter_torrents_supports_full_rule_fields_in_rust_entry(self):
        module = _build_filter_module(
            rule_string="TMDB & LABEL & SIZE & SEED & PUB & SITE",
            rule_set={
                "TMDB": {"tmdb": {"original_language": "zh,cn"}},
                "LABEL": {"include": "官方", "match": ["labels"]},
                "SIZE": {"size_range": "100-400"},
                "SEED": {"seeders": "5"},
                "PUB": {"publish_time": "0-120"},
                "SITE": {"include": "Alpha", "match": ["site_name"]},
            },
        )
        torrent = TorrentInfo(
            site_name="Alpha",
            title="Show S01E01-E02 1080p",
            description="",
            labels=["官方"],
            size=600 * 1024 * 1024,
            seeders=8,
            pubdate=(datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
        )
        mediainfo = MediaInfo()
        mediainfo.original_language = "zh"

        filtered = module.filter_torrents(rule_groups=["test"], torrent_list=[torrent], mediainfo=mediainfo)

        self.assertEqual([torrent], filtered)
        self.assertEqual(100, torrent.pri_order)

    def test_filter_torrents_uses_rust_entry_without_python_match_fallback(self):
        module = _build_filter_module(
            rule_string="KEEP",
            rule_set={"KEEP": {"include": "Movie"}},
        )

        def fail_python_fallback(*_args, **_kwargs):
            """
            如果入口仍调用旧 Python 私有匹配逻辑，测试应立即失败。
            """
            raise AssertionError("Python fallback should not be called")

        module._FilterModule__filter_torrents = fail_python_fallback

        with patch("app.modules.filter.rust_accel.is_enabled", return_value=True), \
                patch("app.modules.filter.rust_accel.filter_torrents", return_value=[(0, 100)]):
            filtered = module.filter_torrents(
                rule_groups=["test"],
                torrent_list=[TorrentInfo(title="Movie", description="")],
            )

        self.assertEqual(1, len(filtered))

    def test_filter_torrents_uses_python_fallback_when_rust_disabled(self):
        """
        Rust 加速关闭时应使用 Python 过滤路径，并保留规则优先级语义。
        """
        module = _build_filter_module(
            rule_string="HDR & !BLU > DV",
            rule_set={
                "HDR": {"include": "HDR"},
                "DV": {"include": "DOVI"},
                "BLU": {"include": "BluRay"},
            },
        )
        torrents = [
            TorrentInfo(title="Movie HDR WEB-DL", description=""),
            TorrentInfo(title="Movie DOVI", description=""),
            TorrentInfo(title="Movie HDR BluRay", description=""),
        ]

        with patch("app.modules.filter.rust_accel.is_enabled", return_value=False):
            filtered = module.filter_torrents(rule_groups=["test"], torrent_list=torrents)

        self.assertEqual(torrents[:2], filtered)
        self.assertEqual(100, filtered[0].pri_order)
        self.assertEqual(99, filtered[1].pri_order)

    def test_filter_torrent_keeps_extra_filter_semantics(self):
        torrent = TorrentInfo(
            title="Movie 1080p HDR",
            description="中字",
            labels=["free"],
            size=3 * 1024 * 1024 * 1024,
            uploadvolumefactor=1,
            downloadvolumefactor=0,
        )

        self.assertTrue(
            TorrentHelper.filter_torrent(
                torrent_info=torrent,
                filter_params={
                    "include": "中字|free",
                    "exclude": "BluRay",
                    "resolution": "1080p",
                    "effect": "HDR",
                    "size": "1000-4000",
                },
            )
        )
        self.assertFalse(
            TorrentHelper.filter_torrent(
                torrent_info=torrent,
                filter_params={"exclude": "HDR"},
            )
        )
        self.assertFalse(
            TorrentHelper.filter_torrent(
                torrent_info=torrent,
                filter_params={"size": "<1000"},
            )
        )
