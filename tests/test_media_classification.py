"""多媒体、多数据源分类领域能力的 A1 验收测试。"""

import copy
import gc
import statistics
import time
from collections.abc import Sequence
from typing import Any, Callable

import pytest

from app.domain.classification.evaluator import ClassificationEvaluator
from app.domain.classification.fields import get_standard_classification_fields
from app.domain.classification.validation import ClassificationPolicyValidator
from app.schemas.category import (
    ClassificationEvaluation,
    ClassificationFacts,
    ClassificationFieldDefinition,
    ClassificationPolicy,
    ClassificationValidationResult,
)

_MISSING = object()


def _category(
    category_id: str,
    media_type: str,
    name: str,
    path: list[str],
) -> dict[str, Any]:
    return {
        "id": category_id,
        "media_type": media_type,
        "name": name,
        "path": path,
        "enabled": True,
        "labels": [],
    }


def _base_policy_payload() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "revision": 1,
        "mode": "first_match",
        "categories": [
            _category("movie.fallback", "电影", "未分类", ["未分类"]),
            _category("movie.hit", "电影", "命中", ["命中"]),
            _category("movie.first", "电影", "第一分类", ["第一分类"]),
            _category("movie.second", "电影", "第二分类", ["第二分类"]),
            _category("tv.fallback", "电视剧", "未分类", ["未分类"]),
            _category("music.fallback", "音乐", "未分类", ["未分类"]),
        ],
        "rules": [],
        "fallbacks": {
            "电影": "movie.fallback",
            "电视剧": "tv.fallback",
            "音乐": "music.fallback",
        },
        "field_aliases": {},
        "updated_at": "2026-09-02T12:00:00+08:00",
    }


def _category_rule(
    rule_id: str,
    when: dict[str, Any],
    category_id: str = "movie.hit",
    *,
    media_types: list[str] | None = None,
    sources: list[str] | None = None,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": rule_id,
        "name": rule_id,
        "kind": "category",
        "enabled": True,
        "priority": 100,
        "media_types": media_types or ["电影"],
        "sources": sources or [],
        "when": when,
        "target": {
            "category_id": category_id,
            "labels": labels or [],
        },
    }


def _label_rule(
    rule_id: str,
    when: dict[str, Any],
    labels: list[str],
) -> dict[str, Any]:
    return {
        "id": rule_id,
        "name": rule_id,
        "kind": "label",
        "enabled": True,
        "priority": 100,
        "media_types": ["电影"],
        "sources": [],
        "when": when,
        "target": {"labels": labels},
    }


def _policy(*rules: dict[str, Any]) -> ClassificationPolicy:
    payload = _base_policy_payload()
    payload["rules"] = list(rules)
    return ClassificationPolicy.model_validate(payload)


def _set_nested(payload: dict[str, Any], field: str, value: Any) -> None:
    parent = payload
    parts = field.split(".")
    for part in parts[:-1]:
        parent = parent.setdefault(part, {})
    if value is _MISSING:
        parent.pop(parts[-1], None)
    else:
        parent[parts[-1]] = value


def _facts(
    *,
    media_type: str = "电影",
    media_source: str = "themoviedb",
    values: dict[str, Any] | None = None,
) -> ClassificationFacts:
    payload: dict[str, Any] = {
        "identity": {
            "media_source": media_source,
            "media_id": "media-1",
        },
        "media": {
            "type": media_type,
            "title": "Spirited Away",
            "year": 2001,
            "language": "ja",
            "countries": ["JP"],
            "genre_keys": ["animation", "family"],
            "genre_names": ["Animation", "Family"],
            "adult": False,
            "runtime": 125,
            "content_rating": "PG",
            "companies": ["Studio Ghibli"],
            "networks": ["NTV"],
        },
        "music": {
            "entity_type": "album",
            "album_type": "Album",
            "secondary_types": ["Live"],
            "genres": ["Rock"],
            "tags": ["j-rock"],
            "artists": ["Example Artist"],
            "artist_country": "JP",
            "release_status": "Official",
        },
        "extensions": {},
    }
    for field, value in (values or {}).items():
        _set_nested(payload, field, value)
    return ClassificationFacts.model_validate(payload)


