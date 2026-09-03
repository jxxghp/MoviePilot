"""旧版 TMDB 分类配置到新版策略的纯迁移与兼容投影测试。"""

from collections.abc import Mapping
from typing import Any, Optional, cast

import pytest

from app.application.classification.legacy import (
    LegacyClassificationMigrationResult,
    build_legacy_tmdb_extension_facts,
    legacy_extension_fields_from_policy,
    migrate_legacy_category_config,
    project_policy_to_legacy_category_config,
    project_policy_to_legacy_category_projection,
)
from app.domain.classification.evaluator import ClassificationEvaluator
from app.domain.classification.facts import build_classification_facts
from app.domain.classification.validation import ClassificationPolicyValidator
from app.domain.context import MediaInfo
from app.schemas.category import (
    CategoryConfig,
    ClassificationCondition,
    ClassificationConditionNode,
    ClassificationFacts,
    ClassificationMediaType,
)
from app.schemas.types import MediaType


def _legacy_tmdb_category(  # noqa: C901, PLR0912
    categories: Mapping[str, Optional[Mapping[str, str]]],
    tmdb_info: Mapping[str, object],
) -> str:
    """冻结旧 CategoryHelper 求值算法，仅用于证明迁移前后语义等价。"""
    if not tmdb_info or not categories:
        return ""
    for name, conditions in categories.items():
        if not conditions:
            return name
        matched = True
        for attribute, configured_value in conditions.items():
            if not configured_value:
                continue
            if attribute == "release_year":
                raw_value = tmdb_info.get("release_date") or tmdb_info.get("first_air_date")
                info_value = str(raw_value)[:4] if raw_value else None
            else:
                info_value = tmdb_info.get(attribute)
            if not info_value:
                matched = False
                continue
            if attribute == "production_countries":
                info_values = [
                    str(country.get("iso_3166_1")).upper() for country in cast(list[Mapping[str, object]], info_value)
                ]
            elif isinstance(info_value, list):
                info_values = [str(value).upper() for value in info_value]
            else:
                info_values = [str(info_value).upper()]

            raw_values = [value for value in configured_value.split(",") if value]
            expanded_values: list[str] = []
            for value in raw_values:
                if "-" not in value:
                    expanded_values.append(value)
                    continue
                value_begin, value_end = value.split("-", 1)
                prefix = ""
                if value_begin.startswith("!"):
                    prefix = "!"
                    value_begin = value_begin[1:]
                if value_begin.isdigit() and value_end.isdigit():
                    expanded_values.extend(f"{prefix}{item}" for item in range(int(value_begin), int(value_end) + 1))
                else:
                    expanded_values.extend([f"{prefix}{value_begin}", f"{prefix}{value_end}"])

            values = [value.upper() for value in expanded_values]
            inverted_values = [value[1:] for value in values if value.startswith("!")]
            positive_values = [value for value in values if not value.startswith("!")]
            if positive_values and not set(positive_values).intersection(info_values):
                matched = False
            if inverted_values and set(inverted_values).intersection(info_values):
                matched = False
        if matched:
            return name
    return ""


def _legacy_config() -> dict[str, dict[str, Optional[dict[str, str]]]]:
    """构造覆盖默认式顺序、来源字段和空兜底的旧配置。"""
    return {
        "movie": {
            "动画电影": {"genre_ids": "16"},
            "华语电影": {"original_language": "zh,cn,bo,za"},
            "美国电影": {"production_countries": "US"},
            "外语电影": None,
        },
        "tv": {
            "国漫": {"genre_ids": "16", "origin_country": "CN,TW,HK"},
            "日番": {"genre_ids": "16", "origin_country": "JP"},
            "未分类": None,
        },
    }


def _media_enum(media_type: ClassificationMediaType) -> MediaType:
    """把分类媒体类型转换为领域媒体枚举。"""
    return MediaType.MOVIE if media_type == "电影" else MediaType.TV


