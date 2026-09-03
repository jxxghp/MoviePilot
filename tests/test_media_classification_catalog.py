"""媒体分类字段与来源能力目录测试。"""

from app.application.classification.catalog import build_classification_field_catalog
from app.domain.classification.fields import get_standard_classification_fields
from app.domain.classification.sources import (
    BUILTIN_CLASSIFICATION_SOURCES,
    STANDARD_CLASSIFICATION_FIELD_IDS,
    builtin_source_field_support,
)
from app.schemas.types import MediaSource


def test_standard_fields_declare_every_builtin_source_explicitly() -> None:
    """每个标准字段都必须显式声明九个内置来源的支持或不可用状态。"""
    expected_sources = set(BUILTIN_CLASSIFICATION_SOURCES)

    definitions = get_standard_classification_fields()
    assert tuple(definition.id for definition in definitions) == STANDARD_CLASSIFICATION_FIELD_IDS
    for definition in definitions:
        assert set(definition.source_support) == expected_sources


def test_builtin_source_capabilities_return_isolated_copies() -> None:
    """调用方修改单来源能力快照时不能污染全局目录。"""
    first = builtin_source_field_support(MediaSource.TMDB.value)
    first["media.year"] = "unavailable"

    second = builtin_source_field_support(MediaSource.TMDB.value)

    assert second["media.year"] == "derived"


def test_catalog_keeps_undeclared_dynamic_source_absent() -> None:
    """插件未声明标准字段能力时必须保持键缺失，而不是断言不可用。"""
    fields = build_classification_field_catalog()

    assert fields
    assert all(
        "example.source" not in definition.source_support
        for definition in fields
    )


def test_catalog_preserves_verified_builtin_capability_levels() -> None:
    """应用层合并注册来源时不得覆盖领域层已验证的内置能力。"""
    fields = {
        definition.id: definition
        for definition in build_classification_field_catalog()
    }

    assert fields["media.content_rating"].source_support["themoviedb"] == "partial"
    assert fields["music.secondary_types"].source_support["musicbrainz"] == "partial"
    assert fields["music.secondary_types"].source_support["theaudiodb"] == "unavailable"


def test_discover_only_builtin_sources_are_explicitly_unavailable() -> None:
    """仅提供发现入口的内置来源不能被误认为可形成分类事实。"""
    discover_sources = {
        MediaSource.Bilibili.value,
        MediaSource.MangoTV.value,
        MediaSource.MiguVideo.value,
        MediaSource.TencentVideo.value,
        MediaSource.Iqiyi.value,
    }

    for media_source in discover_sources:
        assert set(builtin_source_field_support(media_source).values()) == {
            "unavailable"
        }