def _evaluate(
    policy: ClassificationPolicy,
    facts: ClassificationFacts | None = None,
    *,
    trace: bool = False,
) -> ClassificationEvaluation:
    return ClassificationEvaluator().evaluate(
        policy=policy,
        facts=facts or _facts(),
        trace=trace,
    )


def _leaf(field: str, operator: str, value: Any = _MISSING) -> dict[str, Any]:
    condition = {"field": field, "operator": operator}
    if value is not _MISSING:
        condition["value"] = value
    return condition


@pytest.mark.parametrize(  # type: ignore[misc]
    ("condition", "fact_values"),
    [
        (_leaf("media.language", "equals", "ja"), {}),
        (_leaf("media.language", "not_equals", "en"), {}),
        (_leaf("media.language", "in", ["ja", "zh"]), {}),
        (_leaf("media.language", "not_in", ["en", "fr"]), {}),
        (_leaf("media.language", "contains", "a"), {}),
        (_leaf("media.language", "starts_with", "j"), {}),
        (_leaf("media.language", "ends_with", "a"), {}),
        (_leaf("media.runtime", "gt", 120), {}),
        (_leaf("media.runtime", "gte", 125), {}),
        (_leaf("media.runtime", "lt", 130), {}),
        (_leaf("media.runtime", "lte", 125), {}),
        (_leaf("media.runtime", "between", [120, 130]), {}),
        (_leaf("media.genre_keys", "contains_any", ["animation", "music"]), {}),
        (_leaf("media.genre_keys", "contains_all", ["animation", "family"]), {}),
        (_leaf("media.genre_keys", "contains_none", ["horror", "war"]), {}),
        (_leaf("media.adult", "is_true"), {"media.adult": True}),
        (_leaf("media.adult", "is_false"), {}),
        (_leaf("media.content_rating", "exists"), {}),
        (_leaf("media.content_rating", "not_exists"), {"media.content_rating": _MISSING}),
    ],
    ids=[
        "equals",
        "not-equals",
        "in",
        "not-in",
        "contains",
        "starts-with",
        "ends-with",
        "gt",
        "gte",
        "lt",
        "lte",
        "between",
        "contains-any",
        "contains-all",
        "contains-none",
        "is-true",
        "is-false",
        "exists",
        "not-exists",
    ],
)
def test_all_operators_have_a_representative_match(
    condition: dict[str, Any],
    fact_values: dict[str, Any],
) -> None:
    evaluation = _evaluate(
        _policy(_category_rule("rule.operator", condition)),
        _facts(values=fact_values),
    )

    assert evaluation.result.recommended.category_id == "movie.hit"
    assert evaluation.result.recommended.rule_id == "rule.operator"


@pytest.mark.parametrize("missing_value", [_MISSING, None], ids=["missing", "null"])  # type: ignore[misc]
@pytest.mark.parametrize(  # type: ignore[misc]
    ("field", "operator", "expected"),
    [
        ("media.content_rating", "not_equals", "R"),
        ("media.content_rating", "not_in", ["R", "NC-17"]),
        ("media.networks", "contains_none", ["Netflix"]),
    ],
)
def test_negative_operators_do_not_match_missing_or_null_values(
    missing_value: Any,
    field: str,
    operator: str,
    expected: Any,
) -> None:
    evaluation = _evaluate(
        _policy(_category_rule("rule.negative", _leaf(field, operator, expected))),
        _facts(values={field: missing_value}),
    )

    assert evaluation.result.recommended.category_id == "movie.fallback"
    assert evaluation.result.recommended.rule_id is None


@pytest.mark.parametrize("missing_value", [_MISSING, None], ids=["missing", "null"])  # type: ignore[misc]
def test_not_exists_matches_missing_and_null_values(missing_value: Any) -> None:
    evaluation = _evaluate(
        _policy(
            _category_rule(
                "rule.not-exists",
                _leaf("media.content_rating", "not_exists"),
            )
        ),
        _facts(values={"media.content_rating": missing_value}),
    )

    assert evaluation.result.recommended.category_id == "movie.hit"


