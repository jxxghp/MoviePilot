"""旧版 TMDB 分类配置到新版策略的纯迁移与兼容投影测试。"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional, cast

import pytest
import yaml

from app.application.classification.configuration import build_builtin_classification_policy
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
    ClassificationPolicy,
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


def _policy_category_name(policy: ClassificationPolicy, facts: ClassificationFacts) -> str:
    """执行指定策略并返回推荐分类名称，用于比较不同策略的业务结果。"""
    evaluation = ClassificationEvaluator.evaluate(policy, facts)
    category_id = evaluation.result.recommended.category_id
    categories = {str(category.id): str(category.name) for category in policy.categories}
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


def test_default_style_config_preserves_order_and_migrates_equivalent_fields() -> None:
    """默认式配置应保持顺序，具有等价语义的字段直接迁移到标准字段。"""
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
    assert "media.language" in all_fields
    assert "media.countries" in all_fields
    assert "extensions.themoviedb.original_language" not in all_fields
    assert "extensions.themoviedb.production_countries" not in all_fields
    assert "extensions.themoviedb.origin_country" not in all_fields
    assert legacy_extension_fields_from_policy(result.policy) == result.extra_fields
    assert ClassificationPolicyValidator.validate(result.policy, result.extra_fields).valid
    assert result.extra_fields == ()


def test_official_category_yaml_has_no_unknown_genre_ids() -> None:
    """仓库官方旧模板中的 Genre ID 均应直接迁移为标准风格键。"""
    config = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config" / "category.yaml").read_text(),
    )

    result = migrate_legacy_category_config(config)

    assert result.valid
    assert result.extra_fields == ()
    assert all(
        field_id == "media.genre_keys"
        for rule in result.policy.rules
        for field_id in _leaf_fields(rule.when)
        if "genre" in field_id
    )


@pytest.mark.parametrize(
    ("media_type", "tmdb_info", "expected"),
    [
        ("电影", {"genre_ids": [16]}, "动画电影"),
        ("电影", {"original_language": "zh"}, "华语电影"),
        ("电影", {"original_language": "en"}, "外语电影"),
        ("电视剧", {"genre_ids": [16], "origin_country": ["CN"]}, "国漫"),
        ("电视剧", {"genre_ids": [16], "origin_country": ["JP"]}, "日番"),
        ("电视剧", {"genre_ids": [99]}, "纪录片"),
        ("电视剧", {"genre_ids": [10762]}, "儿童"),
        ("电视剧", {"genre_ids": [10764]}, "综艺"),
        ("电视剧", {"origin_country": ["CN"]}, "国产剧"),
        ("电视剧", {"origin_country": ["US"]}, "欧美剧"),
        ("电视剧", {"origin_country": ["JP"]}, "日韩剧"),
        ("电视剧", {"origin_country": ["AU"]}, "未分类"),
    ],
)
def test_builtin_policy_matches_official_legacy_category_semantics(
    media_type: ClassificationMediaType,
    tmdb_info: Mapping[str, object],
    expected: str,
) -> None:
    """内置标准规则应逐个复现官方 category.yaml 的分类结果。"""
    config = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config" / "category.yaml").read_text(),
    )
    migrated = migrate_legacy_category_config(config)
    facts = _tmdb_facts(migrated, tmdb_info, media_type)
    builtin = build_builtin_classification_policy()

    assert _legacy_tmdb_category(config["movie" if media_type == "电影" else "tv"], tmdb_info) == expected
    assert _category_name(migrated, facts) == expected
    assert _policy_category_name(builtin, facts) == expected


def test_deleted_legacy_rules_keep_alias_only_fields_registered() -> None:
    """删除最后一条旧规则后，仍需登记孤立别名字段以通过策略校验。"""
    migrated = migrate_legacy_category_config(
        {
            "movie": {"中文电影": {"original_language": "zh"}},
            "tv": {},
        }
    )
    policy = migrated.policy.model_copy(deep=True, update={"rules": []})

    fields = legacy_extension_fields_from_policy(policy)

    assert fields == ()
    assert ClassificationPolicyValidator.validate(policy, fields).valid


def test_first_empty_rule_becomes_global_fallback_and_later_entries_are_disabled() -> None:
    """首个全空项应成为媒体类型全局兜底，后续分类和规则保留但永远禁用。"""
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

    assert result.policy.fallbacks["电影"] == legacy_categories[0].id
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
    """用户自定义未知 Genre ID 与已知值混用时仍保持旧的 OR 与排除语义。"""
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


def test_known_negative_genre_ids_use_standard_field() -> None:
    """官方词表内的排除 Genre ID 也应直接迁移到标准风格字段。"""
    result = migrate_legacy_category_config(
        {
            "movie": {
                "非动画纪录": {"genre_ids": "!16,!99"},
                "兜底": None,
            },
            "tv": {},
        }
    )

    fields = _leaf_fields(result.policy.rules[0].when)
    assert result.valid
    assert fields == ["media.genre_keys"]
    assert "extensions.themoviedb.genre_ids" not in fields
    assert result.extra_fields == ()
    assert _category_name(result, _tmdb_facts(result, {"genre_ids": [16]}, "电影")) == "兜底"
    assert _category_name(result, _tmdb_facts(result, {"genre_ids": [18]}, "电影")) == "非动画纪录"


@pytest.mark.parametrize("negative", [False, True])
@pytest.mark.parametrize("field_name", ["origin_country", "genre_ids"])
def test_large_value_sets_remain_compact_and_preserve_legacy_semantics(
    field_name: str,
    negative: bool,
) -> None:
    """大枚举只产生集合条件，旧 API 投影后每个正向或排除值仍保持原语义。"""
    values = [str(index) for index in range(1, 61)]
    prefix = "!" if negative else ""
    config = {
        "movie": {
            "目标": {field_name: ",".join(f"{prefix}{value}" for value in values)},
            "兜底": None,
        },
        "tv": {},
    }
    result = migrate_legacy_category_config(config)
    projection = project_policy_to_legacy_category_projection(result.policy)
    remigrated = migrate_legacy_category_config(projection.config)

    assert result.valid
    assert ClassificationPolicyValidator.validate(result.policy, result.extra_fields).valid
    assert len(_leaf_fields(result.policy.rules[0].when)) <= 2
    assert projection.exact
    for actual in [*([value] for value in values), ["999"], ["1", "999"], [], None]:
        tmdb_info = {field_name: actual, "id": 1}
        expected = _legacy_tmdb_category(config["movie"], tmdb_info)
        assert _category_name(result, _tmdb_facts(result, tmdb_info, "电影")) == expected
        assert _category_name(remigrated, _tmdb_facts(remigrated, tmdb_info, "电影")) == expected


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
    assert projection.config.movie == CategoryConfig.model_validate(
        {
            "movie": {
                "近年": {"release_year": "2020,2021,2022,2024"},
                "字母年": {"release_year": "ABCD,EFGH"},
                "兜底": None,
            }
        }
    ).movie


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


def test_non_tmdb_source_uses_the_same_media_type_fallback() -> None:
    """媒体类型全局兜底对不同数据源保持一致。"""
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
    expected = CategoryConfig.model_validate(config)
    assert expected.movie is not None
    assert expected.movie["组合"] is not None
    expected.movie["组合"].release_year = "2020,2021,2022,2024"
    assert projected.config == expected
    assert project_policy_to_legacy_category_config(migrated.policy) == projected.config


def test_migration_does_not_create_unused_duplicate_fallback_categories() -> None:
    """已有旧默认分类时直接复用，不能再创建未分类/通用的无用目录。"""
    result = migrate_legacy_category_config({"movie": {"未分类": None}, "tv": {"未分类": None}})
    assert result.valid
    assert [item.path for item in result.policy.categories if item.media_type == "电影"] == [["未分类"]]
    assert [item.path for item in result.policy.categories if item.media_type == "电视剧"] == [["未分类"]]
    assert result.policy.fallbacks["音乐"] == "music.uncategorized"
    assert ClassificationPolicyValidator.validate(result.policy, result.extra_fields).valid


def test_legacy_country_dictionary_keeps_country_codes_and_genre_ids_distinct() -> None:
    """旧地区条件能直接选择代码，未知旧风格编号不能误填为标准风格键。"""
    result = migrate_legacy_category_config({"tv": {"日韩剧": {"origin_country": "JP,KR", "genre_ids": "999"}}})
    fields = {item.id: item for item in result.extra_fields}
    assert all(field_id != "extensions.themoviedb.origin_country" for field_id in fields)
    assert not fields["extensions.themoviedb.genre_ids"].options
