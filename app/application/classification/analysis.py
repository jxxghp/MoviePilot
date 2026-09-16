"""分类字段目录、预览和近期历史影响分析应用服务。"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from types import SimpleNamespace
from typing import Any, Literal, Optional, Protocol, TypeAlias, cast

from app.application.classification.catalog import (
    build_classification_field_catalog,
    build_retired_classification_field_catalog,
)
from app.application.classification.configuration import (
    ClassificationPolicyConfigurationService,
    ClassificationPolicyValidationError,
)
from app.application.classification.contract import ClassificationPolicyConflictError
from app.application.classification.execution import (
    ClassificationExecutionPort,
    evaluate_classification_facts,
)
from app.application.history import (
    DownloadHistoryQueryPort,
    DownloadHistorySnapshot,
    TransferHistoryQueryPort,
    TransferHistorySnapshot,
)
from app.domain.classification.facts import build_classification_facts
from app.domain.classification.fields import field_definition_map
from app.domain.classification.validation import (
    MAX_CATEGORY_DEPTH,
    MAX_CATEGORY_PATH_LENGTH,
    MAX_CATEGORY_SEGMENT_LENGTH,
    MAX_CONDITION_DEPTH,
    MAX_CONDITIONS_PER_RULE,
    MAX_RULES,
    MAX_TOTAL_CONDITIONS,
)
from app.schemas.category import (
    ClassificationEvaluation,
    ClassificationFacts,
    ClassificationFactValue,
    ClassificationFieldCatalog,
    ClassificationIdentityFacts,
    ClassificationImpactAnalysis,
    ClassificationImpactChange,
    ClassificationImpactGroup,
    ClassificationMediaFacts,
    ClassificationMediaType,
    ClassificationMusicFacts,
    ClassificationPolicy,
    ClassificationPolicyLimits,
    ClassificationPreviewInput,
    ClassificationPreviewRequest,
    ClassificationResult,
    ClassificationValidationResult,
)
from app.schemas.context import MediaInfo as SchemaMediaInfo
from app.schemas.music import MusicInfo as SchemaMusicInfo

_UNCLASSIFIED_CATEGORY_ID = "__unclassified__"
_DEFAULT_RESOLVE_CONCURRENCY = 3

ClassificationImpactFactsResolver: TypeAlias = Callable[
    [DownloadHistorySnapshot | TransferHistorySnapshot],
    Awaitable[ClassificationFacts | None],
]
"""按历史记录重新读取完整媒体信息的异步端口。"""


@dataclass(frozen=True, slots=True)
class ClassificationImpactSampleBatch:
    """保存一次影响分析使用的有界事实集合及其采样边界。"""

    source: Literal["request", "recent_history"]
    facts: tuple[ClassificationFacts, ...]
    scanned_count: int
    skipped_count: int
    unresolved_count: int = 0
    truncated: bool = False
    warnings: tuple[str, ...] = ()


class ClassificationImpactSampleProvider(Protocol):
    """为影响分析异步提供近期标准分类事实样本。"""

    async def load(self, limit: int) -> ClassificationImpactSampleBatch:
        """读取最多 ``limit`` 个按媒体身份去重的近期事实。"""
        ...


class RecentHistoryClassificationSampleProvider:
    """从近期下载和成功整理历史构造有限的标准分类事实。"""

    def __init__(
        self,
        *,
        download_history: DownloadHistoryQueryPort,
        transfer_history: TransferHistoryQueryPort,
        facts_resolver: ClassificationImpactFactsResolver | None = None,
        resolve_concurrency: int = _DEFAULT_RESOLVE_CONCURRENCY,
    ) -> None:
        """保存由 API 请求或宿主运行时提供的只读历史端口。"""
        if resolve_concurrency <= 0:
            raise ValueError("影响分析详情读取并发上限必须大于 0")
        self._download_history = download_history
        self._transfer_history = transfer_history
        self._facts_resolver = facts_resolver
        self._resolve_concurrency = resolve_concurrency

    async def load(self, limit: int) -> ClassificationImpactSampleBatch:
        """合并两类近期历史，按时间和 ID 排序后去重并投影事实。"""
        downloads, transfers = await asyncio.gather(
            self._download_history.async_list_by_page(page=1, count=limit),
            self._transfer_history.async_list_by_page(
                page=1,
                count=limit,
                status=True,
            ),
        )
        records = [
            *(_HistorySampleRecord("download", item.date, item.id, item) for item in downloads),
            *(_HistorySampleRecord("transfer", item.date, item.id, item) for item in transfers),
        ]
        records.sort(
            key=lambda item: (item.date or "", item.record_id, item.kind),
            reverse=True,
        )
        unique_records: list[tuple[_HistorySampleRecord, ClassificationFacts]] = []
        seen: set[tuple[str, str, str, str]] = set()
        skipped_count = 0
        for record in records:
            projected = _history_facts(record.payload)
            if projected is None:
                skipped_count += 1
                continue
            identity = projected.identity
            entity_type = projected.music.entity_type if projected.music else ""
            identity_key = (
                identity.media_source,
                identity.media_id,
                projected.media.type,
                entity_type or "",
            )
            if identity_key in seen:
                skipped_count += 1
                continue
            seen.add(identity_key)
            unique_records.append((record, projected))

        if self._facts_resolver is None:
            facts = [projected for _, projected in unique_records[:limit]]
            skipped_count += max(0, len(unique_records) - len(facts))
            return ClassificationImpactSampleBatch(
                source="recent_history",
                facts=tuple(facts),
                scanned_count=len(records),
                skipped_count=skipped_count,
                truncated=len(unique_records) > limit,
                warnings=(
                    "近期历史仅稳定保存媒体身份、类型、标题和年份；其它字段缺失时相关规则不会命中",
                ),
            )

        records_to_resolve = unique_records[:limit]
        skipped_count += max(0, len(unique_records) - len(records_to_resolve))
        resolved = await self._resolve_records(records_to_resolve)
        facts = []
        unresolved_count = 0
        for item in resolved:
            if item is None:
                unresolved_count += 1
                skipped_count += 1
                continue
            facts.append(item)

        warnings = [
            "系统会按近期下载和整理记录中的来源与编号重新读取完整媒体信息；无法读取的记录不会参与比较",
        ]
        if unresolved_count:
            warnings.append(
                f"{unresolved_count} 条记录无法获取完整媒体信息，未纳入比较",
            )
        if len(unique_records) > limit:
            warnings.append(f"符合条件的记录超过 {limit} 条，本次最多比较 {limit} 条")
        return ClassificationImpactSampleBatch(
            source="recent_history",
            facts=tuple(facts),
            scanned_count=len(records),
            skipped_count=skipped_count,
            unresolved_count=unresolved_count,
            truncated=len(unique_records) > limit,
            warnings=tuple(warnings),
        )

    async def _resolve_records(
        self,
        records: Sequence[tuple[_HistorySampleRecord, ClassificationFacts]],
    ) -> list[ClassificationFacts | None]:
        """以固定并发上限重新读取详情，并拒绝身份不一致的返回值。"""
        facts_resolver = self._facts_resolver
        if facts_resolver is None:
            return [projected for _, projected in records]
        semaphore = asyncio.Semaphore(self._resolve_concurrency)

        async def resolve(
            record: _HistorySampleRecord,
            projected: ClassificationFacts,
        ) -> ClassificationFacts | None:
            async with semaphore:
                try:
                    facts = await facts_resolver(record.payload)
                except Exception:  # noqa: BLE001  单条详情失败不应阻断整批分析
                    return None
            if facts is None or _classification_identity_key(facts) != _classification_identity_key(projected):
                return None
            return facts

        return list(await asyncio.gather(*(resolve(record, projected) for record, projected in records)))


@dataclass(frozen=True, slots=True)
class _HistorySampleRecord:
    """统一下载和整理历史的排序元数据。"""

    kind: Literal["download", "transfer"]
    date: Optional[str]
    record_id: int
    payload: DownloadHistorySnapshot | TransferHistorySnapshot


class ClassificationAnalysisService:
    """协调当前策略快照、纯求值器和有边界样本影响分析。"""

    def __init__(
        self,
        configuration: ClassificationPolicyConfigurationService,
        *,
        sample_provider: ClassificationImpactSampleProvider | None = None,
        execution: ClassificationExecutionPort | None = None,
    ) -> None:
        """保存策略配置、样本提供器和可选的统一执行事实端口。"""
        self._configuration = configuration
        self._sample_provider = sample_provider
        self._execution = execution

    def fields(self) -> ClassificationFieldCatalog:
        """分开返回新规则可选字段和已有规则使用的退役字段。"""
        extra_fields = self._configuration.extra_fields()
        return ClassificationFieldCatalog(
            fields=list(
                build_classification_field_catalog(extra_fields)
            ),
            retired_fields=list(
                build_retired_classification_field_catalog(extra_fields)
            ),
            limits=ClassificationPolicyLimits(
                max_category_depth=MAX_CATEGORY_DEPTH,
                max_category_segment_length=MAX_CATEGORY_SEGMENT_LENGTH,
                max_category_path_length=MAX_CATEGORY_PATH_LENGTH,
                max_condition_depth=MAX_CONDITION_DEPTH,
                max_conditions_per_rule=MAX_CONDITIONS_PER_RULE,
                max_rules=MAX_RULES,
                max_total_conditions=MAX_TOTAL_CONDITIONS,
            ),
        )

    def validate(
        self,
        policy: ClassificationPolicy,
    ) -> ClassificationValidationResult:
        """使用发布时相同字段目录校验一个完整策略草稿。"""
        return self._configuration.validate(policy)

    def preview(self, request: ClassificationPreviewRequest) -> ClassificationEvaluation:
        """对搜索结果或兼容事实执行活动策略或合法草稿，并返回完整命中轨迹。"""
        policy = request.policy or self._configuration.active()
        if request.policy is not None:
            self._require_valid(policy)
        facts = self._preview_facts(
            request.input,
            policy=policy,
            use_execution=self._execution is not None,
        )
        return evaluate_classification_facts(policy, facts, trace=True)

    def _preview_facts(
        self,
        input_data: ClassificationPreviewInput,
        *,
        policy: ClassificationPolicy,
        use_execution: bool,
    ) -> ClassificationFacts:
        """构造预览事实；活动策略优先复用真实执行端口的补充链路。"""
        if input_data.kind == "facts":
            return input_data.facts
        media = _preview_media(input_data.media)
        if use_execution and self._execution is not None:
            facts = self._execution.build_facts(cast(Any, media), policy=policy)
            if facts is not None:
                return facts
        return build_classification_facts_from_media(
            media,
            extra_fields=self._configuration.extra_fields(),
        )

    async def impact(
        self,
        policy: ClassificationPolicy,
        *,
        expected_revision: int,
        sample_limit: int,
        example_limit: int,
        samples: Sequence[ClassificationFacts] = (),
    ) -> ClassificationImpactAnalysis:
        """在最多 200 条有界样本内比较活动策略和合法候选策略。"""
        active = self._configuration.active()
        if expected_revision != active.revision:
            raise ClassificationPolicyConflictError(
                expected_revision=expected_revision,
                current_revision=active.revision,
            )
        candidate = cast(
            ClassificationPolicy,
            policy.model_copy(
                deep=True,
                update={"revision": active.revision + 1},
            ),
        )
        self._require_valid(candidate)
        batch = await self._sample_batch(samples, sample_limit)
        return _build_impact_analysis(
            active, candidate, batch, sample_limit, example_limit
        )

    def _require_valid(self, policy: ClassificationPolicy) -> None:
        """策略存在发布阻断错误时抛出携带结构化结果的稳定异常。"""
        validation = self.validate(policy)
        if not validation.valid:
            raise ClassificationPolicyValidationError(validation)

    async def _sample_batch(
        self,
        samples: Sequence[ClassificationFacts],
        sample_limit: int,
    ) -> ClassificationImpactSampleBatch:
        """优先使用请求事实，否则委托近期历史提供器生成样本。"""
        if samples:
            selected_list: list[ClassificationFacts] = []
            seen: set[tuple[str, str, str, str]] = set()
            skipped_count = 0
            for sample in samples:
                identity = sample.identity
                identity_key = _classification_identity_key(sample)
                if not identity.media_source.strip() or not identity.media_id.strip():
                    skipped_count += 1
                    continue
                if identity_key in seen:
                    skipped_count += 1
                    continue
                if len(selected_list) >= sample_limit:
                    skipped_count += 1
                    continue
                seen.add(identity_key)
                selected_list.append(sample.model_copy(deep=True))
            selected = tuple(selected_list)
            warnings = (
                tuple(
                    item
                    for item in (
                        f"显式事实共 {len(samples)} 条，仅比较前 {sample_limit} 条"
                        if len(samples) > sample_limit
                        else "",
                        "显式事实中存在重复或缺少稳定身份的记录，已跳过"
                        if skipped_count
                        else "",
                    )
                    if item
                )
            )
            return ClassificationImpactSampleBatch(
                source="request",
                facts=selected,
                scanned_count=len(samples),
                skipped_count=skipped_count,
                unresolved_count=0,
                truncated=skipped_count > 0 or len(samples) > sample_limit,
                warnings=warnings,
            )
        if self._sample_provider is None:
            return ClassificationImpactSampleBatch(
                source="recent_history",
                facts=(),
                scanned_count=0,
                skipped_count=0,
                unresolved_count=0,
                truncated=False,
                warnings=("近期历史样本提供器未配置，本次影响分析没有可比较样本",),
            )
        return await self._sample_provider.load(sample_limit)


def _history_facts(
    history: DownloadHistorySnapshot | TransferHistorySnapshot,
) -> ClassificationFacts | None:
    """把历史中稳定保存的身份、类型、标题和年份投影为部分事实。"""
    media_source = _enum_text(history.media_source)
    media_id = str(history.media_id or "").strip()
    media_type = _classification_media_type(history.type)
    if not media_source or not media_id or media_type is None:
        return None
    music = None
    if media_type == "音乐":
        music = ClassificationMusicFacts(
            entity_type=str(history.music_type or "").strip() or None
        )
    return ClassificationFacts(
        identity=ClassificationIdentityFacts(
            media_source=media_source,
            media_id=media_id,
        ),
        media=ClassificationMediaFacts(
            type=media_type,
            title=str(history.title or "").strip() or None,
            year=_history_year(history.year),
        ),
        music=music,
    )


def _classification_identity_key(facts: ClassificationFacts) -> tuple[str, str, str, str]:
    """返回媒体详情可用于核对的来源、编号、类型和音乐实体键。"""
    return (
        facts.identity.media_source,
        facts.identity.media_id,
        facts.media.type,
        facts.music.entity_type if facts.music and facts.music.entity_type else "",
    )


def _preview_media(payload: Mapping[str, Any]) -> object:
    """把预览媒体载荷转换为可供执行服务消费的媒体对象。"""
    media_type = _enum_text(payload.get("type"))
    model = SchemaMusicInfo if media_type == "音乐" else SchemaMediaInfo
    try:
        return model.model_validate(dict(payload))
    except ValueError:
        # 插件来源不一定属于内置 MediaSource 枚举，使用轻量对象保留其完整字段。
        return SimpleNamespace(**dict(payload))


def build_classification_facts_from_media(
    media: object,
    *,
    extra_fields: Sequence[object] = (),
) -> ClassificationFacts:
    """把搜索或识别得到的完整媒体对象转换为统一分类数据。"""
    return build_classification_facts(
        cast(Any, media),
        extensions=_media_extension_facts(media, extra_fields=extra_fields),
    )


def build_classification_facts_from_media_payload(
    payload: Mapping[str, Any],
    *,
    extra_fields: Sequence[object] = (),
) -> ClassificationFacts:
    """把前端选择的媒体搜索结果转换为统一分类数据，并兼容插件来源。"""
    return build_classification_facts_from_media(
        _preview_media(payload),
        extra_fields=extra_fields,
    )


def _media_extension_facts(
    media: object,
    *,
    extra_fields: Sequence[object] = (),
) -> dict[str, dict[str, ClassificationFactValue]]:
    """按 extensions.<source>.<field> 命名空间整理媒体携带的扩展字段。"""
    raw_facts = getattr(media, "classification_facts", None)
    if not isinstance(raw_facts, Mapping):
        return {}
    definitions = field_definition_map(cast(Any, extra_fields))
    extensions: dict[str, dict[str, ClassificationFactValue]] = {}
    for raw_field, value in raw_facts.items():
        field_id = str(raw_field or "").strip()
        if not field_id.startswith("extensions."):
            continue
        if not _is_classification_fact_value(value):
            continue
        definition = definitions.get(field_id)
        if definition is None:
            parts = field_id.split(".", 2)
            if len(parts) != 3 or not parts[1] or not parts[2]:
                continue
            extensions.setdefault(parts[1], {})[parts[2]] = cast(
                ClassificationFactValue, value
            )
            continue
        extension_sources = [
            source
            for source, support in definition.source_support.items()
            if support == "extension"
        ]
        if len(extension_sources) != 1:
            continue
        source = extension_sources[0]
        local_field = field_id.removeprefix(f"extensions.{source}.")
        if source and local_field:
            extensions.setdefault(source, {})[local_field] = cast(ClassificationFactValue, value)
    return extensions


def _is_classification_fact_value(value: object) -> bool:
    """判断扩展字段是否为分类契约允许的 JSON 标量或标量列表。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    return isinstance(value, list) and all(
        item is None or isinstance(item, (str, int, float, bool)) for item in value
    )