def _year_from_tmdb(tmdb_info: Mapping[str, object]) -> Optional[str]:
    """按旧优先级从 TMDB 详情提取年份文本。"""
    release = tmdb_info.get("release_date") or tmdb_info.get("first_air_date")
    return str(release)[:4] if release else None


def _tmdb_facts(
    result: LegacyClassificationMigrationResult,
    tmdb_info: Mapping[str, object],
    media_type: ClassificationMediaType,
) -> ClassificationFacts:
    """用真实标准事实构造器和迁移扩展投影组装 TMDB 分类事实。"""
    media = MediaInfo(
        media_source="themoviedb",
        media_id=str(tmdb_info.get("id") or "fixture"),
        type=_media_enum(media_type),
        title=str(tmdb_info.get("title") or tmdb_info.get("name") or "Fixture"),
        year=_year_from_tmdb(tmdb_info),
        genre_ids=cast(list[int], tmdb_info.get("genre_ids") or []),
        original_language=cast(Optional[str], tmdb_info.get("original_language")),
        origin_country=cast(Optional[list[str]], tmdb_info.get("origin_country")),
        production_countries=cast(
            Optional[list[dict[str, Any]]],
            tmdb_info.get("production_countries"),
        ),
        adult=cast(Optional[bool], tmdb_info.get("adult")),
        runtime=cast(Optional[int], tmdb_info.get("runtime")),
    )
    return build_classification_facts(
        media,
        extensions=build_legacy_tmdb_extension_facts(result.policy, tmdb_info),
    )


def _category_name(
    result: LegacyClassificationMigrationResult,
    facts: ClassificationFacts,
) -> str:
    """执行新版求值并把稳定分类 ID 还原为目录分类名称。"""
    evaluation = ClassificationEvaluator.evaluate(result.policy, facts)
    category_id = evaluation.result.recommended.category_id
    categories: dict[str, str] = {str(category.id): str(category.name) for category in result.policy.categories}
    return categories.get(category_id or "", "")


def _leaf_fields(node: ClassificationConditionNode) -> list[str]:
    """按条件树顺序提取叶子字段。"""
    if isinstance(node, ClassificationCondition):
        return [node.field]
    children: list[ClassificationConditionNode]
    if node.all is not None:
        children = node.all
    elif node.any is not None:
        children = node.any
    elif node.not_ is not None:
        children = [node.not_]
    else:
        children = []
    return [field for child in children for field in _leaf_fields(child)]


def test_default_style_config_preserves_order_and_uses_safe_standard_fields() -> None:
    """默认式配置应保持顺序，已知风格使用标准字段，其余字段保持等价。"""
    result = migrate_legacy_category_config(_legacy_config())

    assert result.valid
    assert isinstance(result, LegacyClassificationMigrationResult)
    assert not result.issues
    assert [category.name for category in result.policy.categories if category.id.startswith("legacy.movie.")] == [
        "动画电影",
        "华语电影",
        "美国电影",
        "外语电影",
    ]
    assert [rule.name for rule in result.policy.rules if rule.media_types == ["电影"]] == [
        "动画电影",
        "华语电影",
        "美国电影",
    ]
    all_fields = [field for rule in result.policy.rules for field in _leaf_fields(rule.when)]
    assert "media.genre_keys" in all_fields
    assert "extensions.themoviedb.genre_ids" not in all_fields
    assert "media.language" not in all_fields
    assert "media.countries" not in all_fields
    assert "extensions.themoviedb.original_language" in all_fields
    assert "extensions.themoviedb.production_countries" in all_fields
    assert "extensions.themoviedb.origin_country" in all_fields
    assert legacy_extension_fields_from_policy(result.policy) == result.extra_fields
    assert ClassificationPolicyValidator.validate(result.policy, result.extra_fields).valid
    origin_country = next(
        field
        for field in result.extra_fields
        if field.id == "extensions.themoviedb.origin_country"
    )
    assert origin_country.label == "原产国家/地区（旧规则）"
    assert origin_country.group == "旧规则"
    assert origin_country.selectable is False
    assert origin_country.replacement_field == "media.countries"


