"""分类持久化快照、订阅引用规范化与冻结恢复测试。"""

from app.application.classification.reference import (
    ClassificationCategoryResolver,
    apply_persisted_classification_snapshot,
    configure_classification_category_resolver,
    effective_classification_snapshot,
    normalize_classification_reference_payload,
    persisted_classification_snapshot,
    reset_classification_category_resolver,
)
from app.domain.context import MediaInfo, MusicInfo
from app.schemas.category import (
    ClassificationCategory,
    ClassificationPolicy,
    ClassificationResult,
    ClassificationSelection,
)
from app.schemas.types import MediaType


class _PolicyProvider:
    """提供可预测的活动分类策略。"""

    def active_policy(self) -> ClassificationPolicy:
        """返回包含改名后路径的测试策略。"""
        return ClassificationPolicy(
            revision=8,
            categories=[
                ClassificationCategory(
                    id="movie.favorite",
                    media_type="电影",
                    name="收藏",
                    path=["收藏", "电影"],
                )
            ],
        )


def test_effective_snapshot_uses_effective_selection_not_music_metadata() -> None:
    """历史投影只读取生效库分类，不得把音乐描述分类写入目录快照。"""
    media = MusicInfo(
        media_source="musicbrainz",
        media_id="recording-1",
        title="Track",
        metadata_category="Soundtrack",
        classification=ClassificationResult(
            recommended=ClassificationSelection(
                category_id="music.auto",
                category_path=["音乐", "自动"],
                rule_id="rule.auto",
                source="automatic",
            ),
            effective=ClassificationSelection(
                category_id="music.manual",
                category_path=["音乐", "收藏"],
                source="subscription",
            ),
            policy_revision=7,
            state="complete",
        ),
    )

    snapshot = effective_classification_snapshot(media)

    assert snapshot.category_id == "music.manual"
    assert snapshot.path == "音乐/收藏"
    assert snapshot.rule_id is None
    assert snapshot.policy_revision == 7
    assert snapshot.source == "subscription"


def test_recommended_only_result_is_not_persisted_as_effective_history() -> None:
    """仅有推荐结果时不得伪造已生效分类 ID、规则或 revision。"""
    media = MediaInfo(
        media_source="themoviedb",
        media_id="2",
        type=MediaType.MOVIE,
        title="Movie",
        classification=ClassificationResult(
            recommended=ClassificationSelection(
                category_id="movie.recommended",
                category_path=["推荐"],
                rule_id="rule.recommended",
                source="automatic",
            ),
            policy_revision=9,
            state="complete",
        ),
    )

    snapshot = effective_classification_snapshot(media)

    assert snapshot.category_id is None
    assert snapshot.rule_id is None
    assert snapshot.policy_revision is None
    assert snapshot.source is None


def test_persisted_snapshot_restores_historical_path_and_revision() -> None:
    """历史恢复必须覆盖当前自动结果，并保留保存时路径与策略版本。"""
    current = MediaInfo(
        media_source="themoviedb",
        media_id="1",
        type=MediaType.MOVIE,
        title="Movie",
        library_category="当前路径",
        classification=ClassificationResult(
            effective=ClassificationSelection(
                category_id="movie.current",
                category_path=["当前路径"],
                rule_id="rule.current",
                source="automatic",
            ),
            policy_revision=12,
            state="complete",
        ),
    )
    historical = persisted_classification_snapshot(
        category_id="movie.old",
        category_path="旧路径/子目录",
        rule_id="rule.old",
        policy_revision=3,
        source="automatic",
    )

    restored = apply_persisted_classification_snapshot(current, historical)

    assert restored is not current
    assert restored.library_category == "旧路径/子目录"
    assert restored.classification.policy_revision == 3
    assert restored.classification.effective.category_id == "movie.old"
    assert restored.classification.effective.rule_id == "rule.old"
    assert current.library_category == "当前路径"


def test_subscription_reference_uses_current_path_and_explicit_null_clears_pair() -> None:
    """新订阅按稳定 ID 刷新路径，显式清空 ID 时同时清空兼容路径。"""
    previous = configure_classification_category_resolver(
        ClassificationCategoryResolver(_PolicyProvider())
    )
    try:
        normalized = normalize_classification_reference_payload(
            {
                "media_category_id": "movie.favorite",
                "media_category": "旧收藏路径",
            },
            media_type=MediaType.MOVIE,
        )
        cleared = normalize_classification_reference_payload(
            {
                "media_category_id": None,
                "media_category": "不得残留",
            },
            media_type=MediaType.MOVIE,
        )
    finally:
        reset_classification_category_resolver(previous)

    assert normalized["media_category_id"] == "movie.favorite"
    assert normalized["media_category"] == "收藏/电影"
    assert cleared["media_category_id"] is None
    assert cleared["media_category"] is None


def test_invalid_legacy_history_path_is_not_restored() -> None:
    """不安全旧路径不能借历史兼容入口逃逸媒体库根目录。"""
    snapshot = persisted_classification_snapshot(category_path="音乐/../逃逸")

    assert snapshot.category_path == ()
    assert snapshot.selected is False