def _classification_media_type(value: object) -> ClassificationMediaType | None:
    """兼容历史中使用的中英文媒体类型值。"""
    normalized = _enum_text(value).casefold()
    aliases: dict[str, ClassificationMediaType] = {
        "电影": "电影",
        "movie": "电影",
        "电视剧": "电视剧",
        "tv": "电视剧",
        "电视": "电视剧",
        "音乐": "音乐",
        "music": "音乐",
    }
    return aliases.get(normalized)


def _history_year(value: object) -> int | None:
    """从历史字符串中提取四位年份，异常值按事实缺失处理。"""
    text = _enum_text(value)[:4]
    return int(text) if text.isdigit() else None


def _enum_text(value: object) -> str:
    """把字符串枚举和值统一为去除首尾空白的文本。"""
    if isinstance(value, Enum):
        value = value.value
    return str(value or "").strip()


def _build_impact_analysis(
    active: ClassificationPolicy,
    candidate: ClassificationPolicy,
    batch: ClassificationImpactSampleBatch,
    requested_limit: int,
    example_limit: int,
) -> ClassificationImpactAnalysis:
    """同步执行有界纯求值比较，并生成按稳定分类 ID 聚合的统计。"""
    previous_categories: Counter[str] = Counter()
    candidate_categories: Counter[str] = Counter()
    changes: list[ClassificationImpactChange] = []
    group_counts: dict[tuple[ClassificationMediaType, str], list[int]] = {}
    partial_count = 0
    degraded_count = 0
    category_changed_count = 0
    path_only_changed_count = 0
    rule_changed_only_count = 0
    became_fallback_count = 0
    for facts in batch.facts:
        previous = evaluate_classification_facts(active, facts).result
        proposed = evaluate_classification_facts(candidate, facts).result
        previous_categories[_category_id(previous)] += 1
        candidate_categories[_category_id(proposed)] += 1
        if "partial" in {previous.state, proposed.state}:
            partial_count += 1
        degraded = previous.state == "complete" and proposed.state == "partial"
        if degraded:
            degraded_count += 1
        changed_fields = _changed_result_fields(previous, proposed)
        if "category_id" in changed_fields:
            category_changed_count += 1
        elif "category_path" in changed_fields:
            path_only_changed_count += 1
        elif "rule_id" in changed_fields:
            rule_changed_only_count += 1
        proposed_selection = proposed.effective or proposed.recommended
        if proposed_selection and proposed_selection.source == "fallback":
            previous_selection = previous.effective or previous.recommended
            if not previous_selection or previous_selection.source != "fallback":
                became_fallback_count += 1
        group_key = (facts.media.type, facts.identity.media_source)
        group = group_counts.setdefault(group_key, [0, 0, 0])
        group[0] += 1
        group[1] += bool(changed_fields)
        group[2] += degraded
        if changed_fields:
            changes.append(
                ClassificationImpactChange(
                    identity=facts.identity,
                    media_type=facts.media.type,
                    title=facts.media.title,
                    changed_fields=changed_fields,
                    previous=previous,
                    candidate=proposed,
                )
            )
    warnings = list(batch.warnings)
    if partial_count:
        warnings.append(
            f"{partial_count} 个样本存在事实缺失，影响结果只反映当前可用字段"
        )
    sample_count = len(batch.facts)
    changed_count = len(changes)
    truncated = (
        batch.truncated
        or batch.scanned_count > sample_count + batch.skipped_count
        or changed_count > example_limit
    )
    return ClassificationImpactAnalysis(
        sampled_at=datetime.now(timezone.utc),
        sample_source=batch.source,
        baseline_revision=active.revision,
        candidate_revision=candidate.revision,
        requested_limit=requested_limit,
        scanned_count=batch.scanned_count,
        skipped_count=batch.skipped_count,
        unresolved_count=batch.unresolved_count,
        truncated=truncated,
        sample_count=sample_count,
        changed_count=changed_count,
        unchanged_count=sample_count - changed_count,
        category_changed_count=category_changed_count,
        path_only_changed_count=path_only_changed_count,
        rule_changed_only_count=rule_changed_only_count,
        became_fallback_count=became_fallback_count,
        partial_count=partial_count,
        degraded_count=degraded_count,
        previous_categories=dict(sorted(previous_categories.items())),
        candidate_categories=dict(sorted(candidate_categories.items())),
        groups=[
            ClassificationImpactGroup(
                media_type=media_type,
                media_source=media_source,
                sampled=counts[0],
                changed=counts[1],
                degraded=counts[2],
            )
            for (media_type, media_source), counts in sorted(group_counts.items())
        ],
        changes=changes[:example_limit],
        warnings=warnings,
    )