def test_all_any_and_not_groups_can_be_nested() -> None:
    condition = {
        "all": [
            _leaf("media.year", "gte", 2000),
            {
                "any": [
                    _leaf("media.language", "equals", "zh"),
                    _leaf("media.countries", "contains_any", ["JP"]),
                ]
            },
            {"not": _leaf("media.adult", "is_true")},
        ]
    }

    evaluation = _evaluate(_policy(_category_rule("rule.nested", condition)))

    assert evaluation.result.recommended.category_id == "movie.hit"
    assert evaluation.result.recommended.rule_id == "rule.nested"


def test_first_matching_category_rule_wins() -> None:
    always = _leaf("media.type", "equals", "电影")
    evaluation = _evaluate(
        _policy(
            _category_rule("rule.first", always, "movie.first"),
            _category_rule("rule.second", always, "movie.second"),
        )
    )

    assert evaluation.result.recommended.category_id == "movie.first"
    assert evaluation.result.recommended.rule_id == "rule.first"
    assert evaluation.result.effective == evaluation.result.recommended


def test_matching_label_rules_accumulate_with_stable_deduplication() -> None:
    always = _leaf("media.type", "equals", "电影")
    evaluation = _evaluate(
        _policy(
            _category_rule(
                "rule.category",
                always,
                labels=["base", "shared"],
            ),
            _label_rule("rule.label-one", always, ["alpha", "shared"]),
            _label_rule("rule.label-two", always, ["alpha", "omega"]),
        )
    )

    assert evaluation.result.labels == ["base", "shared", "alpha", "omega"]


def test_media_type_fallback_is_used_when_no_category_rule_matches() -> None:
    evaluation = _evaluate(
        _policy(
            _category_rule(
                "rule.no-match",
                _leaf("media.year", "lt", 1900),
            )
        )
    )

    assert evaluation.result.recommended.category_id == "movie.fallback"
    assert evaluation.result.recommended.category_path == ["未分类"]
    assert evaluation.result.recommended.rule_id is None
    assert evaluation.result.effective.source == "fallback"


@pytest.mark.parametrize(  # type: ignore[misc]
    ("media_type", "media_source", "expected_category"),
    [
        ("电影", "themoviedb", "movie.hit"),
        ("电影", "douban", "movie.fallback"),
        ("音乐", "themoviedb", "music.fallback"),
    ],
)
def test_rule_source_and_media_type_restrictions_are_applied_before_conditions(
    media_type: str,
    media_source: str,
    expected_category: str,
) -> None:
    rule = _category_rule(
        "rule.restricted",
        _leaf("media.year", "gte", 2000),
        media_types=["电影"],
        sources=["themoviedb"],
    )

    evaluation = _evaluate(
        _policy(rule),
        _facts(media_type=media_type, media_source=media_source),
    )

    assert evaluation.result.recommended.category_id == expected_category


def test_trace_reports_actual_values_and_match_decisions() -> None:
    evaluation = _evaluate(
        _policy(
            _category_rule(
                "rule.trace",
                _leaf("media.year", "equals", 1999),
            )
        ),
        trace=True,
    )

    assert evaluation.trace[0].rule_id == "rule.trace"
    assert evaluation.trace[0].matched is False
    assert evaluation.trace[0].conditions[0].field == "media.year"
    assert evaluation.trace[0].conditions[0].actual == 2001
    assert evaluation.trace[0].conditions[0].matched is False


def _validation_report(
    payload: dict[str, Any],
    *,
    fields: Sequence[ClassificationFieldDefinition] | None = None,
) -> ClassificationValidationResult:
    standard_fields = get_standard_classification_fields()
    extra_fields = tuple(fields or ())
    if extra_fields[:len(standard_fields)] == standard_fields:
        extra_fields = extra_fields[len(standard_fields):]
    return ClassificationPolicyValidator.validate(
        ClassificationPolicy.model_validate(payload),
        extra_fields=extra_fields,
    )


