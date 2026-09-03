"""新版媒体分类 API、预览和有界影响分析验收测试。"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TypeVar, cast

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.api.context import get_classification_runtime
from app.api.dependencies.auth import (
    get_current_active_superuser_async,
    get_current_active_user_async,
)
from app.api.endpoints import classification as classification_endpoint
from app.api.routers import API_V1_ROUTER_SPECS
from app.application.classification.analysis import (
    ClassificationAnalysisService,
    RecentHistoryClassificationSampleProvider,
)
from app.application.classification.configuration import (
    ClassificationPolicyConfigurationService,
    build_default_classification_policy,
)
from app.application.classification.contract import ClassificationPolicyConflictError
from app.application.classification.runtime import ClassificationRuntime
from app.application.history import DownloadHistorySnapshot, TransferHistorySnapshot
from app.schemas.category import (
    ClassificationCategory,
    ClassificationCondition,
    ClassificationFacts,
    ClassificationFieldDefinition,
    ClassificationMusicFacts,
    ClassificationPolicy,
    ClassificationPolicyPublishRequest,
    ClassificationPolicyRollbackRequest,
    ClassificationPolicyState,
    ClassificationPreviewRequest,
    ClassificationRule,
    ClassificationTarget,
)
from app.schemas.types import MediaSource

T = TypeVar("T")
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


class _MemoryPolicyStore:
    """以隔离快照模拟分类策略 CAS 存储。"""

    def __init__(self) -> None:
        """初始化空状态。"""
        self.state: ClassificationPolicyState | None = None

    def load(self) -> ClassificationPolicyState | None:
        """返回持久化状态副本。"""
        return self.state.model_copy(deep=True) if self.state else None

    def compare_and_set(
        self,
        *,
        expected_revision: int,
        state: ClassificationPolicyState,
    ) -> None:
        """仅在当前 revision 等于期望值时替换状态。"""
        current_revision = self.state.active.revision if self.state else 0
        if current_revision != expected_revision:
            raise ClassificationPolicyConflictError(
                expected_revision=expected_revision,
                current_revision=current_revision,
            )
        self.state = state.model_copy(deep=True)


class _InlineExecutor:
    """在测试协程中直接执行同步短操作。"""

    async def run(self, operation: Callable[[], T]) -> T:
        """执行并返回操作结果。"""
        return operation()


class _DownloadHistory:
    """提供固定下载历史列表的异步只读端口。"""

    def __init__(self, records: list[DownloadHistorySnapshot]) -> None:
        """保存测试记录。"""
        self.records = records

    async def async_list_by_page(
        self,
        page: int = 1,
        count: int = 30,
    ) -> list[DownloadHistorySnapshot]:
        """返回请求窗口内的固定记录。"""
        assert page == 1
        return self.records[:count]


class _TransferHistory:
    """提供固定整理历史列表的异步只读端口。"""

    def __init__(self, records: list[TransferHistorySnapshot]) -> None:
        """保存测试记录。"""
        self.records = records

    async def async_list_by_page(
        self,
        page: int = 1,
        count: int = 30,
        status: bool | None = None,
    ) -> list[TransferHistorySnapshot]:
        """只接受成功历史过滤并返回固定记录。"""
        assert page == 1
        assert status is True
        return self.records[:count]


def _service() -> ClassificationPolicyConfigurationService:
    """构造带固定时钟和异步执行器的已初始化策略服务。"""
    service = ClassificationPolicyConfigurationService(
        _MemoryPolicyStore(),
        clock=lambda: NOW,
        async_executor=cast(object, _InlineExecutor()),
    )
    service.initialize()
    return service


def _facts(
    *,
    media_source: str = "themoviedb",
    media_id: str = "1",
    media_type: str = "电影",
    title: str = "示例",
    year: int | None = 2024,
) -> ClassificationFacts:
    """构造预览和影响分析使用的最小标准事实。"""
    music = (
        ClassificationMusicFacts(entity_type="album")
        if media_type == "音乐"
        else None
    )
    return ClassificationFacts.model_validate(
        {
            "identity": {
                "media_source": media_source,
                "media_id": media_id,
            },
            "media": {
                "type": media_type,
                "title": title,
                "year": year,
            },
            "music": music,
        }
    )


def _candidate_policy() -> ClassificationPolicy:
    """构造把 2024 年电影从兜底分类迁移到新分类的合法草稿。"""
    policy = build_default_classification_policy()
    policy.categories.append(
        ClassificationCategory(
            id="movie.2024",
            media_type="电影",
            name="2024 电影",
            path=["年代", "2024"],
        )
    )
    policy.rules.append(
        ClassificationRule(
            id="rule.movie.2024",
            name="2024 年电影",
            kind="category",
            media_types=["电影"],
            when=ClassificationCondition(
                field="media.year",
                operator="equals",
                value=2024,
            ),
            target=ClassificationTarget(category_id="movie.2024"),
        )
    )
    return policy


def _response_payload(response: JSONResponse) -> dict[str, object]:
    """解析端点返回的结构化 JSON 错误响应。"""
    return cast(dict[str, object], json.loads(response.body))


def test_field_catalog_exposes_typed_options_and_server_limits() -> None:
    """字段 API 数据足以驱动前端控件，无需硬编码值类型和结构限制。"""
    catalog = ClassificationAnalysisService(_service()).fields()
    media_type = next(field for field in catalog.fields if field.id == "media.type")

    assert media_type.value_type == "enum"
    assert media_type.allow_custom_values is False
    assert [(option.value, option.label) for option in media_type.options] == [
        ("电影", "电影"),
        ("电视剧", "电视剧"),
        ("音乐", "音乐"),
    ]
    assert catalog.limits.max_condition_depth == 3
    assert catalog.limits.max_conditions_per_rule == 30
    assert catalog.limits.max_rules == 1000
    assert catalog.retired_fields == []


def test_field_catalog_exposes_retired_fields_outside_new_rule_options() -> None:
    """字段 API 应保留旧规则解析信息，但不得把它混入新条件选项。"""
    retired = ClassificationFieldDefinition(
        id="extensions.themoviedb.genre_ids",
        label="风格（旧规则）",
        group="旧规则",
        value_type="string_list",
        operators=["contains_any"],
        media_types=["电影"],
        source_support={"themoviedb": "extension"},
        selectable=False,
        replacement_field="media.genre_keys",
    )
    configuration = ClassificationPolicyConfigurationService(
        _MemoryPolicyStore(),
        extra_fields=[retired],
        clock=lambda: NOW,
    )
    configuration.initialize()

    catalog = ClassificationAnalysisService(configuration).fields()

    assert retired.id not in {field.id for field in catalog.fields}
    assert [field.id for field in catalog.retired_fields] == [retired.id]
    assert catalog.retired_fields[0].replacement_field == "media.genre_keys"


def test_preview_returns_condition_path_and_structured_missing_fact_warning() -> None:
    """预览 trace 能定位规则条件，缺失字段提示保留代码、字段和来源。"""
    service = _service()
    policy = build_default_classification_policy()
    policy.rules.append(
        ClassificationRule(
            id="rule.language",
            name="日语",
            kind="label",
            media_types=["电影"],
            when=ClassificationCondition(
                field="media.language",
                operator="equals",
                value="ja",
            ),
            target=ClassificationTarget(labels=["日语"]),
        )
    )

    evaluation = ClassificationAnalysisService(service).preview(
        ClassificationPreviewRequest(
            policy=policy,
            input={"kind": "facts", "facts": _facts()},
        )
    )

    assert evaluation.trace[0].conditions[0].path == ["rules", 0, "when"]
    assert evaluation.warnings[0].code == "missing_fact"
    assert evaluation.warnings[0].field == "media.language"
    assert evaluation.warnings[0].source == "themoviedb"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_recent_history_samples_are_bounded_deduplicated_and_honest() -> None:
    """近期历史样本按身份去重，脏记录跳过，并明确只包含基础事实。"""
    provider = RecentHistoryClassificationSampleProvider(
        download_history=cast(
            object,
            _DownloadHistory(
                [
                    DownloadHistorySnapshot(
                        id=1,
                        path="/downloads/a",
                        type="电影",
                        title="较旧",
                        year="2024",
                        media_source=MediaSource.TMDB,
                        media_id="10",
                        date="2026-09-01 10:00:00",
                    ),
                    DownloadHistorySnapshot(
                        id=2,
                        path="/downloads/b",
                        type="电影",
                        title="无身份",
                        date="2026-09-01 09:00:00",
                    ),
                ]
            ),
        ),
        transfer_history=cast(
            object,
            _TransferHistory(
                [
                    TransferHistorySnapshot(
                        id=3,
                        type="电影",
                        title="较新",
                        year="2024",
                        media_source=MediaSource.TMDB,
                        media_id="10",
                        date="2026-09-02 10:00:00",
                    ),
                    TransferHistorySnapshot(
                        id=4,
                        type="音乐",
                        title="专辑",
                        year="2023",
                        media_source=MediaSource.MusicBrainz,
                        media_id="album-1",
                        music_type="album",
                        date="2026-09-02 09:00:00",
                    ),
                ]
            ),
        ),
    )

    batch = await provider.load(10)

    assert batch.scanned_count == 4
    assert batch.skipped_count == 2
    assert [item.media.title for item in batch.facts] == ["较新", "专辑"]
    assert batch.facts[1].music is not None
    assert batch.facts[1].music.entity_type == "album"
    assert "仅稳定保存" in batch.warnings[0]


@pytest.mark.asyncio  # type: ignore[misc]
async def test_impact_analysis_checks_revision_and_preserves_statistics() -> None:
    """影响分析以活动 revision 为基线，并保持总量、分组和变化示例一致。"""
    service = _service()
    analysis = ClassificationAnalysisService(service)
    samples = [
        _facts(),
        _facts(
            media_source="musicbrainz",
            media_id="album-1",
            media_type="音乐",
            title="专辑",
            year=2023,
        ),
    ]

    result = await analysis.impact(
        _candidate_policy(),
        expected_revision=1,
        sample_limit=100,
        example_limit=20,
        samples=samples,
    )

    assert result.baseline_revision == 1
    assert result.candidate_revision == 2
    assert result.sample_count == 2
    assert result.changed_count == 1
    assert result.unchanged_count == 1
    assert sum(group.sampled for group in result.groups) == 2
    assert result.changes[0].changed_fields[:2] == [
        "category_id",
        "category_path",
    ]
    assert result.changes[0].candidate.recommended is not None
    assert result.changes[0].candidate.recommended.category_id == "movie.2024"

    with pytest.raises(ClassificationPolicyConflictError):
        await analysis.impact(
            _candidate_policy(),
            expected_revision=0,
            sample_limit=100,
            example_limit=20,
            samples=samples,
        )


@pytest.mark.asyncio  # type: ignore[misc]
async def test_new_api_returns_structured_conflict_and_validation_data() -> None:
    """发布冲突和领域校验失败使用真实状态码并把结构化详情放入 data。"""
    service = _service()
    runtime = ClassificationRuntime(service)
    conflict = await classification_endpoint.publish_policy(
        ClassificationPolicyPublishRequest(
            expected_revision=0,
            policy=_candidate_policy(),
        ),
        object(),
        runtime,
    )
    assert isinstance(conflict, JSONResponse)
    assert conflict.status_code == 409
    conflict_payload = _response_payload(conflict)
    assert cast(dict[str, object], conflict_payload["data"])["current_revision"] == 1

    invalid = _candidate_policy()
    invalid.fallbacks.pop("音乐")
    rejected = await classification_endpoint.publish_policy(
        ClassificationPolicyPublishRequest(
            expected_revision=1,
            policy=invalid,
        ),
        object(),
        runtime,
    )
    assert isinstance(rejected, JSONResponse)
    assert rejected.status_code == 422
    validation_payload = _response_payload(rejected)
    issues = cast(dict[str, object], validation_payload["data"])["issues"]
    assert any(
        cast(dict[str, object], issue)["code"] == "missing_fallback"
        for issue in cast(list[object], issues)
    )
    assert runtime.require_policy().revision == 1


@pytest.mark.asyncio  # type: ignore[misc]
async def test_new_api_history_and_rollback_publish_monotonic_revision() -> None:
    """历史不含活动版本，回滚历史内容时生成新的单调 revision。"""
    service = _service()
    runtime = ClassificationRuntime(service)
    published = await runtime.publish_policy(
        _candidate_policy(),
        expected_revision=1,
    )
    history = await classification_endpoint.get_history(object(), runtime)

    assert published.revision == 2
    assert history.active_revision == 2
    assert [item.revision for item in history.items] == [1]

    rolled_back = await classification_endpoint.rollback_policy(
        1,
        ClassificationPolicyRollbackRequest(expected_revision=2),
        object(),
        runtime,
    )

    assert not isinstance(rolled_back, JSONResponse)
    assert rolled_back.restored_from_revision == 1
    assert rolled_back.policy.revision == 3
    assert rolled_back.policy.categories == build_default_classification_policy().categories


def test_classification_router_uses_documented_media_prefix() -> None:
    """新版分类端点独立注册在设计文档约定的 media/classification 前缀。"""
    spec = next(
        item
        for item in API_V1_ROUTER_SPECS
        if item.router is classification_endpoint.router
    )

    assert spec.prefix == "/media/classification"
    assert {route.path for route in classification_endpoint.router.routes} == {
        "/policy",
        "/fields",
        "/validate",
        "/preview",
        "/impact",
        "/history",
        "/rollback/{revision}",
    }


def test_classification_routes_bind_documented_permissions() -> None:
    """三个只读接口使用普通登录用户，其余管理接口要求超级管理员。"""
    def dependency_names(path: str, method: str) -> set[str]:
        """返回一个分类路由直接声明的依赖函数名。"""
        route = next(
            item
            for item in classification_endpoint.router.routes
            if item.path == path and method in item.methods
        )
        return {
            getattr(dependency.call, "__name__", "")
            for dependency in route.dependant.dependencies
        }

    assert "get_current_active_user_async" in dependency_names("/policy", "GET")
    assert "get_current_active_user_async" in dependency_names("/fields", "GET")
    assert "get_current_active_user_async" in dependency_names("/preview", "POST")
    for path, method in (
        ("/policy", "PUT"),
        ("/validate", "POST"),
        ("/impact", "POST"),
        ("/history", "GET"),
        ("/rollback/{revision}", "POST"),
    ):
        assert "get_current_active_superuser_async" in dependency_names(path, method)


def test_fastapi_envelope_and_conflict_payload_are_frontend_compatible() -> None:
    """真实路由成功响应只包装一次，409 详情位于标准 envelope.data。"""
    service = _service()
    runtime = ClassificationRuntime(service)
    app = FastAPI()
    app.include_router(
        classification_endpoint.router,
        prefix="/api/v1/media/classification",
    )

    async def current_user() -> object:
        """返回普通登录用户测试替身。"""
        return object()

    async def superuser() -> object:
        """返回超级管理员测试替身。"""
        return object()

    app.dependency_overrides[get_current_active_user_async] = current_user
    app.dependency_overrides[get_current_active_superuser_async] = superuser
    app.dependency_overrides[get_classification_runtime] = lambda: runtime

    client = TestClient(app)
    policy_response = client.get("/api/v1/media/classification/policy")
    conflict_response = client.put(
        "/api/v1/media/classification/policy",
        json={
            "expected_revision": 0,
            "policy": _candidate_policy().model_dump(mode="json"),
        },
    )

    assert policy_response.status_code == 200
    assert policy_response.json()["data"]["revision"] == 1
    assert "success" not in policy_response.json()["data"]
    assert conflict_response.status_code == 409
    assert conflict_response.json()["data"] == {
        "code": "classification_revision_conflict",
        "expected_revision": 0,
        "current_revision": 1,
    }