def _category_id(result: ClassificationResult) -> str:
    """返回结果中的稳定分类 ID；无分类时使用明确聚合键。"""
    selection = result.effective or result.recommended
    return selection.category_id if selection and selection.category_id else _UNCLASSIFIED_CATEGORY_ID


def _changed_result_fields(
    previous: ClassificationResult,
    candidate: ClassificationResult,
) -> list[str]:
    """返回除预期 revision 变化外真正影响分类行为的结果字段。"""
    previous_selection = previous.effective or previous.recommended
    candidate_selection = candidate.effective or candidate.recommended
    fields = {
        "category_id": (
            getattr(previous_selection, "category_id", None),
            getattr(candidate_selection, "category_id", None),
        ),
        "category_path": (
            tuple(getattr(previous_selection, "category_path", ())),
            tuple(getattr(candidate_selection, "category_path", ())),
        ),
        "rule_id": (
            getattr(previous_selection, "rule_id", None),
            getattr(candidate_selection, "rule_id", None),
        ),
        "source": (
            getattr(previous_selection, "source", None),
            getattr(candidate_selection, "source", None),
        ),
        "labels": (tuple(previous.labels), tuple(candidate.labels)),
        "state": (previous.state, candidate.state),
    }
    return [field for field, values in fields.items() if values[0] != values[1]]
