"""完整识别结果分类执行与 Chain 收口验收测试。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Optional
from unittest.mock import AsyncMock, Mock, patch

from app.application.classification.execution import ClassificationExecutionService
from app.application.classification.legacy import migrate_legacy_category_config
from app.chain.base import ChainBase
from app.domain.context import MediaInfo, MusicArtistInfo, MusicInfo
from app.domain.metainfo import MetaInfo
from app.schemas.category import (
    CategoryConfig,
    CategoryRule,
    ClassificationFacts,
    ClassificationPolicy,
    ClassificationResult,
    ClassificationSelection,
)
from app.schemas.types import MediaSource, MediaType


class _Runtime:
    """提供可变活动策略与隔离 legacy 快照的测试端口。"""

    def __init__(
        self,
        policy: ClassificationPolicy | None,
        legacy: CategoryConfig | None = None,
    ) -> None:
        """保存测试需要的当前策略和旧配置。"""
        self.policy = policy
        self.legacy = legacy or CategoryConfig()

    def active_policy(self) -> ClassificationPolicy | None:
        """返回活动策略副本。"""
        return self.policy.model_copy(deep=True) if self.policy else None

    def legacy_config(self) -> CategoryConfig:
        """返回 legacy 配置副本。"""
        return self.legacy.model_copy(deep=True)


def _policy(*, revision: int = 7, movie_category: str = "movie.jp") -> ClassificationPolicy:
    """构造同时覆盖影视、音乐专辑和艺术家的分类策略。"""
    return ClassificationPolicy.model_validate(
        {
            "schema_version": 2,
            "revision": revision,
            "categories": [
                {
                    "id": movie_category,
                    "media_type": "电影",
                    "name": "日本动画",
                    "path": ["动画", "日本"],
                },
                {
                    "id": "movie.other",
                    "media_type": "电影",
                    "name": "其它电影",
                    "path": ["其它电影"],
                },
                {
                    "id": "tv.other",
                    "media_type": "电视剧",
                    "name": "其它剧集",
                    "path": ["其它剧集"],
                },
                {
                    "id": "music.live",
                    "media_type": "音乐",
                    "name": "现场音乐",
                    "path": ["音乐", "现场"],
                },
                {
                    "id": "music.artist",
                    "media_type": "音乐",
                    "name": "艺术家",
                    "path": ["音乐", "艺术家"],
                },
                {
                    "id": "music.other",
                    "media_type": "音乐",
                    "name": "其它音乐",
                    "path": ["其它音乐"],
                },
            ],
            "rules": [
                {
                    "id": "rule.movie.jp",
                    "name": "日本动画",
                    "kind": "category",
                    "media_types": ["电影"],
                    "when": {
                        "all": [
                            {
                                "field": "media.genre_keys",
                                "operator": "contains_any",
                                "value": ["animation"],
                            },
                            {
                                "field": "media.countries",
                                "operator": "contains_any",
                                "value": ["JP"],
                            },
                        ]
                    },
                    "target": {"category_id": movie_category},
                },
                {
                    "id": "rule.music.live",
                    "name": "现场音乐",
                    "kind": "category",
                    "media_types": ["音乐"],
                    "when": {
                        "all": [
                            {
                                "field": "music.secondary_types",
                                "operator": "contains_any",
                                "value": ["Live"],
                            }
                        ]
                    },
                    "target": {"category_id": "music.live"},
                },
                {
                    "id": "rule.music.artist",
                    "name": "艺术家",
                    "kind": "category",
                    "media_types": ["音乐"],
                    "when": {
                        "all": [
                            {
                                "field": "music.entity_type",
                                "operator": "equals",
                                "value": "artist",
                            }
                        ]
                    },
                    "target": {"category_id": "music.artist"},
                },
            ],
            "fallbacks": {
                "电影": "movie.other",
                "电视剧": "tv.other",
                "音乐": "music.other",
            },
        }
    )


def test_execution_classifies_copy_and_preserves_source_identity() -> None:
    """完整来源对象应被复制分类，主身份和来源对象不得被改写。"""
    source = MediaInfo(
        media_source="douban",
        media_id="1291561",
        type=MediaType.MOVIE,
        title="千与千寻",
        origin_country=["JP"],
        genres=[{"name": "动画"}],
        library_category="旧缓存分类",
        classification=ClassificationResult(
            policy_revision=1,
            state="complete",
        ),
    )

    finalized = ClassificationExecutionService(_Runtime(_policy())).finalize(source)

    assert finalized is not source
    assert finalized.media_source == MediaSource.Douban
    assert finalized.media_id == "1291561"
    assert finalized.library_category == "动画/日本"
    assert finalized.classification is not None
    assert finalized.classification.policy_revision == 7
    assert finalized.classification.effective.category_id == "movie.jp"
    assert source.library_category == "旧缓存分类"
    assert source.classification.policy_revision == 1


def test_execution_builds_complete_facts_without_mutating_media() -> None:
    """影响分析事实入口应复用插件字段构造，并保持原媒体对象不变。"""
    source = MediaInfo(
        media_source=MediaSource.Douban,
        media_id="native-1",
        type=MediaType.MOVIE,
        title="Example",
        genres=[{"name": "动画"}],
        origin_country=["JP"],
    )
    service = ClassificationExecutionService(
        _Runtime(_policy()),
        extension_facts_provider=lambda media: {
            "example.source": {"region_group": "east-asia"}
        },
    )

    facts = asyncio.run(service.async_build_facts(source))

    assert facts is not None
    assert facts.identity.media_id == "native-1"
    assert facts.media.genre_keys == ["animation"]
    assert facts.media.countries == ["JP"]
    assert facts.extensions["example.source"]["region_group"] == "east-asia"
    assert source.classification is None


def test_execution_reclassifies_cached_result_after_policy_revision_changes() -> None:
    """缓存对象携带旧 revision 时必须按当前策略重新分类。"""
    runtime = _Runtime(_policy(revision=7))
    service = ClassificationExecutionService(runtime)
    source = MediaInfo(
        media_source="douban",
        media_id="1",
        type=MediaType.MOVIE,
        origin_country=["JP"],
        genres=[{"name": "动画"}],
    )
    first = service.finalize(source)
    runtime.policy = _policy(revision=8, movie_category="movie.other")

    second = service.finalize(first)

    assert second.classification is not None
    assert second.classification.policy_revision == 8
    assert second.classification.effective.category_id == "movie.other"
    assert second.library_category == "其它电影"
    assert first.classification.policy_revision == 7


def test_execution_refreshes_same_revision_after_auxiliary_facts_change() -> None:
    """来源缓存恢复后即使 revision 相同，也必须按当前完整事实重新求值。"""
    service = ClassificationExecutionService(_Runtime(_policy()))
    source = MediaInfo(
        media_source="anilist",
        media_id="1",
        type=MediaType.MOVIE,
    )
    first = service.finalize(source)
    first.genre_ids = [16]
    first.origin_country = ["JP"]

    reclassified = service.finalize(first)
    refreshed = service.finalize(first, refresh=True)

    assert reclassified.library_category == "动画/日本"
    assert refreshed.library_category == "动画/日本"
    assert refreshed.classification is not None
    assert refreshed.classification.effective.rule_id == "rule.movie.jp"


def test_execution_applies_manual_effective_override_without_losing_recommendation() -> None:
    """订阅或目录人工覆盖只替换 effective，仍保留自动推荐供 UI 解释。"""
    source = MediaInfo(
        media_source="douban",
        media_id="1",
        type=MediaType.MOVIE,
        origin_country=["JP"],
        genres=[{"name": "动画"}],
    )
    override = ClassificationSelection(
        category_path=["收藏", "家庭电影"],
        source="subscription",
    )

    finalized = ClassificationExecutionService(_Runtime(_policy())).finalize(
        source,
        effective_override=override,
    )

    assert finalized.library_category == "收藏/家庭电影"
    assert finalized.classification is not None
    assert finalized.classification.recommended.category_id == "movie.jp"
    assert finalized.classification.effective.source == "subscription"
    assert finalized.classification.effective.category_path == ["收藏", "家庭电影"]


def test_execution_preserves_subscription_override_during_auxiliary_refresh() -> None:
    """补充来源事实再次分类时必须保留已装配的订阅人工覆盖。"""
    service = ClassificationExecutionService(_Runtime(_policy()))
    source = MediaInfo(
        media_source="douban",
        media_id="1",
        type=MediaType.MOVIE,
        origin_country=["JP"],
        genres=[{"name": "动画"}],
    )
    overridden = service.finalize(
        source,
        effective_override=ClassificationSelection(
            category_id="movie.manual",
            category_path=["收藏", "家庭电影"],
            source="subscription",
        ),
    )

    refreshed = service.finalize(overridden, refresh=True)

    assert refreshed.classification.recommended.category_id == "movie.jp"
    assert refreshed.classification.effective.category_id == "movie.manual"
    assert refreshed.classification.effective.source == "subscription"
    assert refreshed.library_category == "收藏/家庭电影"


class _StaticEnrichment:
    """为同步和异步收口补充同一组国家事实。"""

    def __init__(self) -> None:
        """创建空调用记录。"""
        self.calls: list[str] = []

    @staticmethod
    def _enrich(facts: ClassificationFacts) -> ClassificationFacts:
        """复制事实并填入当前缺失的国家。"""
        enriched = facts.model_copy(deep=True)
        enriched.media.countries = ["JP"]
        return enriched

    def enrich(
        self,
        policy: ClassificationPolicy,
        facts: ClassificationFacts,
        media: object,
    ) -> ClassificationFacts:
        """记录同步调用并返回补充事实。"""
        del policy, media
        self.calls.append("sync")
        return self._enrich(facts)

    async def async_enrich(
        self,
        policy: ClassificationPolicy,
        facts: ClassificationFacts,
        media: object,
    ) -> ClassificationFacts:
        """记录异步调用并返回同一补充事实。"""
        del policy, media
        self.calls.append("async")
        return self._enrich(facts)


def test_execution_sync_and_async_apply_the_same_enriched_facts() -> None:
    """同步和异步执行服务必须在同一求值边界消费补充事实。"""
    source = MediaInfo(
        media_source=MediaSource.Douban,
        media_id="1291561",
        type=MediaType.MOVIE,
        title="千与千寻",
        genres=[{"name": "动画"}],
    )
    enrichment = _StaticEnrichment()
    service = ClassificationExecutionService(
        _Runtime(_policy()),
        enrichment=enrichment,
    )

    sync_result = service.finalize(source)
    async_result = asyncio.run(service.async_finalize(source))

    assert enrichment.calls == ["sync", "async"]
    assert sync_result.classification == async_result.classification
    assert sync_result.library_category == async_result.library_category == "动画/日本"
    assert sync_result.media_source == async_result.media_source == MediaSource.Douban
    assert sync_result.media_id == async_result.media_id == "1291561"
    assert source.library_category == ""


def test_music_metadata_category_is_never_promoted_without_a_rule() -> None:
    """音乐来源描述分类只能形成事实，目录分类必须来自规则或兜底。"""
    music = MusicInfo(
        media_source="musicbrainz",
        media_id="recording-1",
        title="Live Recording",
        metadata_category="Album / Live",
        secondary_types=["Live"],
    )

    finalized = ClassificationExecutionService(_Runtime(_policy())).finalize(music)

    assert finalized.metadata_category == "Album / Live"
    assert finalized.library_category == "音乐/现场"
    assert finalized.classification.effective.rule_id == "rule.music.live"
    assert music.library_category == ""


def test_artist_detail_uses_music_entity_facts() -> None:
    """完整艺术家详情应支持 music.entity_type 分类并保留描述类型。"""
    artist = MusicArtistInfo(
        media_source="musicbrainz",
        media_id="artist-1",
        name="Example Band",
        artist_type="Group",
    )

    finalized = ClassificationExecutionService(_Runtime(_policy())).finalize(artist)

    assert finalized.library_category == "音乐/艺术家"
    assert finalized.metadata_category == "Group"
    assert finalized.classification is not None
    assert finalized.classification.effective.rule_id == "rule.music.artist"


def test_incomplete_identity_remains_not_evaluated() -> None:
    """本地音乐兜底缺少远端身份时不得伪造已完成分类。"""
    music = MusicInfo(
        title="Local Track",
        metadata_category="Rock",
    )

    finalized = ClassificationExecutionService(_Runtime(_policy())).finalize(music)

    assert finalized.classification is not None
    assert finalized.classification.state == "not_evaluated"
    assert finalized.classification.policy_revision == 7
    assert finalized.library_category == ""
    assert finalized.metadata_category == "Rock"


def test_active_legacy_policy_builds_tmdb_extension_facts_at_execution() -> None:
    """迁移策略使用的 TMDB 动态字段应在收口点从受控原始详情构造。"""
    migration = migrate_legacy_category_config(
        CategoryConfig(
            movie={"特别电影": CategoryRule(custom_flag="yes")},
        )
    )
    assert migration.valid is True
    media = MediaInfo(
        media_source="themoviedb",
        media_id="10",
        type=MediaType.MOVIE,
        tmdb_info={"id": 10, "media_type": "movie", "custom_flag": "yes"},
    )

    finalized = ClassificationExecutionService(
        _Runtime(migration.policy)
    ).finalize(media)

    assert finalized.library_category == "特别电影"
    assert finalized.classification is not None
    assert finalized.classification.effective.rule_id is not None


def test_invalid_policy_uses_read_only_legacy_tmdb_fallback() -> None:
    """策略不可用时只对 TMDB 恢复旧分类，并明确标记 invalid_policy。"""
    legacy = CategoryConfig(
        movie={
            "日本动画": CategoryRule(
                genre_ids="16",
                production_countries="JP",
            )
        }
    )
    source = MediaInfo(
        media_source="themoviedb",
        media_id="20",
        type=MediaType.MOVIE,
        tmdb_info={
            "id": 20,
            "media_type": "movie",
            "genre_ids": [16],
            "production_countries": [{"iso_3166_1": "JP"}],
        },
    )

    finalized = ClassificationExecutionService(_Runtime(None, legacy)).finalize(source)

    assert finalized.library_category == "日本动画"
    assert finalized.classification is not None
    assert finalized.classification.state == "invalid_policy"
    assert finalized.classification.effective.source == "legacy"


class _OrderingClassificationService:
    """记录共享上报与分类收口顺序的测试执行服务。"""

    def __init__(self, events: list[str]) -> None:
        """保存共享顺序记录列表。"""
        self.events = events
        self.calls: list[MediaInfo] = []

    def finalize(
        self,
        media: MediaInfo,
        *,
        effective_override: ClassificationSelection | None = None,
        refresh: bool = False,
    ) -> MediaInfo:
        """复制候选并写入可观察分类。"""
        del effective_override, refresh
        self.events.append("finalize")
        self.calls.append(media)
        result = deepcopy(media)
        result.set_library_category("已分类")
        return result

    async def async_finalize(
        self,
        media: MediaInfo,
        *,
        effective_override: ClassificationSelection | None = None,
        refresh: bool = False,
    ) -> MediaInfo:
        """记录异步分类收口，并返回独立结果。"""
        del effective_override, refresh
        self.events.append("async_finalize")
        self.calls.append(media)
        result = deepcopy(media)
        result.set_library_category("已分类")
        return result


def _keep_candidate(**kwargs: object) -> Optional[MediaInfo]:
    """保持原生候选不变，隔离插件补充分支。"""
    value = kwargs.get("mediainfo")
    return value if isinstance(value, MediaInfo) else None


async def _async_keep_candidate(**kwargs: object) -> Optional[MediaInfo]:
    """异步保持原生候选不变。"""
    return _keep_candidate(**kwargs)


def test_sync_recognition_finalizes_after_share_report() -> None:
    """同步完整识别应先完成共享上报，再通过应用服务分类返回副本。"""
    chain = ChainBase()
    events: list[str] = []
    classifier = _OrderingClassificationService(events)
    chain.classification_service = classifier
    candidate = MediaInfo(
        media_source="themoviedb",
        media_id="30",
        type=MediaType.MOVIE,
        title="Example",
    )
    share = Mock()
    share.report_recognize_share.side_effect = lambda **_kwargs: events.append("report")

    with patch.object(chain, "_run_native_media_recognize", return_value=candidate), patch.object(
        chain,
        "_supplement_media_recognize",
        side_effect=_keep_candidate,
    ), patch("app.chain._recognition._recognition_share_snapshot", return_value=share):
        result = chain.recognize_media(meta=MetaInfo("Example"), cache=False)

    assert events == ["report", "finalize"]
    assert classifier.calls == [candidate]
    assert isinstance(result, MediaInfo)
    assert result is not candidate
    assert result.library_category == "已分类"


def test_async_recognition_uses_the_same_finalize_boundary() -> None:
    """异步完整识别应调用同一应用服务的有界异步分类收口。"""
    chain = ChainBase()
    events: list[str] = []
    classifier = _OrderingClassificationService(events)
    chain.classification_service = classifier
    candidate = MediaInfo(
        media_source="douban",
        media_id="40",
        type=MediaType.TV,
        title="Example TV",
    )
    share = Mock()
    share.async_report_recognize_share = AsyncMock(
        side_effect=lambda **_kwargs: events.append("report")
    )

    async def exercise() -> MediaInfo | None:
        """执行异步识别并返回分类后的媒体。"""
        with patch.object(
            chain,
            "_async_run_native_media_recognize",
            AsyncMock(return_value=candidate),
        ), patch.object(
            chain,
            "_async_supplement_media_recognize",
            side_effect=_async_keep_candidate,
        ), patch(
            "app.chain._recognition._recognition_share_snapshot",
            return_value=share,
        ):
            return await chain.async_recognize_media(
                meta=MetaInfo("Example TV"),
                cache=False,
            )

    result = asyncio.run(exercise())

    assert events == ["report", "async_finalize"]
    assert classifier.calls == [candidate]
    assert isinstance(result, MediaInfo)
    assert result is not candidate
    assert result.library_category == "已分类"