def test_first_empty_rule_becomes_source_fallback_and_later_entries_are_disabled() -> None:
    """首个全空项应成为 TMDB 兜底，后续分类和规则保留但永远禁用。"""
    result = migrate_legacy_category_config(
        {
            "movie": {
                "首个兜底": {"adult": None},
                "后续规则": {"adult": "TRUE"},
                "后续空项": None,
            },
            "tv": {},
        }
    )
    legacy_categories = [category for category in result.policy.categories if category.id.startswith("legacy.movie.")]

    assert result.policy.source_fallbacks["themoviedb"]["电影"] == legacy_categories[0].id
    assert [category.enabled for category in legacy_categories] == [True, False, False]
    assert [rule.enabled for rule in result.policy.rules] == [False, False, False]
    assert [issue.code for issue in result.issues].count("unreachable_legacy_category") == 2
    assert ClassificationPolicyValidator.validate(result.policy, result.extra_fields).valid
    projection = project_policy_to_legacy_category_projection(result.policy)
    assert (
        projection.config.movie
        == CategoryConfig.model_validate(
            {
                "movie": {
                    "首个兜底": {"adult": None},
                    "后续规则": {"adult": "TRUE"},
                    "后续空项": None,
                }
            }
        ).movie
    )


def test_mixed_genre_ids_keep_positive_or_and_negative_and_semantics() -> None:
    """已知与未知 Genre ID 混合时，正值保持 OR，排除值必须全部满足。"""
    result = migrate_legacy_category_config(
        {
            "movie": {
                "混合类型": {"genre_ids": "16,999,!99,!777"},
                "兜底": None,
            },
            "tv": {},
        }
    )

    assert result.valid
    extension_ids = {field.id for field in legacy_extension_fields_from_policy(result.policy)}
    assert "extensions.themoviedb.genre_ids" in extension_ids
    genre_field = next(
        field
        for field in result.extra_fields
        if field.id == "extensions.themoviedb.genre_ids"
    )
    assert genre_field.label == "风格（旧规则）"
    assert genre_field.selectable is False
    assert genre_field.replacement_field == "media.genre_keys"
    assert ClassificationPolicyValidator.validate(result.policy, result.extra_fields).valid
    assert _category_name(result, _tmdb_facts(result, {"genre_ids": [16]}, "电影")) == "混合类型"
    assert _category_name(result, _tmdb_facts(result, {"genre_ids": [999]}, "电影")) == "混合类型"
    assert _category_name(result, _tmdb_facts(result, {"genre_ids": [16, 99]}, "电影")) == "兜底"
    assert _category_name(result, _tmdb_facts(result, {"genre_ids": [999, 777]}, "电影")) == "兜底"
    assert _category_name(result, _tmdb_facts(result, {}, "电影")) == "兜底"


def test_safe_unknown_field_is_declared_but_unsafe_field_blocks_publish() -> None:
    """合法未知 TMDB 一级字段可迁移，非法字段段必须产生阻断错误。"""
    safe = migrate_legacy_category_config(
        {
            "movie": {"已发行": {"status": "released"}, "兜底": None},
            "tv": {},
        }
    )
    unsafe = migrate_legacy_category_config(
        {
            "movie": {"非法": {"bad.field": "X"}, "兜底": None},
            "tv": {},
        }
    )

    assert safe.valid
    assert safe.extra_fields[0].id == "extensions.themoviedb.status"
    assert safe.extra_fields[0].source_support == {"themoviedb": "extension"}
    assert safe.extra_fields[0].media_types == ["电影"]
    assert _category_name(safe, _tmdb_facts(safe, {"status": "Released"}, "电影")) == "已发行"
    assert not unsafe.valid
    assert unsafe.issues[0].code == "invalid_legacy_field"
    assert not unsafe.policy.rules[0].enabled


