from app.modules.themoviedb.category import CategoryHelper
from app.schemas.category import CategoryRule


def test_category_diagnostics_preserve_first_match_and_explain_later_matches() -> None:
    """分类诊断可展示多条匹配，但实际结果仍采用第一条。"""
    rules = {
        "动漫": CategoryRule(genre_ids="16"),
        "欧美剧": CategoryRule(origin_country="US"),
    }

    decision = CategoryHelper.evaluate_category(
        categorys=rules,
        tmdb_info={"genre_ids": [16], "origin_country": ["US"]},
    )

    assert decision.selected_category == "动漫"
    assert [rule.matched for rule in decision.rules] == [True, True]
    assert decision.rules[0].selected is True
    assert decision.rules[1].reachable is False
    assert any(warning.code == "multiple_category_matches" for warning in decision.warnings)
    assert CategoryHelper.get_category(rules, {"genre_ids": [16], "origin_country": ["US"]}) == "动漫"


def test_category_diagnostics_explain_failed_animation_region_rules() -> None:
    """美国动画应展示国漫、日番失败原因并继续命中欧美剧。"""
    rules = {
        "国漫": CategoryRule(genre_ids="16", origin_country="CN"),
        "日番": CategoryRule(genre_ids="16", origin_country="JP"),
        "欧美剧": CategoryRule(origin_country="US"),
    }

    decision = CategoryHelper.evaluate_category(
        categorys=rules,
        tmdb_info={"genre_ids": [16], "origin_country": ["US"]},
    )

    assert decision.selected_category == "欧美剧"
    assert decision.rules[0].conditions[1].matched is False
    assert decision.rules[1].conditions[1].matched is False
    assert decision.rules[2].selected is True


def test_unconditional_category_before_end_emits_unreachable_warning() -> None:
    """中途兜底规则应保持命中语义并产生不可达诊断。"""
    rules = {
        "兜底": None,
        "综艺": CategoryRule(genre_ids="10764"),
    }

    decision = CategoryHelper.evaluate_category(
        categorys=rules,
        tmdb_info={"genre_ids": [10764]},
    )

    assert decision.selected_category == "兜底"
    assert decision.rules[1].matched is True
    assert decision.rules[1].reachable is False
    assert any(warning.code == "unconditional_category_not_last" for warning in decision.warnings)


def test_empty_category_conditions_are_treated_as_unconditional_fallback() -> None:
    """仅含空值条件的规则也应按无条件兜底规则诊断。"""
    rules = {
        "空值兜底": CategoryRule(genre_ids=""),
        "综艺": CategoryRule(genre_ids="10764"),
    }

    decision = CategoryHelper.evaluate_category(
        categorys=rules,
        tmdb_info={"genre_ids": [10764]},
    )

    assert decision.selected_category == "空值兜底"
    assert decision.rules[1].matched is True
    assert decision.rules[1].reachable is False
    assert any(
        warning.code == "unconditional_category_not_last" and warning.related_indices == [0]
        for warning in decision.warnings
    )


def test_category_range_and_exclusion_keep_existing_semantics() -> None:
    """年份范围与排除条件在诊断重构后必须保持现有结果。"""
    rules = {
        "近期非美国": CategoryRule(release_year="2024-2026", origin_country="!US"),
    }

    matched = CategoryHelper.evaluate_category(
        categorys=rules,
        tmdb_info={"first_air_date": "2025-01-01", "origin_country": ["CN"]},
    )
    excluded = CategoryHelper.evaluate_category(
        categorys=rules,
        tmdb_info={"first_air_date": "2025-01-01", "origin_country": ["US"]},
    )

    assert matched.selected_category == "近期非美国"
    assert excluded.selected_category == ""
