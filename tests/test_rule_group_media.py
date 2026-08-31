from contextlib import contextmanager
from unittest.mock import MagicMock

from app.application.filtering import normalize_media_type
from app.application.rules import RuleHelper
# pylint: disable=no-name-in-module  # SubscribeChain 由惰性稳定入口导出，Pylint 无法静态解析。
from app.chain.subscribe import SubscribeChain
from app.domain.context import MediaInfo, MusicInfo, TorrentInfo
from app.modules.filter import FilterModule
from app.runtime.events import Event
from app.schemas.event import ConfigChangeEventData
from app.schemas.rule import FilterRuleGroup
from app.schemas.types import EventType, MediaType, SystemConfigKey


def test_agent_rule_group_media_type_accepts_music_aliases():
    """Agent 规则组写入入口应统一接受中英文音乐类型。"""
    assert normalize_media_type("music") == MediaType.MUSIC.value
    assert normalize_media_type("音乐") == MediaType.MUSIC.value


def test_music_rule_group_matches_music_media(monkeypatch):
    """音乐规则组应在音乐搜索和订阅的过滤上下文中生效。"""
    helper = RuleHelper()
    groups = [
        FilterRuleGroup(name="music", rule_string="FLAC", media_type=MediaType.MUSIC.value),
        FilterRuleGroup(name="movie", rule_string="BLURAY", media_type=MediaType.MOVIE.value),
    ]
    monkeypatch.setattr(helper, "get_rule_groups", lambda: groups)

    matched = helper.get_rule_group_by_media(
        media=MusicInfo(title="Example"),
        group_names=["music", "movie"],
    )

    assert [group.name for group in matched] == ["music"]


def test_music_rule_group_filters_music_torrents(monkeypatch):
    """音乐专属规则组被选中后应实际过滤音乐资源。"""
    helper = RuleHelper()
    groups = [FilterRuleGroup(name="music", rule_string="FLAC", media_type=MediaType.MUSIC.value)]
    monkeypatch.setattr(helper, "get_rule_groups", lambda: groups)
    module = FilterModule()
    module.rulehelper = helper
    module.rule_set = {"FLAC": {"include": "FLAC"}}
    lossless = TorrentInfo(title="Artist Album FLAC", description="")
    lossy = TorrentInfo(title="Artist Album MP3 320kbps", description="")

    filtered = module.filter_torrents(
        rule_groups=["music"],
        torrent_list=[lossless, lossy],
        mediainfo=MusicInfo(title="Album"),
    )

    assert filtered == [lossless]


def test_rule_group_category_cannot_cross_media_types(monkeypatch):
    """二级分类相同时仍必须先匹配规则组的主媒体类型。"""
    helper = RuleHelper()
    groups = [
        FilterRuleGroup(
            name="movie-category",
            rule_string="BLURAY",
            media_type=MediaType.MOVIE.value,
            category="shared",
        )
    ]
    monkeypatch.setattr(helper, "get_rule_groups", lambda: groups)
    media = MediaInfo(type=MediaType.TV, category="shared")

    assert helper.get_rule_group_by_media(media=media, group_names=["movie-category"]) == []


def test_reconcile_rule_group_references_removes_all_dangling_bindings(monkeypatch):
    """规则组事件必须委托单一原子服务处理全部引用。"""
    mutation = MagicMock()

    @contextmanager
    def mutation_scope():
        """提供可观测的同步规则组事务作用域。"""
        yield mutation

    chain = object.__new__(SubscribeChain)
    chain.rule_group_mutation_scope = mutation_scope

    SubscribeChain.reconcile_rule_group_references(
        chain,
        Event(
            EventType.ConfigChanged,
            ConfigChangeEventData(
                key=SystemConfigKey.UserFilterRuleGroups,
                value=[{"name": "keep", "rule_string": "4K"}],
            ),
        ),
    )

    definitions = [{"name": "keep", "rule_string": "4K"}]
    mutation.apply.assert_called_once_with(
        definitions,
        expected_rule_groups=definitions,
    )


def test_site_deleted_delegates_reference_cleanup_to_atomic_scope() -> None:
    """站点删除事件不得分别修改 SystemConfig 和订阅仓储。"""
    mutation = MagicMock()

    @contextmanager
    def mutation_scope():
        """提供可观测的同步站点引用事务作用域。"""
        yield mutation

    chain = object.__new__(SubscribeChain)
    chain.site_reference_mutation_scope = mutation_scope

    SubscribeChain.remove_site(
        chain,
        Event(EventType.SiteDeleted, {"site_id": 3}),
    )

    mutation.apply.assert_called_once_with(3)