def _assert_validation_error(
    payload: dict[str, Any],
    expected_code: str,
    *,
    fields: Sequence[ClassificationFieldDefinition] | None = None,
) -> None:
    report = _validation_report(payload, fields=fields)
    error_codes = {
        issue.code for issue in report.issues if issue.severity == "error"
    }
    assert report.valid is False
    assert expected_code in error_codes, error_codes


def test_valid_policy_has_no_validation_errors() -> None:
    payload = _base_policy_payload()
    payload["rules"] = [
        _category_rule(
            "rule.valid",
            _leaf("media.genre_keys", "contains_any", ["animation"]),
            sources=["themoviedb"],
        )
    ]

    report = _validation_report(payload)

    assert report.valid is True
    assert report.issues == []


@pytest.mark.parametrize(  # type: ignore[misc]
    ("mutate", "expected_code"),
    [
        (
            lambda payload: payload["categories"].append(
                copy.deepcopy(payload["categories"][0])
            ),
            "duplicate_category_id",
        ),
        (
            lambda payload: payload["rules"].append(
                copy.deepcopy(payload["rules"][0])
            ),
            "duplicate_rule_id",
        ),
    ],
    ids=["category-id", "rule-id"],
)
def test_duplicate_ids_are_rejected(
    mutate: Callable[[dict[str, Any]], None],
    expected_code: str,
) -> None:
    payload = _base_policy_payload()
    payload["rules"] = [
        _category_rule("rule.duplicate", _leaf("media.year", "gte", 2000))
    ]
    mutate(payload)

    _assert_validation_error(payload, expected_code)


@pytest.mark.parametrize(  # type: ignore[misc]
    "path",
    [
        [],
        [""],
        ["."],
        [".."],
        ["动画/电影"],
        ["动画\\电影"],
        ["非法\x00路径"],
        ["x" * 65],
        ["一", "二", "三", "四", "五"],
    ],
)
def test_invalid_category_paths_are_rejected(path: list[str]) -> None:
    payload = _base_policy_payload()
    payload["categories"][0]["path"] = path

    _assert_validation_error(payload, "invalid_category_path")


def test_duplicate_paths_are_rejected_within_the_same_media_type() -> None:
    payload = _base_policy_payload()
    payload["categories"][1]["path"] = payload["categories"][0]["path"]

    _assert_validation_error(payload, "duplicate_category_path")


@pytest.mark.parametrize(  # type: ignore[misc]
    ("target", "expected_code"),
    [
        ("movie.unknown", "target_category_not_found"),
        ("tv.fallback", "target_media_type_mismatch"),
    ],
    ids=["not-found", "cross-media-type"],
)
def test_category_rule_targets_must_exist_and_match_the_rule_media_type(
    target: str,
    expected_code: str,
) -> None:
    payload = _base_policy_payload()
    payload["rules"] = [
        _category_rule(
            "rule.invalid-target",
            _leaf("media.year", "gte", 2000),
            target,
        )
    ]

    _assert_validation_error(payload, expected_code)


@pytest.mark.parametrize(  # type: ignore[misc]
    ("condition", "expected_code"),
    [
        (_leaf("media.unregistered", "equals", "x"), "unknown_field"),
        (_leaf("media.year", "contains", "20"), "unsupported_operator"),
        (_leaf("media.year", "between", ["old", 2020]), "invalid_condition_value"),
        (_leaf("media.language", "in", []), "empty_membership_value"),
        (_leaf("media.year", "between", [2025, 2000]), "invalid_between_range"),
    ],
    ids=[
        "unknown-field",
        "unsupported-operator",
        "wrong-value-type",
        "empty-in",
        "reversed-between",
    ],
)
def test_condition_contract_errors_are_rejected(
    condition: dict[str, Any],
    expected_code: str,
) -> None:
    payload = _base_policy_payload()
    payload["rules"] = [_category_rule("rule.invalid-condition", condition)]

    _assert_validation_error(payload, expected_code)


def test_condition_tree_depth_limit_is_enforced() -> None:
    condition: dict[str, Any] = _leaf("media.year", "gte", 2000)
    for _ in range(4):
        condition = {"not": condition}
    payload = _base_policy_payload()
    payload["rules"] = [_category_rule("rule.too-deep", condition)]

    _assert_validation_error(payload, "max_depth_exceeded")


