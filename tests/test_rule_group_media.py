from types import SimpleNamespace

from app.chain import subscribe as subscribe_module
from app.chain.subscribe import SubscribeChain
from app.agent.tools.impl._filter_rule_utils import normalize_media_type
from app.application.rules import RuleHelper
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
    groups = [
        FilterRuleGroup(name="music", rule_string="FLAC", media_type=MediaType.MUSIC.value)
    ]
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
    """规则组设置保存后应清理全局默认项、订阅默认值和已有订阅中的悬空名称。"""
    values = {
        SystemConfigKey.SearchFilterRuleGroups: ["keep", "deleted"],
        SystemConfigKey.SubscribeFilterRuleGroups: ["deleted"],
        SystemConfigKey.BestVersionFilterRuleGroups: ["keep"],
        SystemConfigKey.DefaultMovieSubscribeConfig: {
            "quality": "WEB-DL",
            "filter_groups": ["deleted", "keep"],
        },
        SystemConfigKey.DefaultTvSubscribeConfig: {"filter_groups": ["deleted"]},
        SystemConfigKey.DefaultMusicSubscribeConfig: {},
    }

    class Config:
        """记录规则引用对账产生的系统配置更新。"""

        def get(self, key):
            """读取当前测试配置。"""
            return values.get(key)

        def set(self, key, value):
            """保存配置并更新测试快照。"""
            values[key] = value
            return True

    subscribes = [
        SimpleNamespace(
            id=1,
            name="Example",
            season=1,
            filter_groups=["deleted", "keep"],
        )
    ]
    updates = []
    subscribe_port = SimpleNamespace(
        list=lambda: subscribes,
        update=lambda subscribe_id, payload: updates.append((subscribe_id, payload)),
    )
    monkeypatch.setattr(subscribe_module, "_system_config", lambda: Config())
    monkeypatch.setattr(
        subscribe_module,
        "get_chain_subscribe_port",
        lambda: subscribe_port,
    )

    SubscribeChain.reconcile_rule_group_references(
        object.__new__(SubscribeChain),
        Event(
            EventType.ConfigChanged,
            ConfigChangeEventData(
                key=SystemConfigKey.UserFilterRuleGroups,
                value=[{"name": "keep", "rule_string": "4K"}],
            ),
        ),
    )

    assert values[SystemConfigKey.SearchFilterRuleGroups] == ["keep"]
    assert values[SystemConfigKey.SubscribeFilterRuleGroups] == []
    assert values[SystemConfigKey.BestVersionFilterRuleGroups] == ["keep"]
    assert values[SystemConfigKey.DefaultMovieSubscribeConfig] == {
        "quality": "WEB-DL",
        "filter_groups": ["keep"],
    }
    assert values[SystemConfigKey.DefaultTvSubscribeConfig] == {
        "filter_groups": [],
    }
    assert updates == [(1, {"filter_groups": ["keep"]})]
    assert subscribes[0].filter_groups == ["keep"]
