"""媒体分类字段与来源能力目录测试。"""

from app.application.classification.catalog import (
    build_classification_field_catalog,
    build_retired_classification_field_catalog,
)
from app.domain.classification.fields import get_standard_classification_fields
from app.domain.classification.sources import (
    BUILTIN_CLASSIFICATION_SOURCES,
    STANDARD_CLASSIFICATION_FIELD_IDS,
    builtin_source_field_support,
)
from app.schemas.category import ClassificationFieldDefinition
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


def test_standard_catalog_uses_user_facing_country_and_genre_names() -> None:
    """通用标准字段应直接使用用户理解的国家和风格名称。"""
    fields = {
        definition.id: definition
        for definition in build_classification_field_catalog()
    }

    assert fields["media.countries"].label == "原产国家/地区"
    assert fields["media.genre_keys"].label == "风格"
    assert fields["media.genre_names"].label == "来源风格"


def test_catalog_separates_retired_fields_from_new_rule_options() -> None:
    """迁移专用字段只能解析已有规则，插件正常扩展字段仍可新增。"""
    retired = ClassificationFieldDefinition(
        id="extensions.themoviedb.origin_country",
        label="原产国家/地区（旧规则）",
        value_type="string_list",
        media_types=["电视剧"],
        selectable=False,
        replacement_field="media.countries",
    )
    plugin = ClassificationFieldDefinition(
        id="extensions.example.region",
        label="地区",
        value_type="string",
        media_types=["电影"],
    )

    selectable_ids = {
        field.id
        for field in build_classification_field_catalog((retired, plugin))
    }
    retired_fields = build_retired_classification_field_catalog((retired, plugin))

    assert "extensions.example.region" in selectable_ids
    assert retired.id not in selectable_ids
    assert [field.id for field in retired_fields] == [retired.id]
    assert retired_fields[0].replacement_field == "media.countries"


def test_plugin_sources_have_no_builtin_capability_declarations() -> None:
    """无宿主模块的来源不得预占插件标识或预设不可用的字段能力。"""
    discover_sources = {
        "bilibili", "mangguodiscover", "migu", "tencentvideodiscover", "iqiyidiscover",
    }

    assert discover_sources.isdisjoint(BUILTIN_CLASSIFICATION_SOURCES)
    assert discover_sources.isdisjoint(source.value for source in MediaSource)
    for field in get_standard_classification_fields():
        assert discover_sources.isdisjoint(field.source_support)


def test_catalog_dictionary_values_match_normalized_facts() -> None:
    """中文标签对应规则实际读取的稳定值，规范风格词表与事实映射保持一致。"""
    from app.domain.classification.vocabulary import GENRE_KEY_ALIASES, TMDB_GENRE_KEYS

    fields = {item.id: item for item in build_classification_field_catalog()}
    countries = {item.value: item.label for item in fields["media.countries"].options}
    assert len(countries) == 249
    assert countries["JP"] == "日本"
    assert countries["KR"] == "韩国"
    assert {item.value for item in fields["media.genre_keys"].options} == set(GENRE_KEY_ALIASES.values()) | set(
        TMDB_GENRE_KEYS.values()
    )
    assert any(item.value == "ja" and item.label == "日语" for item in fields["media.language"].options)
    assert fields["media.countries"].allow_custom_values
    assert fields["media.genre_keys"].allow_custom_values


def test_catalog_source_candidates_preserve_original_values_and_are_isolated() -> None:
    """来源风格只作为开放候选，序列化保留来源和原值且不共享可变状态。"""
    fields = {item.id: item for item in build_classification_field_catalog()}
    genres = fields["media.genre_names"]
    assert genres.allow_custom_values
    assert any(item.value == "动画" for item in genres.source_options["douban"])
    assert any(item.value == "Action" for item in genres.source_options["anilist"])
    assert "bangumi" not in genres.source_options
    assert genres.model_dump()["source_options"]["anilist"][0]["value"] == "Action"
    genres.source_options["anilist"].clear()
    fresh = {item.id: item for item in build_classification_field_catalog()}
    assert fresh["media.genre_names"].source_options["anilist"]
