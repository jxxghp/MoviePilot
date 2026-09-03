"""媒体分类语义在 REST Schema 边界的兼容测试。"""

import pytest
from pydantic import ValidationError

from app.schemas.category import (
    ClassificationPolicy,
    ClassificationResult,
    ClassificationSelection,
)
from app.schemas.context import MediaInfo
from app.schemas.music import MusicAlbumInfo, MusicInfo


def _classification_result() -> ClassificationResult:
    """构造可用于序列化往返的最小分类结果。"""
    selection = ClassificationSelection(
        category_id="music.live",
        category_path=["现场专辑"],
        rule_id="rule.music.live",
        source="automatic",
    )
    return ClassificationResult(
        recommended=selection,
        effective=selection,
        labels=["现场"],
        policy_revision=12,
        state="complete",
    )


def test_video_legacy_category_is_promoted_to_library_category() -> None:
    """旧影视客户端只提交 category 时应无损迁移为媒体库分类。"""
    media = MediaInfo.model_validate(
        {
            "type": "电影",
            "category": "动画/电影",
        }
    )

    assert media.library_category == "动画/电影"
    assert media.category == "动画/电影"
    assert media.metadata_category == ""


def test_music_legacy_category_is_migrated_to_metadata_only() -> None:
    """旧音乐来源 category 不得被误认成媒体库目录分类。"""
    music = MusicInfo.model_validate(
        {
            "type": "音乐",
            "category": "Album / Live",
        }
    )

    assert music.metadata_category == "Album / Live"
    assert music.library_category == ""
    assert music.category == ""


def test_music_explicit_new_categories_keep_compatibility_field_on_library() -> None:
    """新音乐载荷中兼容 category 始终跟随 library_category。"""
    music = MusicInfo.model_validate(
        {
            "type": "音乐",
            "category": "旧值",
            "library_category": "现场专辑",
            "metadata_category": "Album / Live",
        }
    )

    assert music.library_category == "现场专辑"
    assert music.metadata_category == "Album / Live"
    assert music.category == "现场专辑"


def test_music_album_schema_uses_the_same_category_contract() -> None:
    """专辑 DTO 与单曲 DTO 必须采用相同的分类字段语义。"""
    album = MusicAlbumInfo.model_validate(
        {
            "type": "音乐",
            "category": "Album / Compilation",
        }
    )

    assert album.metadata_category == "Album / Compilation"
    assert album.library_category == ""
    assert album.category == ""


def test_classification_snapshot_round_trip_remains_strongly_typed() -> None:
    """分类快照经过 JSON 往返后仍应恢复为 ClassificationResult。"""
    music = MusicInfo(
        media_source="musicbrainz",
        media_id="recording-1",
        title="Example",
        library_category="现场专辑",
        metadata_category="Album / Live",
        classification=_classification_result(),
    )

    restored = MusicInfo.model_validate_json(music.model_dump_json())

    assert isinstance(restored.classification, ClassificationResult)
    assert restored.classification.policy_revision == 12
    assert restored.category == "现场专辑"
    assert restored.metadata_category == "Album / Live"


def test_legacy_policy_defaults_to_primary_only_enrichment_mode() -> None:
    """旧策略载荷缺少新字段时必须保持零跨源调用的兼容默认值。"""
    policy = ClassificationPolicy.model_validate(
        {
            "schema_version": 2,
            "revision": 1,
            "categories": [],
            "rules": [],
            "fallbacks": {},
        }
    )

    assert policy.enrichment_mode == "primary_only"
    assert policy.model_dump(mode="json")["enrichment_mode"] == "primary_only"


def test_policy_rejects_unknown_enrichment_mode() -> None:
    """未知补充模式必须在持久化和执行前由严格 schema 拒绝。"""
    with pytest.raises(ValidationError):
        ClassificationPolicy.model_validate(
            {
                "schema_version": 2,
                "revision": 1,
                "enrichment_mode": "always_replace",
                "categories": [],
                "rules": [],
                "fallbacks": {},
            }
        )
