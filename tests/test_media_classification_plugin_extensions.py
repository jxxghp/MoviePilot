"""插件媒体来源分类声明、事实过滤和动态 provider 契约测试。"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.application.classification.configuration import (
    ClassificationPolicyConfigurationService,
)
from app.application.classification.execution import ClassificationExecutionService
from app.domain.classification.facts import build_classification_facts
from app.domain.context import MediaInfo
from app.runtime.extensions.plugin.classification import (
    PluginClassificationDeclarationError,
    PluginClassificationRegistry,
)
from app.schemas.category import (
    CategoryConfig,
    ClassificationEnrichmentRequest,
    ClassificationEnrichmentResponse,
    ClassificationFieldDefinition,
    ClassificationPolicy,
)
from app.schemas.event import MediaSourceInfo
from app.schemas.types import MediaSource, MediaType
from app.sdk.classification import (
    MEDIA_SOURCE_CLASSIFICATION_PROTOCOL_VERSION,
    classify_media,
)
from app.sdk.classification import (
    ClassificationEnrichmentRequest as PublicClassificationEnrichmentRequest,
)
from app.sdk.classification import (
    ClassificationEnrichmentResponse as PublicClassificationEnrichmentResponse,
)
from app.sdk.classification import (
    ClassificationFieldDefinition as PublicClassificationFieldDefinition,
)
from app.sdk.classification import (
    MediaSourceInfo as PublicMediaSourceInfo,
)

PLUGIN_ID = "classificationexample"
OTHER_PLUGIN_ID = "classificationother"
SOURCE_ID = "example.source"
FIELD_PREFIX = f"extensions.{SOURCE_ID}."


class _Runtime:
    """向分类执行服务提供固定活动策略和空 legacy 配置。"""

    def __init__(self, policy: ClassificationPolicy) -> None:
        """保存与调用方隔离的活动策略。"""
        self._policy = policy.model_copy(deep=True)

    def active_policy(self) -> ClassificationPolicy:
        """返回活动策略的深拷贝。"""
        return self._policy.model_copy(deep=True)

    @staticmethod
    def legacy_config() -> CategoryConfig:
        """返回不包含旧分类规则的配置。"""
        return CategoryConfig()


def _field(
    local_id: str,
    *,
    value_type: str = "string",
    operators: list[str] | None = None,
    media_types: list[str] | None = None,
    options: list[object] | None = None,
    allow_custom_values: bool = True,
    source_id: str = SOURCE_ID,
) -> dict[str, object]:
    """构造一个插件来源扩展字段声明。"""
    default_operators = {
        "string": ["equals", "exists"],
        "enum": ["equals", "in"],
        "integer": ["equals", "gte"],
        "number": ["equals", "gte"],
        "year": ["equals", "between"],
        "string_list": ["contains_any", "contains_all"],
        "boolean": ["is_true", "is_false"],
    }
    return {
        "id": f"extensions.{source_id}.{local_id}",
        "label": local_id.replace("_", " "),
        "value_type": value_type,
        "operators": operators or default_operators[value_type],
        "media_types": media_types or [],
        "options": options or [],
        "allow_custom_values": allow_custom_values,
    }


def _source(
    *,
    source_id: str = SOURCE_ID,
    media_types: list[str] | None = None,
    fields: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """构造一个插件媒体来源声明。"""
    return {
        "name": "Example Source",
        "media_source": source_id,
        "media_types": media_types or ["电影"],
        "classification_fields": fields or [_field("region_group")],
    }


def _registry() -> PluginClassificationRegistry:
    """构造使用隔离日志桩的插件分类注册表。"""
    return PluginClassificationRegistry(Mock())


def test_public_sdk_exposes_stable_protocol_version_and_schema_types() -> None:
    """公开 SDK v2 应保留来源声明并增加富化请求响应模型。"""
    assert MEDIA_SOURCE_CLASSIFICATION_PROTOCOL_VERSION == 2
    assert PublicClassificationFieldDefinition is ClassificationFieldDefinition
    assert PublicMediaSourceInfo is MediaSourceInfo
    assert PublicClassificationEnrichmentRequest is ClassificationEnrichmentRequest
    assert PublicClassificationEnrichmentResponse is ClassificationEnrichmentResponse


def test_public_sdk_classify_media_returns_copy_before_runtime_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """宿主尚未装配时公开分类调用应返回隔离副本，而不是修改插件对象。"""
    media = MediaInfo(
        media_source=MediaSource(SOURCE_ID),
        media_id="native-1",
        type=MediaType.MOVIE,
        title="Example",
    )

    def unavailable_context() -> object:
        """模拟插件在 lifespan 完成装配前调用 SDK。"""
        raise RuntimeError("runtime unavailable")

    monkeypatch.setattr(
        "app.sdk.classification.get_chain_runtime_context",
        unavailable_context,
    )

    classified = classify_media(media)

    assert classified is not media
    assert classified.title == media.title
    classified.title = "Changed"
    assert media.title == "Example"


def test_public_sdk_classify_media_uses_composed_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """宿主完成装配后公开分类调用应委托唯一分类执行服务。"""
    media = MediaInfo(
        media_source=MediaSource(SOURCE_ID),
        media_id="native-1",
        type=MediaType.MOVIE,
        title="Example",
    )
    classified = deepcopy(media)
    classified.set_library_category("电影/扩展")
    service = Mock()
    service.finalize.return_value = classified
    monkeypatch.setattr(
        "app.sdk.classification.get_chain_runtime_context",
        lambda: SimpleNamespace(classification_service=service),
    )

    result = classify_media(media)

    assert result is classified
    service.finalize.assert_called_once_with(media, refresh=True)


def _all_value_type_source() -> dict[str, object]:
    """构造覆盖全部扩展事实值类型的有效来源声明。"""
    return _source(
        fields=[
            _field("region_group"),
            _field(
                "release_status",
                value_type="enum",
                options=["official", "bootleg"],
                allow_custom_values=False,
            ),
            _field(
                "distribution_tier",
                value_type="enum",
                options=[1, 2],
                allow_custom_values=False,
            ),
            _field("rank", value_type="integer"),
            _field("score", value_type="number"),
            _field("release_year", value_type="year"),
            _field("tags", value_type="string_list"),
            _field("verified", value_type="boolean"),
        ]
    )


def _media(
    facts: dict[str, object],
    *,
    media_type: MediaType = MediaType.MOVIE,
) -> MediaInfo:
    """构造携带完整扩展字段路径和稳定来源身份的媒体对象。"""
    return MediaInfo(
        media_source=MediaSource(SOURCE_ID),
        media_id="native-1",
        type=media_type,
        title="Example",
        classification_facts=facts,
    )


def _extension_policy() -> ClassificationPolicy:
    """构造由插件扩展字段命中分类、否则回退的活动策略。"""
    return ClassificationPolicy.model_validate(
        {
            "revision": 7,
            "categories": [
                {
                    "id": "movie.extension",
                    "media_type": "电影",
                    "name": "扩展命中",
                    "path": ["扩展命中"],
                },
                {
                    "id": "movie.fallback",
                    "media_type": "电影",
                    "name": "电影兜底",
                    "path": ["电影兜底"],
                },
            ],
            "rules": [
                {
                    "id": "rule.extension.region",
                    "name": "插件地区组",
                    "kind": "category",
                    "media_types": ["电影"],
                    "sources": [SOURCE_ID],
                    "when": {
                        "all": [
                            {
                                "field": f"{FIELD_PREFIX}region_group",
                                "operator": "equals",
                                "value": "east-asia",
                            }
                        ]
                    },
                    "target": {"category_id": "movie.extension"},
                }
            ],
            "fallbacks": {"电影": "movie.fallback"},
        }
    )


def test_registry_accepts_valid_declaration_and_returns_deep_copies() -> None:
    """有效声明应被规范化，输入和返回快照均不能污染注册表。"""
    registry = _registry()
    declaration = _all_value_type_source()

    registry.replace(PLUGIN_ID, [declaration])
    declaration["name"] = "Mutated Source"
    declaration_fields = declaration["classification_fields"]
    assert isinstance(declaration_fields, list)
    declaration_fields[0]["label"] = "Mutated Field"

    first_sources = registry.sources()
    first_fields = registry.fields()
    first_sources[0]["name"] = "Changed Snapshot"
    first_fields[0].label = "Changed Snapshot"

    current_source = registry.sources()[0]
    current_fields = registry.fields()
    assert current_source["name"] == "Example Source"
    assert current_source["plugin_id"] == PLUGIN_ID
    assert current_fields[0].label == "region group"
    assert current_fields[0].media_types == ["电影"]
    assert current_fields[0].source_support == {SOURCE_ID: "extension"}


def test_registry_rejects_builtin_duplicate_and_cross_plugin_sources() -> None:
    """插件不得覆盖内置来源、重复声明来源或占用其他插件的来源。"""
    registry = _registry()

    with pytest.raises(PluginClassificationDeclarationError, match="内置媒体来源"):
        registry.replace(PLUGIN_ID, [_source(source_id="themoviedb")])
    with pytest.raises(PluginClassificationDeclarationError, match="重复声明"):
        registry.replace(PLUGIN_ID, [_source(), _source()])

    registry.replace(PLUGIN_ID, [_source()])
    with pytest.raises(PluginClassificationDeclarationError, match="已由插件"):
        registry.replace(OTHER_PLUGIN_ID, [_source()])

    assert registry.sources(PLUGIN_ID)[0]["media_source"] == SOURCE_ID
    assert registry.sources(OTHER_PLUGIN_ID) == []


@pytest.mark.parametrize(
    ("field", "error_text"),
    [
        (_field("region_group", source_id="other.source"), "命名空间"),
        (
            _field("rank", value_type="integer", operators=["contains"]),
            "不兼容的操作符",
        ),
        (_field("music_only", media_types=["音乐"]), "媒体类型超出"),
    ],
)
def test_registry_rejects_invalid_namespace_operator_and_media_type(
    field: dict[str, object],
    error_text: str,
) -> None:
    """字段命名空间、操作符和值适用媒体类型必须受来源声明约束。"""
    registry = _registry()

    with pytest.raises(PluginClassificationDeclarationError, match=error_text):
        registry.replace(PLUGIN_ID, [_source(fields=[field])])

    assert registry.sources() == []


def test_registry_replacement_is_atomic_and_remove_is_idempotent() -> None:
    """无效替换保留旧快照，有效替换完整覆盖，移除可重复调用。"""
    registry = _registry()
    registry.replace(PLUGIN_ID, [_source()])

    invalid = _source(fields=[_field("rank", value_type="integer", operators=["contains"])])
    with pytest.raises(PluginClassificationDeclarationError):
        registry.replace(PLUGIN_ID, [invalid])
    assert [field.id for field in registry.fields()] == [f"{FIELD_PREFIX}region_group"]

    registry.replace(PLUGIN_ID, [_source(fields=[_field("replacement")])])
    assert [field.id for field in registry.fields()] == [f"{FIELD_PREFIX}replacement"]

    registry.remove(PLUGIN_ID)
    registry.remove(PLUGIN_ID)
    assert registry.sources() == []
    assert registry.fields() == ()


def test_registry_accepts_complete_extension_paths_for_all_value_types() -> None:
    """全部协议值类型应从完整字段路径转换为来源内局部事实。"""
    registry = _registry()
    registry.replace(PLUGIN_ID, [_all_value_type_source()])
    media = _media(
        {
            f"{FIELD_PREFIX}region_group": "east-asia",
            f"{FIELD_PREFIX}release_status": "official",
            f"{FIELD_PREFIX}distribution_tier": 2,
            f"{FIELD_PREFIX}rank": 3,
            f"{FIELD_PREFIX}score": 8.5,
            f"{FIELD_PREFIX}release_year": 2026,
            f"{FIELD_PREFIX}tags": ["anime", "movie"],
            f"{FIELD_PREFIX}verified": True,
        }
    )

    accepted = registry.facts(media)
    facts = build_classification_facts(media, extensions=accepted)

    assert accepted == {
        SOURCE_ID: {
            "region_group": "east-asia",
            "release_status": "official",
            "distribution_tier": 2,
            "rank": 3,
            "score": 8.5,
            "release_year": 2026,
            "tags": ["anime", "movie"],
            "verified": True,
        }
    }
    assert facts.identity.media_source == SOURCE_ID
    assert facts.identity.media_id == "native-1"
    assert media.media_source == MediaSource(SOURCE_ID)
    assert media.media_id == "native-1"


@pytest.mark.parametrize("invalid_value", [float("inf"), float("-inf"), float("nan")])
def test_registry_rejects_non_finite_json_numbers(invalid_value: float) -> None:
    """扩展事实不得接受 JSON 无法稳定表达的非有限数值。"""
    registry = _registry()
    registry.replace(PLUGIN_ID, [_all_value_type_source()])

    accepted = registry.facts(_media({f"{FIELD_PREFIX}score": invalid_value}))

    assert accepted == {}


@pytest.mark.parametrize(
    ("facts", "media_type", "expected"),
    [
        (
            {
                f"{FIELD_PREFIX}region_group": "east-asia",
                f"{FIELD_PREFIX}unknown": "ignored",
            },
            MediaType.MOVIE,
            {SOURCE_ID: {"region_group": "east-asia"}},
        ),
        (
            {
                f"{FIELD_PREFIX}region_group": "east-asia",
                "extensions.other.source.region_group": "ignored",
            },
            MediaType.MOVIE,
            {SOURCE_ID: {"region_group": "east-asia"}},
        ),
        (
            {f"{FIELD_PREFIX}region_group": ["wrong"]},
            MediaType.MOVIE,
            {},
        ),
        (
            {f"{FIELD_PREFIX}region_group": "east-asia"},
            MediaType.TV,
            {},
        ),
    ],
)
def test_registry_ignores_unregistered_cross_source_and_type_mismatches(
    facts: dict[str, object],
    media_type: MediaType,
    expected: dict[str, dict[str, object]],
) -> None:
    """无效事实按字段忽略，媒体类型不符时拒绝整批且不改写身份。"""
    registry = _registry()
    registry.replace(PLUGIN_ID, [_source()])
    media = _media(facts, media_type=media_type)

    accepted = registry.facts(media)
    projected = build_classification_facts(media, extensions=accepted)

    assert accepted == expected
    assert projected.identity.media_source == SOURCE_ID
    assert projected.identity.media_id == "native-1"
    assert media.media_source == MediaSource(SOURCE_ID)
    assert media.media_id == "native-1"


def test_configuration_service_reads_dynamic_fields_without_caching() -> None:
    """动态 provider 的新增和撤销必须立即反映到配置服务字段目录。"""
    registry = _registry()
    service = ClassificationPolicyConfigurationService(
        Mock(),
        extra_fields_provider=registry.fields,
    )

    assert service.extra_fields() == ()
    registry.replace(PLUGIN_ID, [_source()])
    assert [field.id for field in service.extra_fields()] == [f"{FIELD_PREFIX}region_group"]
    registry.remove(PLUGIN_ID)
    assert service.extra_fields() == ()


def test_policy_validation_immediately_rejects_removed_plugin_field() -> None:
    """插件撤销后保留策略内容，但再次校验必须把旧扩展字段标记为不可用。"""
    registry = _registry()
    registry.replace(PLUGIN_ID, [_source()])
    service = ClassificationPolicyConfigurationService(
        Mock(),
        extra_fields_provider=registry.fields,
    )
    policy = _extension_policy()

    before = service.validate(policy)
    assert not any(issue.code == "unknown_field" for issue in before.issues)
    registry.remove(PLUGIN_ID)
    validation = service.validate(policy)

    assert any(issue.code == "unknown_field" for issue in validation.issues)


def test_execution_service_uses_registry_provider_and_revokes_old_facts() -> None:
    """执行服务只使用当前登记事实，来源撤销后同一旧对象必须回退。"""
    registry = _registry()
    registry.replace(PLUGIN_ID, [_source()])
    service = ClassificationExecutionService(
        _Runtime(_extension_policy()),
        extension_facts_provider=registry.facts,
    )
    media = _media({f"{FIELD_PREFIX}region_group": "east-asia"})

    classified = service.finalize(media)
    registry.remove(PLUGIN_ID)
    revoked = service.finalize(media)

    assert classified.media_source == MediaSource(SOURCE_ID)
    assert classified.media_id == "native-1"
    assert classified.library_category == "扩展命中"
    assert classified.classification is not None
    assert classified.classification.effective.category_id == "movie.extension"
    assert revoked.media_source == MediaSource(SOURCE_ID)
    assert revoked.media_id == "native-1"
    assert revoked.library_category == "电影兜底"
    assert revoked.classification is not None
    assert revoked.classification.effective.category_id == "movie.fallback"
    assert media.library_category == ""


def test_extension_facts_survive_cache_roundtrip_with_identical_result() -> None:
    """插件事实经媒体缓存字典往返后应得到相同分类且保持来源身份。"""
    registry = _registry()
    registry.replace(PLUGIN_ID, [_source()])
    service = ClassificationExecutionService(
        _Runtime(_extension_policy()),
        extension_facts_provider=registry.facts,
    )
    source = _media({f"{FIELD_PREFIX}region_group": "east-asia"})
    restored = MediaInfo()
    restored.from_dict(source.to_dict())

    direct = service.finalize(source)
    cached = service.finalize(restored)

    assert cached.classification == direct.classification
    assert cached.library_category == direct.library_category == "扩展命中"
    assert cached.classification_facts == source.classification_facts
    assert cached.media_source == source.media_source == MediaSource(SOURCE_ID)
    assert cached.media_id == source.media_id == "native-1"


def test_execution_service_isolates_extension_provider_failure() -> None:
    """扩展事实端口异常不能导致识别失败，结果按缺失字段使用策略兜底。"""

    def fail_provider(_media):
        """模拟插件运行时在事实读取期间失效。"""
        raise RuntimeError("registry unavailable")

    service = ClassificationExecutionService(
        _Runtime(_extension_policy()),
        extension_facts_provider=fail_provider,
    )
    media = _media({f"{FIELD_PREFIX}region_group": "east-asia"})

    classified = service.finalize(media)

    assert classified.media_source == MediaSource(SOURCE_ID)
    assert classified.media_id == "native-1"
    assert classified.library_category == "电影兜底"
