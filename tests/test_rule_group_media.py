"""
验证过滤规则组与媒体类型的匹配范围。
"""

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from app.application.classification.execution import ClassificationExecutionService
from app.application.classification.reference import (
    ClassificationCategoryResolver,
    configure_classification_category_resolver,
    reset_classification_category_resolver,
)
from app.application.classification.runtime import ClassificationRuntime
from app.application.filtering import normalize_media_type
from app.application.rules import BUILTIN_RULE_SET, RuleHelper

# 由惰性稳定入口导出，Pylint 无法静态解析。
# pylint: disable=no-name-in-module  # SubscribeChain
from app.chain.subscribe import SubscribeChain
from app.domain.context import MediaInfo, MusicInfo, TorrentInfo
from app.modules.filter import FilterModule
from app.runtime.events import Event
from app.schemas.category import ClassificationCategory, ClassificationPolicy, ClassificationSelection
from app.schemas.event import ConfigChangeEventData
from app.schemas.rule import FilterRuleGroup
from app.schemas.types import EventType, MediaType, SystemConfigKey


@pytest.fixture
def named_classification() -> Iterator[ClassificationRuntime]:
    """
    装配名称与目录不同的分类策略，并恢复进程级分类解析器。
    """
    policy = ClassificationPolicy(
        revision=1,
        categories=[
            ClassificationCategory(id="movie.default", media_type="电影", name="电影", path=["Movie"]),
            ClassificationCategory(id="tv.default", media_type="电视剧", name="电视剧", path=["TV"]),
            ClassificationCategory(id="tv.favorite", media_type="电视剧", name="收藏剧集", path=["TV", "Favorites"]),
            ClassificationCategory(id="tv.disabled", media_type="电视剧", name="停用剧集", path=["Disabled"], enabled=False),
            ClassificationCategory(id="music.default", media_type="音乐", name="专辑", path=["Music"]),
        ],
        fallbacks={"电影": "movie.default", "电视剧": "tv.default", "音乐": "music.default"},
    )
    service = MagicMock()
    service.active.return_value = policy
    runtime = ClassificationRuntime(service)
    previous = configure_classification_category_resolver(ClassificationCategoryResolver(runtime))
    try:
        yield runtime
    finally:
        reset_classification_category_resolver(previous)


@pytest.mark.parametrize("media_type", [MediaType.MOVIE, MediaType.TV, MediaType.MUSIC])
def test_rule_group_matches_category_name_from_api(named_classification, monkeypatch, media_type) -> None:
    """
    分类接口提供的显示名称应匹配真实分类结果，不能直接与目录路径比较。
    """
    source = (
        MusicInfo(title="Album", media_source="musicbrainz", media_id="release-1")
        if media_type == MediaType.MUSIC
        else MediaInfo(type=media_type, title="Show", media_source="themoviedb", media_id="300146")
    )
    media = ClassificationExecutionService(named_classification).finalize(source, allow_enrichment=False)
    category = named_classification.media_categories().root[media_type.value][0]
    assert category != media.category
    helper = RuleHelper()
    group = FilterRuleGroup(name="named", rule_string="RULE1", media_type=media_type.value, category=category)
    monkeypatch.setattr(helper, "get_rule_groups", lambda: [group])

    assert helper.get_rule_group_by_media(media=media, group_names=["named"]) == [group]


@pytest.mark.parametrize("use_rust", [False, True], ids=["python", "rust"])
def test_named_tv_category_enforces_episode_size(named_classification, monkeypatch, use_rust) -> None:
    """
    Issue 7139 的分类名称必须使单集和多集体积限制实际进入两种过滤引擎。
    """
    media = ClassificationExecutionService(named_classification).finalize(
        MediaInfo(type=MediaType.TV, title="嫁金钗", media_source="themoviedb", media_id="300146", number_of_episodes=30),
        allow_enrichment=False,
    )
    assert media.category == "TV"
    helper = RuleHelper()
    group = FilterRuleGroup(
        name="电视剧规则", rule_string="1080P & !DOLBY & !ATMOS & !HDR & RULE1",
        media_type="电视剧", category="电视剧",
    )
    monkeypatch.setattr(helper, "get_rule_groups", lambda: [group])
    module = FilterModule()
    module.rulehelper = helper
    module.rule_set = {**BUILTIN_RULE_SET, "RULE1": {"size_range": "0-1024"}}
    monkeypatch.setattr("app.modules.filter.rust_accel.is_enabled", lambda: use_rust)
    engine = MagicMock(wraps=module._FilterModule__filter_torrents_with_python)
    monkeypatch.setattr(module, "_FilterModule__filter_torrents_with_python", engine)
    oversized = [
        TorrentInfo(
            title=f"Where the Mask Ends S01 {episodes} 2026 1080p NF WEB-DL x264 AAC2.0-HHWEB",
            description=f"嫁金钗 | 第{description}集 |1080p | 类型: 剧情/爱情/古装",
            size=int(gib * 1024**3),
        )
        for episodes, description, gib in [
            ("E16", "16", 2.14), ("E07-E08", "07-08", 3.74),
            ("E09-E10", "09-10", 3.60), ("E11-E12", "11-12", 3.57),
            ("E01-E04", "01-04", 7.32), ("", "", 1.88),
        ]
    ]
    accepted = [
        TorrentInfo(title="Where the Mask Ends S01E13 1080p WEB-DL H.264-MWeb", size=508 * 1024**2),
        TorrentInfo(title="Where the Mask Ends S01E01-E02 1080p WEB-DL H.264", size=2 * 1024**3),
    ]

    result = module.filter_torrents(["电视剧规则"], oversized + accepted, media)

    assert result == accepted
    assert all(torrent.pri_order == 100 for torrent in result)
    assert engine.call_count == (0 if use_rust else 1)


