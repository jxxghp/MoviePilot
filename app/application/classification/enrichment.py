"""缺失标准分类事实的可选跨来源补充服务。"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, TypeAlias, cast

from pydantic import ValidationError

from app.domain.classification.evaluator import read_fact
from app.domain.classification.fields import (
    classification_fact_matches_definition,
    field_definition_map,
)
from app.schemas.category import (
    ClassificationCondition,
    ClassificationConditionGroup,
    ClassificationConditionNode,
    ClassificationEnrichmentRequest,
    ClassificationEnrichmentResponse,
    ClassificationFacts,
    ClassificationFactSource,
    ClassificationFieldDefinition,
    ClassificationPolicy,
)
from app.schemas.types import MediaSource

_ENRICHMENT_METHOD = "get_media_classification_facts"
_DEFAULT_TIMEOUT_SECONDS = 3.0
_DEFAULT_MAX_CONCURRENCY = 3


@dataclass(frozen=True, slots=True)
class ClassificationEnrichmentProvider:
    """绑定一个运行中模块的稳定身份、来源范围和补充方法。"""

    provider_id: str
    provider_name: str
    media_sources: tuple[str, ...]
    callback: Callable[..., object]


class ClassificationEnrichmentProviderCatalog(Protocol):
    """按调用时运行状态返回补充 provider 快照。"""

    def providers(self) -> tuple[ClassificationEnrichmentProvider, ...]:
        """返回按稳定优先级排列的当前 provider。"""
        ...


@dataclass(frozen=True, slots=True)
class ClassificationEnrichmentCacheEntry:
    """区分缓存未命中、空响应和有效响应。"""

    response: ClassificationEnrichmentResponse | None


class ClassificationEnrichmentCache(Protocol):
    """补充结果 TTL 缓存的最小同步端口。"""

    def get(self, key: str) -> ClassificationEnrichmentCacheEntry | None:
        """读取缓存项；不存在时返回空值。"""
        ...

    def set(self, key: str, entry: ClassificationEnrichmentCacheEntry) -> None:
        """写入已校验响应或确定性空响应。"""
        ...


ProviderSubmit: TypeAlias = Callable[..., Future[Any]]
"""向进程托管线程池提交同步 provider 的端口。"""

ClassificationEnrichmentLogger: TypeAlias = Callable[[str], None]
"""接收不会阻断分类流程的补充诊断。"""


@dataclass(frozen=True, slots=True)
class _ProviderCall:
    """保存一个 provider 的裁剪请求和缓存身份。"""

    index: int
    provider: ClassificationEnrichmentProvider
    request: ClassificationEnrichmentRequest
    cache_key: str


class _NoopEnrichmentCache:
    """未注入缓存时保持接口语义的空实现。"""

    @staticmethod
    def get(key: str) -> None:
        """始终返回未命中。"""
        del key
        return None

    @staticmethod
    def set(key: str, entry: ClassificationEnrichmentCacheEntry) -> None:
        """忽略写入。"""
        del key, entry


class ClassificationEnrichmentService:
    """在有界预算内并发补充缺失事实，并保持主身份与已有事实不变。"""

    def __init__(
        self,
        catalog: ClassificationEnrichmentProviderCatalog,
        *,
        submit: ProviderSubmit,
        cache: ClassificationEnrichmentCache | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_concurrency: int = _DEFAULT_MAX_CONCURRENCY,
        logger: ClassificationEnrichmentLogger | None = None,
    ) -> None:
        """保存延迟 provider 目录、线程端口和有界执行参数。"""
        if timeout_seconds <= 0:
            raise ValueError("分类事实补充总超时必须大于 0")
        if max_concurrency <= 0:
            raise ValueError("分类事实补充并发上限必须大于 0")
        self._catalog = catalog
        self._submit = submit
        self._cache = cache or _NoopEnrichmentCache()
        self._timeout_seconds = timeout_seconds
        self._max_concurrency = max_concurrency
        self._logger = logger

    def enrich(
        self,
        policy: ClassificationPolicy,
        facts: ClassificationFacts,
        media: object,
    ) -> ClassificationFacts:
        """同步执行补全；默认模式或没有缺失引用时不发现 provider。"""
        calls = self._build_calls(policy, facts, media)
        if not calls:
            return facts
        responses = self._run_sync(calls)
        return self._merge_responses(facts, calls, responses)

    async def async_enrich(
        self,
        policy: ClassificationPolicy,
        facts: ClassificationFacts,
        media: object,
    ) -> ClassificationFacts:
        """异步执行相同补全合同，同步 provider 在线程中有界并发运行。"""
        calls = self._build_calls(policy, facts, media)
        if not calls:
            return facts
        responses = await self._run_async(calls)
        return self._merge_responses(facts, calls, responses)

    def _build_calls(
        self,
        policy: ClassificationPolicy,
        facts: ClassificationFacts,
        media: object,
    ) -> tuple[_ProviderCall, ...]:
        """按策略、缺失事实和 provider 来源能力裁剪独立请求。"""
        if policy.enrichment_mode != "enrich_missing":
            return ()
        missing_fields = _referenced_missing_standard_fields(policy, facts)
        if not missing_fields:
            return ()
        providers = self._catalog.providers()
        if not providers:
            return ()
        definitions = field_definition_map()
        external_ids = _external_identities(media)
        calls: list[_ProviderCall] = []
        for provider in providers:
            eligible_fields = _provider_fields(
                provider,
                missing_fields,
                definitions,
                primary_source=facts.identity.media_source,
            )
            if not eligible_fields:
                continue
            request = ClassificationEnrichmentRequest(
                identity=facts.identity.model_copy(deep=True),
                media_type=facts.media.type,
                missing_fields=list(eligible_fields),
                external_ids=external_ids,
                policy_revision=policy.revision,
                timeout_seconds=self._timeout_seconds,
            )
            calls.append(
                _ProviderCall(
                    index=len(calls),
                    provider=provider,
                    request=request,
                    cache_key=_cache_key(provider, request),
                )
            )
        return tuple(calls)

    def _run_sync(
        self,
        calls: Sequence[_ProviderCall],
    ) -> dict[int, ClassificationEnrichmentResponse | None]:
        """按并发上限滚动提交同步 provider，并在总预算到期时停止等待。"""
        responses: dict[int, ClassificationEnrichmentResponse | None] = {}
        queue: list[_ProviderCall] = []
        for call in calls:
            cached = self._cache.get(call.cache_key)
            if cached is not None:
                responses[call.index] = cached.response
            else:
                queue.append(call)
        if not queue:
            return responses

        deadline = time.monotonic() + self._timeout_seconds
        pending: dict[Future[Any], _ProviderCall] = {}
        next_index = 0

        def submit_next() -> None:
            """在还有执行容量时提交下一个稳定顺序 provider。"""
            nonlocal next_index
            if next_index >= len(queue):
                return
            call = queue[next_index]
            next_index += 1
            pending[self._submit(self._invoke, call, deadline)] = call

        for _ in range(min(self._max_concurrency, len(queue))):
            submit_next()
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, _ = wait(
                tuple(pending),
                timeout=remaining,
                return_when=FIRST_COMPLETED,
            )
            if not done:
                break
            for future in done:
                call = pending.pop(future)
                self._record_future(call, future, responses)
                submit_next()
        for future, call in pending.items():
            future.cancel()
            self._warn(
                f"classification_enrichment_timeout provider={call.provider.provider_id}"
            )
        return responses

    async def _run_async(
        self,
        calls: Sequence[_ProviderCall],
    ) -> dict[int, ClassificationEnrichmentResponse | None]:
        """包装宿主管理的线程 Future，并以 asyncio 总超时封口。"""
        responses: dict[int, ClassificationEnrichmentResponse | None] = {}
        queue: list[_ProviderCall] = []
        for call in calls:
            cached = self._cache.get(call.cache_key)
            if cached is not None:
                responses[call.index] = cached.response
            else:
                queue.append(call)
        if not queue:
            return responses

        deadline = time.monotonic() + self._timeout_seconds
        loop = asyncio.get_running_loop()
        pending: dict[asyncio.Future[Any], _ProviderCall] = {}
        next_index = 0

        def submit_next() -> None:
            """在宿主管理线程池还有执行容量时提交下一个 provider。"""
            nonlocal next_index
            if next_index >= len(queue):
                return
            call = queue[next_index]
            next_index += 1
            future = self._submit(self._invoke, call, deadline)
            pending[asyncio.wrap_future(future, loop=loop)] = call

        for _ in range(min(self._max_concurrency, len(queue))):
            submit_next()
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, _ = await asyncio.wait(
                tuple(pending),
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                break
            for future in done:
                call = pending.pop(future)
                self._record_async_future(call, future, responses)
                submit_next()
        for future in pending:
            future.cancel()
            future.add_done_callback(_consume_task_result)
        if pending:
            self._warn(
                f"classification_enrichment_timeout providers={len(pending)}"
            )
        return responses

    def _invoke(
        self,
        call: _ProviderCall,
        deadline: float,
    ) -> ClassificationEnrichmentResponse | None:
        """调用并严格校验一个同步 provider 响应。"""
        remaining = max(0.001, deadline - time.monotonic())
        request = call.request.model_copy(
            deep=True,
            update={"timeout_seconds": remaining},
        )
        raw = call.provider.callback(request=request)
        if inspect.isawaitable(raw):
            close = getattr(raw, "close", None)
            if callable(close):
                close()
            raise TypeError(
                f"{_ENRICHMENT_METHOD} 必须是同步方法；异步识别会在线程中调用它"
            )
        if raw is None:
            return None
        try:
            response = ClassificationEnrichmentResponse.model_validate(raw)
        except ValidationError as error:
            raise ValueError("分类事实 provider 响应结构无效") from error
        return _validated_response(call.provider, request, response)

    def _record_future(
        self,
        call: _ProviderCall,
        future: Future[Any],
        responses: dict[int, ClassificationEnrichmentResponse | None],
    ) -> None:
        """隔离一个同步 Future 的错误并缓存已校验终态。"""
        try:
            response = cast(ClassificationEnrichmentResponse | None, future.result())
        except Exception as error:  # noqa: BLE001  provider 故障必须隔离
            self._warn(
                f"classification_enrichment_error provider={call.provider.provider_id} error={error}"
            )
            return
        responses[call.index] = response
        self._cache.set(
            call.cache_key,
            ClassificationEnrichmentCacheEntry(response=response),
        )

    def _record_async_future(
        self,
        call: _ProviderCall,
        future: asyncio.Future[Any],
        responses: dict[int, ClassificationEnrichmentResponse | None],
    ) -> None:
        """隔离异步包装 Future 的错误并缓存已校验终态。"""
        try:
            response = cast(ClassificationEnrichmentResponse | None, future.result())
        except Exception as error:  # noqa: BLE001  provider 故障必须隔离
            self._warn(
                f"classification_enrichment_error provider={call.provider.provider_id} error={error}"
            )
            return
        responses[call.index] = response
        self._cache.set(
            call.cache_key,
            ClassificationEnrichmentCacheEntry(response=response),
        )

    @staticmethod
    def _merge_responses(
        facts: ClassificationFacts,
        calls: Sequence[_ProviderCall],
        responses: Mapping[int, ClassificationEnrichmentResponse | None],
    ) -> ClassificationFacts:
        """按 provider 目录顺序只填充仍缺失的事实并记录提供来源。"""
        merged = facts.model_copy(deep=True)
        for call in calls:
            response = responses.get(call.index)
            if response is None:
                continue
            for field_id, value in response.facts.items():
                _, missing = read_fact(merged, field_id)
                if not missing:
                    continue
                if not _set_standard_fact(merged, field_id, value):
                    continue
                merged.field_sources[field_id] = ClassificationFactSource(
                    media_source=response.media_source,
                    provider_id=call.provider.provider_id,
                    provider_name=call.provider.provider_name,
                )
        return cast(ClassificationFacts, merged)

    def _warn(self, message: str) -> None:
        """尽力记录诊断，日志端口失败也不得影响识别。"""
        if self._logger is None:
            return
        try:
            self._logger(message)
        except Exception:  # noqa: BLE001  诊断不能成为分类硬依赖
            return


def _referenced_missing_standard_fields(
    policy: ClassificationPolicy,
    facts: ClassificationFacts,
) -> tuple[str, ...]:
    """返回当前媒体实际适用规则中引用且缺失的标准事实。"""
    definitions = field_definition_map()
    media_type = facts.media.type
    media_source = facts.identity.media_source
    fields: list[str] = []
    for rule in policy.rules:
        if not rule.enabled or media_type not in rule.media_types:
            continue
        if rule.sources and media_source not in rule.sources:
            continue
        for field_id in _condition_field_ids(rule.when):
            if field_id.startswith(("identity.", "extensions.")):
                continue
            definition = definitions.get(field_id)
            if definition is None or media_type not in definition.media_types:
                continue
            _, missing = read_fact(facts, field_id)
            if missing and field_id not in fields:
                fields.append(field_id)
    return tuple(fields)


def _condition_field_ids(node: ClassificationConditionNode) -> tuple[str, ...]:
    """按策略声明顺序递归返回条件树叶子字段。"""
    if isinstance(node, ClassificationCondition):
        return (node.field,)
    if not isinstance(node, ClassificationConditionGroup):
        return ()
    if node.all is not None:
        children: Sequence[ClassificationConditionNode] = node.all
    elif node.any is not None:
        children = node.any
    elif node.not_ is not None:
        children = (node.not_,)
    else:
        children = ()
    return tuple(
        field_id
        for child in children
        for field_id in _condition_field_ids(child)
    )


def _provider_fields(
    provider: ClassificationEnrichmentProvider,
    missing_fields: Sequence[str],
    definitions: Mapping[str, ClassificationFieldDefinition],
    *,
    primary_source: str,
) -> tuple[str, ...]:
    """按 provider 来源能力过滤不能跨源提供的字段。"""
    candidate_sources = tuple(
        source for source in provider.media_sources if source != primary_source
    )
    if provider.media_sources and not candidate_sources:
        return ()
    fields: list[str] = []
    for field_id in missing_fields:
        definition = definitions[field_id]
        if not candidate_sources:
            fields.append(field_id)
            continue
        if any(
            definition.source_support.get(source) != "unavailable"
            for source in candidate_sources
        ):
            fields.append(field_id)
    return tuple(fields)


def _validated_response(
    provider: ClassificationEnrichmentProvider,
    request: ClassificationEnrichmentRequest,
    response: ClassificationEnrichmentResponse,
) -> ClassificationEnrichmentResponse:
    """验证来源所有权、同媒体证明、请求字段和值类型。"""
    source_id = response.media_source.strip()
    if not source_id or source_id == request.identity.media_source:
        raise ValueError("补充 provider 必须返回不同于主身份的有效媒体来源")
    if provider.media_sources and source_id not in provider.media_sources:
        raise ValueError("补充 provider 返回了未声明拥有的媒体来源")
    _validate_match(request, response)

    definitions = field_definition_map()
    accepted: dict[str, Any] = {}
    for field_id, value in response.facts.items():
        if field_id not in request.missing_fields or field_id.startswith(
            ("identity.", "extensions.")
        ):
            continue
        definition = definitions.get(field_id)
        if definition is None or request.media_type not in definition.media_types:
            continue
        support = definition.source_support.get(source_id)
        if support == "unavailable":
            continue
        if value is None or not classification_fact_matches_definition(
            value, definition
        ):
            continue
        accepted[field_id] = value
    return cast(
        ClassificationEnrichmentResponse,
        response.model_copy(deep=True, update={"facts": accepted}),
    )


def _validate_match(
    request: ClassificationEnrichmentRequest,
    response: ClassificationEnrichmentResponse,
) -> None:
    """只接受已知外部 ID 或对主身份的明确映射证明。"""
    match = response.match
    if match.kind == "external_id":
        expected = request.external_ids.get(match.media_source)
        if expected is None or expected != match.media_id:
            raise ValueError("补充结果没有命中请求中已知的外部 ID")
        return
    if (
        match.media_source != request.identity.media_source
        or match.media_id != request.identity.media_id
    ):
        raise ValueError("明确映射必须回指完整主媒体身份")


def _external_identities(media: object) -> dict[str, str]:
    """从标准媒体对象读取可独立验证的来源专用外部 ID。"""
    fields = (
        (MediaSource.TMDB.value, "tmdb_id"),
        (MediaSource.IMDb.value, "imdb_id"),
        (MediaSource.TVDB.value, "tvdb_id"),
        (MediaSource.Douban.value, "douban_id"),
        (MediaSource.Bangumi.value, "bangumi_id"),
        (MediaSource.AniList.value, "anilist_id"),
    )
    identities: dict[str, str] = {}
    for source, attribute in fields:
        value = getattr(media, attribute, None)
        text = _enum_text(value)
        if text:
            identities[source] = text
    if isrc := _enum_text(getattr(media, "isrc", None)):
        identities["isrc"] = isrc
    return identities


def _set_standard_fact(
    facts: ClassificationFacts,
    field_id: str,
    value: object,
) -> bool:
    """把已校验值写入 media 或 music 标准事实的一层字段。"""
    namespace, separator, attribute = field_id.partition(".")
    if not separator or namespace not in {"media", "music"} or not attribute:
        return False
    target = getattr(facts, namespace, None)
    if target is None or not hasattr(target, attribute):
        return False
    setattr(target, attribute, value)
    return True


def _cache_key(
    provider: ClassificationEnrichmentProvider,
    request: ClassificationEnrichmentRequest,
) -> str:
    """生成包含策略、身份、字段集合和 provider 的稳定缓存键。"""
    payload = {
        "provider_id": provider.provider_id,
        "identity": request.identity.model_dump(mode="json"),
        "media_type": request.media_type,
        "missing_fields": sorted(request.missing_fields),
        "external_ids": dict(sorted(request.external_ids.items())),
        "policy_revision": request.policy_revision,
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _enum_text(value: object) -> str:
    """把字符串枚举或普通值转换为稳定文本。"""
    if isinstance(value, Enum):
        value = value.value
    return str(value or "").strip()


def _consume_task_result(future: asyncio.Future[Any]) -> None:
    """消费超时后线程 Future 包装的终态，避免未读取异常告警。"""
    if future.cancelled():
        return
    try:
        future.exception()
    except (asyncio.CancelledError, Exception):
        return