def test_extension_fact_projection_reproduces_legacy_string_views() -> None:
    """扩展事实应严格复现假值缺失、国家提取及列表标量大写语义。"""
    result = migrate_legacy_category_config(
        {
            "movie": {
                "字段视图": {
                    "adult": "TRUE",
                    "runtime": "120",
                    "origin_country": "CN",
                    "production_countries": "US",
                    "release_year": "2024",
                    "keywords": "ONE",
                    "genre_ids": "999",
                },
                "兜底": None,
            },
            "tv": {},
        }
    )
    tmdb_info = {
        "adult": False,
        "runtime": 120,
        "origin_country": ["cn", "hk"],
        "production_countries": [{"iso_3166_1": "us"}, {}],
        "release_date": "2024-09-02",
        "keywords": ["one", {"id": 1}],
        "genre_ids": [999],
    }

    by_policy = build_legacy_tmdb_extension_facts(result.policy, tmdb_info)
    by_fields = build_legacy_tmdb_extension_facts(result.extra_fields, tmdb_info)

    assert by_policy == by_fields
    assert by_policy == {
        "themoviedb": {
            "runtime": ["120"],
            "origin_country": ["CN", "HK"],
            "production_countries": ["US", "NONE"],
            "release_year": ["2024"],
            "keywords": ["ONE", "{'ID': 1}"],
            "genre_ids": ["999"],
        }
    }


def test_missing_fact_and_positive_negative_combination_match_legacy() -> None:
    """缺失事实不得命中排除条件，正值 OR 与负值排除应同时生效。"""
    config = {
        "movie": {
            "指定地区": {"origin_country": "CN,TW,!HK"},
            "非假成人": {"adult": "!FALSE"},
            "兜底": None,
        },
        "tv": {},
    }
    result = migrate_legacy_category_config(config)

    fixtures: list[tuple[dict[str, object], str]] = [
        ({"origin_country": ["CN"]}, "指定地区"),
        ({"origin_country": ["TW", "HK"]}, "兜底"),
        ({"adult": True}, "非假成人"),
        ({"adult": False}, "兜底"),
        ({"id": 5}, "兜底"),
    ]
    for tmdb_info, expected in fixtures:
        legacy = _legacy_tmdb_category(config["movie"], tmdb_info)
        current = _category_name(result, _tmdb_facts(result, tmdb_info, "电影"))
        assert legacy == expected
        assert current == legacy


def test_release_year_supports_values_ranges_and_non_numeric_hyphen_endpoints() -> None:
    """年份单值、多值、闭区间和非数字端点应沿用旧展开语义。"""
    config = {
        "movie": {
            "近年": {"release_year": "2020-2022,2024"},
            "字母年": {"release_year": "ABCD-EFGH"},
            "兜底": None,
        },
        "tv": {},
    }
    result = migrate_legacy_category_config(config)

    assert _category_name(result, _tmdb_facts(result, {"release_date": "2021-01-01"}, "电影")) == "近年"
    assert _category_name(result, _tmdb_facts(result, {"release_date": "2024-01-01"}, "电影")) == "近年"
    assert _category_name(result, _tmdb_facts(result, {"release_date": "ABCD-date"}, "电影")) == "字母年"
    assert _category_name(result, _tmdb_facts(result, {"release_date": "2023-01-01"}, "电影")) == "兜底"
    projection = project_policy_to_legacy_category_projection(result.policy)
    assert projection.exact
    assert projection.config.movie == CategoryConfig.model_validate(config).movie


def test_stable_ids_are_repeatable_ascii_and_media_type_scoped() -> None:
    """相同输入必须生成相同 ASCII ID，同名电影和电视剧分类不能碰撞。"""
    config = {
        "movie": {"同名": {"adult": "TRUE"}},
        "tv": {"同名": {"adult": "TRUE"}},
    }
    first = migrate_legacy_category_config(config)
    second = migrate_legacy_category_config(config)
    first_ids = [category.id for category in first.policy.categories if category.id.startswith("legacy.")]
    second_ids = [category.id for category in second.policy.categories if category.id.startswith("legacy.")]

    assert first_ids == second_ids
    assert len(set(first_ids)) == 2
    assert all(category_id.isascii() for category_id in first_ids)


