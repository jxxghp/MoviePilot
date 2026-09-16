"""跨来源分类事实补充服务的协议、缓存和并发边界测试。"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

import pytest

from app.application.classification.enrichment import (
    ClassificationEnrichmentCacheEntry,
    ClassificationEnrichmentProvider,
    ClassificationEnrichmentService,
)
from app.domain.classification.evaluator import ClassificationEvaluator
from app.domain.classification.facts import build_classification_facts
from app.domain.context import MediaInfo
from app.schemas.category import (
    ClassificationEnrichmentMatch,
    ClassificationEnrichmentResponse,
    ClassificationPolicy,
)
from app.schemas.types import MediaSource, MediaType


class _Catalog:
    """返回固定 provider，并记录是否发生运行时能力发现。"""

    def __init__(self, *providers: ClassificationEnrichmentProvider) -> None:
        """保存按优先级排列的 provider。"""
        self._providers = tuple(providers)
        self.calls = 0

    def providers(self) -> tuple[ClassificationEnrichmentProvider, ...]:
        """记录并返回 provider 快照。"""
        self.calls += 1
        return self._providers


class _MemoryCache:
    """保存测试进程内的富化终态缓存。"""

    def __init__(self) -> None:
        """创建空缓存。"""
        self.values: dict[str, ClassificationEnrichmentCacheEntry] = {}

    def get(self, key: str) -> ClassificationEnrichmentCacheEntry | None:
        """按稳定键返回缓存项。"""
        return self.values.get(key)

    def set(self, key: str, entry: ClassificationEnrichmentCacheEntry) -> None:
        """保存已校验的有效或空响应。"""
        self.values[key] = entry


def _inline_submit(callback: Any, *args: Any) -> Future[Any]:
    """同步执行线程任务并用 Future 复刻生产提交端口。"""
    future: Future[Any] = Future()
    try:
        future.set_result(callback(*args))
    except Exception as error:  # noqa: BLE001 测试端口必须把 provider 故障保留在 Future 中
        future.set_exception(error)
    return future


def _policy(
    *,
    enrichment_mode: str = "enrich_missing",
    revision: int = 1,
) -> ClassificationPolicy:
    """构造同时引用已有和缺失标准事实的最小电影策略。"""
    return ClassificationPolicy.model_validate(
        {
            "revision": revision,
            "enrichment_mode": enrichment_mode,
            "categories": [
                {
                    "id": "movie.jp",
                    "media_type": "电影",
                    "name": "日本电影",
                    "path": ["日本电影"],
                },
                {
                    "id": "movie.other",
                    "media_type": "电影",
                    "name": "其它电影",
                    "path": ["其它电影"],
                },
            ],
            "rules": [
                {
                    "id": "rule.movie.jp",
                    "name": "日本电影",
                    "kind": "category",
                    "media_types": ["电影"],
                    "when": {
                        "all": [
                            {
                                "field": "media.year",
                                "operator": "gte",
                                "value": 2000,
                            },
                            {
                                "field": "media.countries",
                                "operator": "contains_any",
                                "value": ["JP"],
                            },
                            {
                                "field": "media.language",
                                "operator": "equals",
                                "value": "ja",
                            },
                        ]
                    },
                    "target": {"category_id": "movie.jp"},
                }
            ],
            "fallbacks": {"电影": "movie.other"},
        }
    )


def _movie() -> MediaInfo:
    """构造带可验证外部 ID、但缺少国家和语言的主来源结果。"""
    return MediaInfo(
        media_source=MediaSource.Douban,
        media_id="1291561",
        type=MediaType.MOVIE,
        title="千与千寻",
        year=2001,
        tmdb_id=129,
        imdb_id="tt0245429",
    )


def _external_response(
    *,
    source: str,
    media_id: str,
    facts: dict[str, object],
) -> ClassificationEnrichmentResponse:
    """构造由已知外部 ID 证明的 provider 响应。"""
    return ClassificationEnrichmentResponse(
        media_source=source,
        match=ClassificationEnrichmentMatch(
            kind="external_id",
            media_source=source,
            media_id=media_id,
        ),
        facts=facts,
    )


def test_primary_only_never_discovers_or_calls_enrichment_providers() -> None:
    """默认模式必须保持原识别成本，不触发 provider 目录读取。"""
    media = _movie()
    facts = build_classification_facts(media)
    catalog = _Catalog(
        ClassificationEnrichmentProvider(
            provider_id="host:themoviedb",
            provider_name="TheMovieDb",
            media_sources=(MediaSource.TMDB.value,),
            callback=lambda **_kwargs: pytest.fail("默认模式不应调用 provider"),
        )
    )
    service = ClassificationEnrichmentService(catalog, submit=_inline_submit)

    enriched = service.enrich(
        _policy(enrichment_mode="primary_only"),
        facts,
        media,
    )

    assert enriched is facts
    assert catalog.calls == 0


def test_enrichment_requests_only_missing_fields_and_merges_by_provider_order() -> None:
    """并发响应只填缺失字段，冲突值按稳定 provider 顺序决定。"""
    media = _movie()
    facts = build_classification_facts(media)
    requests: list[tuple[str, tuple[str, ...]]] = []

    def tmdb_provider(*, request: Any) -> ClassificationEnrichmentResponse:
        """返回国家并尝试夹带非请求字段。"""
        requests.append(("tmdb", tuple(request.missing_fields)))
        return _external_response(
            source=MediaSource.TMDB.value,
            media_id="129",
            facts={
                "media.countries": ["JP"],
                "media.year": 1999,
                "identity.media_source": MediaSource.TMDB.value,
            },
        )

    def imdb_provider(*, request: Any) -> ClassificationEnrichmentResponse:
        """返回冲突国家和语言，由合并器保留先到目录中的国家。"""
        requests.append(("imdb", tuple(request.missing_fields)))
        return _external_response(
            source=MediaSource.IMDb.value,
            media_id="tt0245429",
            facts={
                "media.countries": ["US"],
                "media.language": "ja",
            },
        )

    service = ClassificationEnrichmentService(
        _Catalog(
            ClassificationEnrichmentProvider(
                provider_id="host:themoviedb",
                provider_name="TheMovieDb",
                media_sources=(MediaSource.TMDB.value,),
                callback=tmdb_provider,
            ),
            ClassificationEnrichmentProvider(
                provider_id="host:imdb",
                provider_name="IMDb",
                media_sources=(MediaSource.IMDb.value,),
                callback=imdb_provider,
            ),
        ),
        submit=_inline_submit,
    )

    enriched = service.enrich(_policy(), facts, media)
    evaluation = ClassificationEvaluator.evaluate(_policy(), enriched, trace=True)

    assert requests == [
        ("tmdb", ("media.countries", "media.language")),
        ("imdb", ("media.countries", "media.language")),
    ]
    assert enriched.identity == facts.identity
    assert enriched.media.year == 2001
    assert enriched.media.countries == ["JP"]
    assert enriched.media.language == "ja"
    assert enriched.field_sources["media.countries"].provider_id == "host:themoviedb"
    assert enriched.field_sources["media.language"].provider_id == "host:imdb"
    traces = {
        condition.field: condition
        for rule in evaluation.trace
        for condition in rule.conditions
    }
    assert traces["media.countries"].source.provider_id == "host:themoviedb"
    assert traces["media.language"].source.provider_id == "host:imdb"
    assert evaluation.result.effective.category_id == "movie.jp"


def test_invalid_match_and_provider_failure_are_isolated() -> None:
    """错误证明和 provider 异常不得阻断后续有效补充或主分类。"""
    media = _movie()
    facts = build_classification_facts(media)
    warnings: list[str] = []

    def failed_provider(**_kwargs: Any) -> None:
        """模拟 provider 内部故障。"""
        raise RuntimeError("provider failed")

    def invalid_provider(**_kwargs: Any) -> ClassificationEnrichmentResponse:
        """返回不属于当前媒体的外部 ID。"""
        return _external_response(
            source=MediaSource.TMDB.value,
            media_id="999",
            facts={"media.countries": ["US"]},
        )

    def valid_provider(**_kwargs: Any) -> ClassificationEnrichmentResponse:
        """在前序故障后提供可验证事实。"""
        return _external_response(
            source=MediaSource.IMDb.value,
            media_id="tt0245429",
            facts={"media.countries": ["JP"], "media.language": "ja"},
        )

    providers = (
        ClassificationEnrichmentProvider(
            provider_id="host:failed",
            provider_name="Failed",
            media_sources=(MediaSource.TMDB.value,),
            callback=failed_provider,
        ),
        ClassificationEnrichmentProvider(
            provider_id="host:invalid",
            provider_name="Invalid",
            media_sources=(MediaSource.TMDB.value,),
            callback=invalid_provider,
        ),
        ClassificationEnrichmentProvider(
            provider_id="host:imdb",
            provider_name="IMDb",
            media_sources=(MediaSource.IMDb.value,),
            callback=valid_provider,
        ),
    )
    service = ClassificationEnrichmentService(
        _Catalog(*providers),
        submit=_inline_submit,
        logger=warnings.append,
    )

    enriched = service.enrich(_policy(), facts, media)

    assert enriched.media.countries == ["JP"]
    assert enriched.media.language == "ja"
    assert enriched.identity == facts.identity
    assert len(warnings) == 2
    assert all("classification_enrichment_error" in warning for warning in warnings)


@pytest.mark.asyncio  # type: ignore[misc]
async def test_valid_response_cache_is_shared_by_sync_and_async_and_revision_scoped() -> None:
    """同步异步共用有效缓存，但策略 revision 变化必须重新调用 provider。"""
    media = _movie()
    facts = build_classification_facts(media)
    cache = _MemoryCache()
    calls = 0

    def provider(**_kwargs: Any) -> ClassificationEnrichmentResponse:
        """记录实际 provider 调用次数。"""
        nonlocal calls
        calls += 1
        return _external_response(
            source=MediaSource.TMDB.value,
            media_id="129",
            facts={"media.countries": ["JP"], "media.language": "ja"},
        )

    service = ClassificationEnrichmentService(
        _Catalog(
            ClassificationEnrichmentProvider(
                provider_id="host:themoviedb",
                provider_name="TheMovieDb",
                media_sources=(MediaSource.TMDB.value,),
                callback=provider,
            )
        ),
        submit=_inline_submit,
        cache=cache,
    )

    sync_result = service.enrich(_policy(revision=1), facts, media)
    async_result = await service.async_enrich(_policy(revision=1), facts, media)
    revised_result = await service.async_enrich(_policy(revision=2), facts, media)

    assert calls == 2
    assert sync_result == async_result == revised_result
    assert len(cache.values) == 2


def test_sync_enrichment_enforces_total_timeout_and_concurrency_limit() -> None:
    """慢 provider 只能占用有限工作槽，且调用方按总预算及时返回。"""
    media = _movie()
    facts = build_classification_facts(media)
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    max_active = 0
    started = 0
    warnings: list[str] = []

    def slow_provider(**_kwargs: Any) -> None:
        """占用工作槽直到测试释放，并记录峰值并发。"""
        nonlocal active, max_active, started
        with lock:
            active += 1
            started += 1
            max_active = max(max_active, active)
        release.wait(1)
        with lock:
            active -= 1
        return None

    providers = tuple(
        ClassificationEnrichmentProvider(
            provider_id=f"host:slow-{index}",
            provider_name=f"Slow {index}",
            media_sources=(MediaSource.TMDB.value,),
            callback=slow_provider,
        )
        for index in range(4)
    )
    executor = ThreadPoolExecutor(max_workers=4)
    service = ClassificationEnrichmentService(
        _Catalog(*providers),
        submit=executor.submit,
        timeout_seconds=0.05,
        max_concurrency=2,
        logger=warnings.append,
    )

    started_at = time.monotonic()
    try:
        result = service.enrich(_policy(), facts, media)
        elapsed = time.monotonic() - started_at
    finally:
        release.set()
        executor.shutdown(wait=True)

    assert result == facts
    assert elapsed < 0.25
    assert started == 2
    assert max_active == 2
    assert len(warnings) == 2
    assert all("classification_enrichment_timeout" in warning for warning in warnings)
