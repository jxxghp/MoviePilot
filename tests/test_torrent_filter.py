from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from app.core.context import MediaInfo, TorrentInfo
from app.helper.torrent import TorrentHelper
from app.modules.filter import FilterModule
from app.modules.filter.builtin_rules import BUILTIN_RULE_SET
from app.utils import rust_accel


class _RuleHelper:
    """
    过滤模块测试用的轻量规则仓库，避免依赖真实系统配置。
    """

    def __init__(self, groups):
        """
        保存测试规则组。
        """
        self._groups = groups

    def get_rule_group_by_media(self, media=None, group_names=None):  # noqa: ARG002
        """
        按名称返回测试规则组。
        """
        if not group_names:
            return self._groups
        return [group for group in self._groups if group.name in group_names]


def _build_filter_module(rule_string: str, rule_set: dict) -> FilterModule:
    """
    构造绑定轻量规则仓库的过滤模块。
    """
    module = FilterModule()
    module.rulehelper = _RuleHelper(
        [SimpleNamespace(name="test", rule_string=rule_string)]
    )
    module.rule_set = rule_set
    return module


def test_filter_torrents_keeps_priority_and_boolean_rule_semantics():
    """
    过滤规则应保持优先级和布尔表达式语义。
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

    filtered = module.filter_torrents(rule_groups=["test"], torrent_list=torrents)

    assert torrents[:2] == filtered
    assert filtered[0].pri_order == 100
    assert filtered[1].pri_order == 99


def test_builtin_hdr_rule_matches_hdr_vivid_release():
    """
    内置 HDR 规则应覆盖 HDR Vivid 合并写法，保证订阅优先级规则可命中。
    """
    module = _build_filter_module(
        rule_string="HDR",
        rule_set=BUILTIN_RULE_SET,
    )
    torrent = TorrentInfo(title="Movie 2026 2160p WEB-DL HDRVivid H265", description="")

    filtered = module.filter_torrents(rule_groups=["test"], torrent_list=[torrent])

    assert [torrent] == filtered
    assert torrent.pri_order == 100


def test_filter_torrents_keeps_lazy_priority_level_parsing():
    """
    命中高优先级规则后不应解析低优先级坏规则。
    """
    module = _build_filter_module(
        rule_string="KEEP > (",
        rule_set={"KEEP": {"include": "Movie"}},
    )
    torrent = TorrentInfo(title="Movie", description="")

    filtered = module.filter_torrents(rule_groups=["test"], torrent_list=[torrent])

    assert [torrent] == filtered
    assert torrent.pri_order == 100


def test_filter_torrents_keeps_sequential_rule_group_semantics():
    """
    多个规则组应按顺序逐轮过滤。
    """
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

    assert [keep] == filtered
    assert keep.pri_order == 100


def test_filter_torrents_supports_full_rule_fields_in_rust_entry():
    """
    Rust 过滤入口应支持完整规则字段。
    """
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

    assert [torrent] == filtered
    assert torrent.pri_order == 100


def test_filter_torrents_uses_rust_entry_without_python_match_fallback():
    """
    Rust 返回旧格式结果时应保持兼容，并且不进入 Python 过滤兜底。
    """
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

    assert len(filtered) == 1


def test_filter_torrents_logs_rust_trace_details():
    """
    Rust trace 返回的规则明细应写入过滤模块 debug 日志。
    """
    module = _build_filter_module(
        rule_string="KEEP",
        rule_set={"KEEP": {"include": "Movie"}},
    )

    with patch("app.modules.filter.rust_accel.filter_torrents",
               return_value=([(0, 100)], ["种子 Alpha - Movie 优先级为 1"])) as rust_filter, \
            patch("app.modules.filter.logger.debug") as log_debug:
        filtered = module.filter_torrents(
            rule_groups=["test"],
            torrent_list=[TorrentInfo(site_name="Alpha", title="Movie", description="")],
        )

    assert len(filtered) == 1
    rust_filter.assert_called_once()
    log_debug.assert_called_with("种子 Alpha - Movie 优先级为 1")


def test_rust_accel_filter_torrents_uses_trace_entry_when_debug_enabled():
    """
    debug 日志启用时 Rust 包装层应调用 trace 入口并返回规则明细。
    """

    class FakeRustExtension:
        """
        提供过滤入口的测试 Rust 扩展替身。
        """

        def is_available(self):
            """
            声明扩展可用。
            """
            return True

        def filter_torrents_with_trace_fast(self, *_args):
            """
            返回带调试日志的过滤结果。
            """
            return [(0, 100)], ["trace"]

        def filter_torrents_fast(self, *_args):
            """
            普通入口不应在本用例中被调用。
            """
            raise AssertionError("trace entry should be used")

    with patch.object(rust_accel, "_moviepilot_rust", FakeRustExtension()), \
            patch.object(rust_accel, "is_debug_log_enabled", return_value=True):
        result = rust_accel.filter_torrents([], [], {})

    assert result == ([(0, 100)], ["trace"])


def test_rust_accel_filter_torrents_keeps_fast_entry_without_debug():
    """
    debug 日志关闭时 Rust 包装层应继续调用原高速入口。
    """

    class FakeRustExtension:
        """
        提供过滤入口的测试 Rust 扩展替身。
        """

        def is_available(self):
            """
            声明扩展可用。
            """
            return True

        def filter_torrents_with_trace_fast(self, *_args):
            """
            trace 入口不应在本用例中被调用。
            """
            raise AssertionError("fast entry should be used")

        def filter_torrents_fast(self, *_args):
            """
            返回普通过滤结果。
            """
            return [(0, 100)]

    with patch.object(rust_accel, "_moviepilot_rust", FakeRustExtension()), \
            patch.object(rust_accel, "is_debug_log_enabled", return_value=False):
        result = rust_accel.filter_torrents([], [], {})

    assert result == ([(0, 100)], [])


def test_filter_torrents_uses_python_fallback_when_rust_disabled():
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

    assert torrents[:2] == filtered
    assert filtered[0].pri_order == 100
    assert filtered[1].pri_order == 99


def test_filter_torrent_keeps_extra_filter_semantics():
    """
    普通过滤参数应保持包含、排除和大小规则语义。
    """
    torrent = TorrentInfo(
        title="Movie 1080p HDR",
        description="中字",
        labels=["free"],
        size=3 * 1024 * 1024 * 1024,
        uploadvolumefactor=1,
        downloadvolumefactor=0,
    )

    assert TorrentHelper.filter_torrent(
        torrent_info=torrent,
        filter_params={
            "include": "中字|free",
            "exclude": "BluRay",
            "resolution": "1080p",
            "effect": "HDR",
            "size": "1000-4000",
        },
    )
    assert not TorrentHelper.filter_torrent(
        torrent_info=torrent,
        filter_params={"exclude": "HDR"},
    )
    assert not TorrentHelper.filter_torrent(
        torrent_info=torrent,
        filter_params={"size": "<1000"},
    )