@pytest.mark.parametrize(  # type: ignore[misc]
    ("media_key", "tmdb_info"),
    [
        ("movie", {"id": 1, "genre_ids": [16], "original_language": "ja"}),
        ("movie", {"id": 2, "genre_ids": [18], "original_language": "zh"}),
        ("movie", {"id": 3, "production_countries": [{"iso_3166_1": "US"}]}),
        ("movie", {"id": 4, "original_language": "fr"}),
        ("tv", {"id": 5, "genre_ids": [16], "origin_country": ["CN"]}),
        ("tv", {"id": 6, "genre_ids": [16], "origin_country": ["JP"]}),
        ("tv", {"id": 7, "genre_ids": [18], "origin_country": ["US"]}),
    ],
)
def test_tmdb_fixtures_match_category_helper_directory_classification(
    media_key: str,
    tmdb_info: dict[str, object],
) -> None:
    """同一 TMDB 详情经旧算法和迁移策略求值后必须得到相同目录分类。"""
    config = _legacy_config()
    result = migrate_legacy_category_config(config)
    media_type: ClassificationMediaType = "电影" if media_key == "movie" else "电视剧"

    legacy_category = _legacy_tmdb_category(config[media_key], tmdb_info)
    current_category = _category_name(result, _tmdb_facts(result, tmdb_info, media_type))

    assert current_category == legacy_category


def test_non_tmdb_source_uses_common_fallback_instead_of_legacy_source_fallback() -> None:
    """非 TMDB 身份不得进入只为旧 category.yaml 保留的来源级兜底。"""
    result = migrate_legacy_category_config(_legacy_config())
    facts = ClassificationFacts.model_validate(
        {
            "identity": {"media_source": "douban", "media_id": "1295644"},
            "media": {"type": "电影", "title": "Fixture"},
            "extensions": {},
        }
    )

    evaluation = ClassificationEvaluator.evaluate(result.policy, facts)

    assert evaluation.result.recommended.category_id == result.policy.fallbacks["电影"]
    assert evaluation.result.recommended.category_id != result.policy.source_fallbacks["themoviedb"]["电影"]
    assert evaluation.result.recommended.source == "fallback"


def test_config_without_empty_entry_remains_valid_and_uses_common_fallback() -> None:
    """没有旧空兜底时不应拒绝迁移，未命中项统一进入通用未分类。"""
    result = migrate_legacy_category_config(
        {
            "movie": {"成人内容": {"adult": "TRUE"}},
            "tv": {},
        }
    )
    facts = _tmdb_facts(result, {"id": 8, "adult": False}, "电影")
    evaluation = ClassificationEvaluator.evaluate(result.policy, facts)

    assert result.valid
    assert result.policy.source_fallbacks == {}
    assert ClassificationPolicyValidator.validate(result.policy, result.extra_fields).valid
    assert evaluation.result.recommended.category_id == result.policy.fallbacks["电影"]
    assert evaluation.result.recommended.source == "fallback"


def test_migrated_policy_round_trips_to_category_config() -> None:
    """迁移器生成的标准配置应通过兼容投影恢复原始字段和值。"""
    config = {
        "movie": {
            "组合": {
                "genre_ids": "16,999,!99",
                "original_language": "zh,cn,!bo",
                "release_year": "2020-2022,2024",
                "status": "released",
            },
            "兜底": None,
        },
        "tv": {"未分类": None},
    }
    migrated = migrate_legacy_category_config(config)
    projected = project_policy_to_legacy_category_projection(migrated.policy)

    assert projected.exact
    assert projected.config == CategoryConfig.model_validate(config)
    assert project_policy_to_legacy_category_config(migrated.policy) == projected.config