@pytest.mark.parametrize("category", ["收藏剧集", "TV/Favorites"])
def test_rule_group_uses_effective_category_override(named_classification, monkeypatch, category) -> None:
    """
    人工生效分类优先于自动推荐，旧目录路径规则继续兼容。
    """
    media = ClassificationExecutionService(named_classification).finalize(
        MediaInfo(type=MediaType.TV, title="Show", media_source="themoviedb", media_id="300146"),
        effective_override=ClassificationSelection(
            category_id="tv.favorite", category_path=["TV", "Favorites"], source="subscription",
        ),
        allow_enrichment=False,
    )
    helper = RuleHelper()
    groups = [
        FilterRuleGroup(name="effective", media_type="电视剧", category=category),
        FilterRuleGroup(name="recommended", media_type="电视剧", category="电视剧"),
        FilterRuleGroup(name="other-type", media_type="电影", category=category),
    ]
    monkeypatch.setattr(helper, "get_rule_groups", lambda: groups)

    assert helper.get_rule_group_by_media(media=media) == groups[:1]


@pytest.mark.parametrize("category", ["停用剧集", "其他剧集"])
def test_named_category_does_not_match_disabled_or_unrelated_category(named_classification, monkeypatch, category) -> None:
    """
    名称解析不能放宽到已停用或与媒体生效分类无关的规则组。
    """
    media = ClassificationExecutionService(named_classification).finalize(
        MediaInfo(type=MediaType.TV, title="Show", media_source="themoviedb", media_id="300146"), allow_enrichment=False,
    )
    helper = RuleHelper()
    group = FilterRuleGroup(name="excluded", media_type="电视剧", category=category)
    monkeypatch.setattr(helper, "get_rule_groups", lambda: [group])

    assert helper.get_rule_group_by_media(media=media) == []


@pytest.mark.parametrize("category, expected", [("电视剧", True), ("TV", True), ("收藏剧集", False)])
def test_named_category_matches_legacy_media_path(named_classification, monkeypatch, category, expected) -> None:
    """
    旧媒体缺少稳定分类 ID 时，仍能按其目录路径关联当前分类名称。
    """
    assert named_classification.active_policy() is not None
    helper = RuleHelper()
    group = FilterRuleGroup(name="legacy", media_type="电视剧", category=category)
    monkeypatch.setattr(helper, "get_rule_groups", lambda: [group])

    matched = helper.get_rule_group_by_media(media=MediaInfo(type=MediaType.TV, category="TV"))

    assert matched == ([group] if expected else [])


def test_agent_rule_group_media_type_accepts_music_aliases():
    """
    Agent 规则组写入入口应统一接受中英文音乐类型。
    """
    assert normalize_media_type("music") == MediaType.MUSIC.value
    assert normalize_media_type("音乐") == MediaType.MUSIC.value


def test_music_rule_group_matches_music_media(monkeypatch):
    """
    音乐规则组应在音乐搜索和订阅的过滤上下文中生效。
    """
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
    """
    音乐专属规则组被选中后应实际过滤音乐资源。
    """
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
    """
    二级分类相同时仍必须先匹配规则组的主媒体类型。
    """
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
    """
    规则组事件必须委托单一原子服务处理全部引用。
    """
    mutation = MagicMock()

    @contextmanager
    def mutation_scope():
        """
        提供可观测的同步规则组事务作用域。
        """
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
    """
    站点删除事件不得分别修改 SystemConfig 和订阅仓储。
    """
    mutation = MagicMock()

    @contextmanager
    def mutation_scope():
        """
        提供可观测的同步站点引用事务作用域。
        """
        yield mutation

    chain = object.__new__(SubscribeChain)
    chain.site_reference_mutation_scope = mutation_scope

    SubscribeChain.remove_site(
        chain,
        Event(EventType.SiteDeleted, {"site_id": 3}),
    )

    mutation.apply.assert_called_once_with(3)


def test_rule_applicability_explains_missing_untyped_and_wrong_media_groups(monkeypatch) -> None:
    """
    诊断复用实际选择策略，缺失规则与范围不符都有稳定原因且不拒绝资源。
    """
    helper = RuleHelper()
    groups = [FilterRuleGroup(name="all", rule_string="1080P"),
              FilterRuleGroup(name="movie", rule_string="1080P", media_type="电影")]
    monkeypatch.setattr(helper, "get_rule_groups", lambda: groups)
    result = helper.explain_rule_groups(media=None, group_names=["all", "movie", "missing"])
    assert [(item.name, item.status, item.reason) for item in result] == [
        ("all", "effective", None), ("movie", "skipped", "rule_group_not_applicable"),
        ("missing", "skipped", "rule_group_missing"),
    ]
    assert helper.explain_rule_groups(media=None, group_names=[]) == []
    movie = MediaInfo(type=MediaType.MOVIE, title="Example")
    assert helper.explain_rule_groups(media=movie, group_names=["movie"])[0].status == "effective"
    assert helper.explain_rule_groups(media=movie, group_names=["all", "all"])[0].name == "all"