def test_policy_rule_count_limit_is_enforced() -> None:
    payload = _base_policy_payload()
    payload["rules"] = [
        _category_rule(
            f"rule.too-many.{index}",
            _leaf("media.year", "gte", 2000),
        )
        for index in range(1001)
    ]

    _assert_validation_error(payload, "max_rules_exceeded")


def test_per_rule_leaf_condition_limit_is_enforced() -> None:
    payload = _base_policy_payload()
    payload["rules"] = [
        _category_rule(
            "rule.too-many-conditions",
            {
                "all": [
                    _leaf("media.year", "gte", 1900)
                    for _ in range(31)
                ]
            },
        )
    ]

    _assert_validation_error(payload, "max_conditions_exceeded")


def test_every_enabled_media_type_requires_a_fallback() -> None:
    payload = _base_policy_payload()
    payload["fallbacks"].pop("音乐")

    _assert_validation_error(payload, "missing_fallback")


def test_extension_field_namespace_must_match_the_restricted_source() -> None:
    standard_fields = list(get_standard_classification_fields())
    field_model = type(standard_fields[0])
    extension_field = field_model.model_validate(
        {
            "id": "extensions.example.source.region_group",
            "label": "来源地区组",
            "value_type": "string",
            "operators": ["equals", "in", "exists", "not_exists"],
            "media_types": ["电影"],
            "options": [],
            "source_support": {"example.source": "extension"},
        }
    )
    payload = _base_policy_payload()
    payload["rules"] = [
        _category_rule(
            "rule.bad-extension-source",
            _leaf(
                "extensions.example.source.region_group",
                "equals",
                "east-asia",
            ),
            sources=["other.source"],
        )
    ]

    _assert_validation_error(
        payload,
        "extension_namespace_mismatch",
        fields=[*standard_fields, extension_field],
    )


def test_standard_field_catalog_is_unique_and_exposes_operator_contracts() -> None:
    fields = get_standard_classification_fields()
    field_map = {field.id: field for field in fields}

    assert len(field_map) == len(fields)
    assert "contains_none" in field_map["media.countries"].operators
    assert "between" in field_map["media.year"].operators
    assert "in" in field_map["media.year"].operators
    assert "not_in" in field_map["media.year"].operators
    assert "is_false" in field_map["media.adult"].operators
    assert set(field_map["music.secondary_types"].media_types) == {"音乐"}


def _percentile_95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[max(0, int(len(ordered) * 0.95) - 1)]


def test_two_hundred_rules_with_six_conditions_evaluate_under_five_ms_p95() -> None:
    rules = []
    for index in range(200):
        rules.append(
            _category_rule(
                f"rule.performance.{index}",
                {
                    "all": [
                        _leaf("media.year", "gte", 1900),
                        _leaf("media.year", "lte", 2100),
                        _leaf("media.language", "in", ["ja", "en"]),
                        _leaf("media.countries", "contains_any", ["JP"]),
                        _leaf("media.genre_keys", "contains_any", ["animation"]),
                        _leaf("media.content_rating", "equals", f"rating-{index}"),
                    ]
                },
            )
        )
    evaluator = ClassificationEvaluator()
    policy = _policy(*rules)
    facts = _facts()

    for _ in range(20):
        evaluator.evaluate(policy=policy, facts=facts, trace=False)

    batch_p95_values = []
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        for _ in range(5):
            samples = []
            for _ in range(40):
                started_at = time.perf_counter_ns()
                evaluator.evaluate(policy=policy, facts=facts, trace=False)
                samples.append((time.perf_counter_ns() - started_at) / 1_000_000_000)
            batch_p95_values.append(_percentile_95(samples))
    finally:
        if gc_was_enabled:
            gc.enable()

    median_p95 = statistics.median(batch_p95_values)
    assert median_p95 < 0.005, (
        f"200 rules x 6 conditions median P95 was {median_p95 * 1000:.3f} ms; "
        f"batch P95 values were {[round(value * 1000, 3) for value in batch_p95_values]} ms"
    )
