"""完整媒体识别结果的分类执行与兼容回退服务。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from enum import Enum
from typing import Protocol, TypeAlias

from app.application.classification.legacy import (
    build_legacy_tmdb_extension_facts,
    resolve_legacy_tmdb_category,
)
from app.domain.classification.evaluator import ClassificationEvaluator
from app.domain.classification.facts import build_classification_facts
from app.domain.context import MediaInfo, MusicAlbumInfo, MusicArtistInfo, MusicInfo
from app.schemas.category import (
    CategoryConfig,
    ClassificationFacts,
    ClassificationFactValue,
    ClassificationPolicy,
    ClassificationResult,
    ClassificationSelection,
)

ClassificationSubject: TypeAlias = (
    MediaInfo | MusicInfo | MusicAlbumInfo | MusicArtistInfo
)
"""可以在完整识别出口执行自动分类的标准媒体对象。"""

ClassificationExtensionFactsProvider: TypeAlias = Callable[
    [ClassificationSubject],
    Mapping[str, Mapping[str, ClassificationFactValue]],
]
"""按当前插件注册表校验并提供来源扩展分类事实的端口。"""


class ClassificationRuntimePort(Protocol):
    """分类执行只需要的活动策略与 legacy 快照端口。"""

    def active_policy(self) -> ClassificationPolicy | None:
        """返回活动策略；配置不可用时返回空值。"""
        ...

    def legacy_config(self) -> CategoryConfig:
        """返回隔离的 legacy 配置快照。"""
        ...


class ClassificationExecutionPort(Protocol):
    """Chain、订阅和整理应用层共享的纯分类执行端口。"""

    async def async_build_facts(
        self,
        media: ClassificationSubject,
    ) -> ClassificationFacts | None:
        """异步构造与实际分类一致的完整事实快照，不写入媒体或策略。"""
        ...

    def finalize(
        self,
        media: ClassificationSubject,
        *,
        extensions: Mapping[
            str,
            Mapping[str, ClassificationFactValue],
        ]
        | None = None,
        effective_override: ClassificationSelection | None = None,
        refresh: bool = False,
    ) -> ClassificationSubject:
        """分类一个完整媒体对象，并按需强制刷新已存在的同 revision 结果。"""
        ...

    async def async_finalize(
        self,
        media: ClassificationSubject,
        *,
        extensions: Mapping[
            str,
            Mapping[str, ClassificationFactValue],
        ]
        | None = None,
        effective_override: ClassificationSelection | None = None,
        refresh: bool = False,
    ) -> ClassificationSubject:
        """异步分类完整媒体对象，并允许有界补充缺失标准事实。"""
        ...


class ClassificationEnrichmentPort(Protocol):
    """分类执行服务消费的同步与异步缺失事实补充端口。"""

    def enrich(
        self,
        policy: ClassificationPolicy,
        facts: ClassificationFacts,
        media: object,
    ) -> ClassificationFacts:
        """同步补充缺失标准事实。"""
        ...

    async def async_enrich(
        self,
        policy: ClassificationPolicy,
        facts: ClassificationFacts,
        media: object,
    ) -> ClassificationFacts:
        """异步补充缺失标准事实。"""
        ...


class ClassificationExecutionService:
    """按当前不可变策略分类完整媒体对象，并隔离来源缓存对象。"""

    def __init__(
        self,
        runtime: ClassificationRuntimePort,
        *,
        extension_facts_provider: ClassificationExtensionFactsProvider | None = None,
        enrichment: ClassificationEnrichmentPort | None = None,
    ) -> None:
        """保存策略运行时、扩展事实校验端口和可选跨来源补充服务。"""
        self._runtime = runtime
        self._extension_facts_provider = extension_facts_provider
        self._enrichment = enrichment

    async def async_build_facts(
        self,
        media: ClassificationSubject,
    ) -> ClassificationFacts | None:
        """构造影响分析使用的完整事实，并复用插件扩展与跨来源补充规则。"""
        finalized, policy, facts, _ = self._prepare(
            media,
            extensions=None,
            effective_override=None,
            refresh=False,
        )
        if policy is None or facts is None:
            return None
        if self._enrichment is not None:
            try:
                facts = await self._enrichment.async_enrich(policy, facts, finalized)
            except Exception:  # noqa: BLE001  详情补充失败时保留主来源事实
                pass
        return facts

    def finalize(
        self,
        media: ClassificationSubject,
        *,
        extensions: Mapping[
            str,
            Mapping[str, ClassificationFactValue],
        ]
        | None = None,
        effective_override: ClassificationSelection | None = None,
        refresh: bool = False,
    ) -> ClassificationSubject:
        """复制并分类完整识别结果，来源缓存中的旧结果永远不作为真值。"""
        finalized, policy, facts, effective_override = self._prepare(
            media,
            extensions=extensions,
            effective_override=effective_override,
            refresh=refresh,
        )
        if policy is None or facts is None:
            return finalized
        if self._enrichment is not None:
            try:
                facts = self._enrichment.enrich(policy, facts, finalized)
            except Exception:  # noqa: BLE001  分类补充不得成为识别硬依赖
                pass
        return self._apply_evaluation(
            finalized,
            policy,
            facts,
            effective_override=effective_override,
        )

    async def async_finalize(
        self,
        media: ClassificationSubject,
        *,
        extensions: Mapping[
            str,
            Mapping[str, ClassificationFactValue],
        ]
        | None = None,
        effective_override: ClassificationSelection | None = None,
        refresh: bool = False,
    ) -> ClassificationSubject:
        """异步复制并分类完整结果，补充失败时继续使用原始事实。"""
        finalized, policy, facts, effective_override = self._prepare(
            media,
            extensions=extensions,
            effective_override=effective_override,
            refresh=refresh,
        )
        if policy is None or facts is None:
            return finalized
        if self._enrichment is not None:
            try:
                facts = await self._enrichment.async_enrich(
                    policy,
                    facts,
                    finalized,
                )
            except Exception:  # noqa: BLE001  分类补充不得成为识别硬依赖
                pass
        return self._apply_evaluation(
            finalized,
            policy,
            facts,
            effective_override=effective_override,
        )

    def _prepare(
        self,
        media: ClassificationSubject,
        *,
        extensions: Mapping[str, Mapping[str, ClassificationFactValue]] | None,
        effective_override: ClassificationSelection | None,
        refresh: bool,
    ) -> tuple[
        ClassificationSubject,
        ClassificationPolicy | None,
        ClassificationFacts | None,
        ClassificationSelection | None,
    ]:
        """复制媒体、读取一次策略并构造同步异步共用的初始事实。"""
        del refresh
        effective_override = effective_override or _explicit_effective_override(media)
        finalized = deepcopy(media)
        policy = self._runtime.active_policy()
        if policy is None:
            self._apply_invalid_policy_fallback(
                finalized,
                effective_override=effective_override,
            )
            return finalized, None, None, effective_override
        registered_extensions = self._registered_extensions(finalized)
        merged_extensions = _classification_extensions(
            policy,
            finalized,
            _merge_supplied_extensions(registered_extensions, extensions),
        )
        try:
            facts = build_classification_facts(
                finalized,
                extensions=merged_extensions,
            )
        except ValueError:
            finalized.set_library_category("")
            finalized.classification = ClassificationResult(
                effective=(
                    effective_override.model_copy(deep=True)
                    if effective_override
                    else None
                ),
                policy_revision=policy.revision,
                state="not_evaluated",
            )
            if effective_override:
                finalized.set_library_category(
                    _category_path_snapshot(effective_override)
                )
            return finalized, policy, None, effective_override
        return finalized, policy, facts, effective_override

    @staticmethod
    def _apply_evaluation(
        finalized: ClassificationSubject,
        policy: ClassificationPolicy,
        facts: ClassificationFacts,
        *,
        effective_override: ClassificationSelection | None,
    ) -> ClassificationSubject:
        """应用纯求值结果和人工覆盖，并更新兼容目录分类。"""
        evaluation = ClassificationEvaluator.evaluate(policy, facts)
        result = evaluation.result.model_copy(deep=True)
        if effective_override:
            result.effective = effective_override.model_copy(deep=True)
        finalized.classification = result
        selection = result.effective or result.recommended
        finalized.set_library_category(
            _category_path_snapshot(selection) if selection else ""
        )
        return finalized

    def _registered_extensions(
        self,
        media: ClassificationSubject,
    ) -> Mapping[str, Mapping[str, ClassificationFactValue]]:
        """隔离可选插件事实端口故障，媒体识别结果仍按缺失字段继续分类。"""
        if self._extension_facts_provider is None:
            return {}
        try:
            return self._extension_facts_provider(media)
        except Exception:  # noqa: BLE001  扩展事实不得成为识别硬依赖
            return {}

    def finalize_many(
        self,
        media_items: Mapping[str, ClassificationSubject],
    ) -> dict[str, ClassificationSubject]:
        """逐项分类路径映射，保持键顺序并隔离每个来源结果。"""
        return {
            key: self.finalize(media)
            for key, media in media_items.items()
        }

    def _apply_invalid_policy_fallback(
        self,
        media: ClassificationSubject,
        *,
        effective_override: ClassificationSelection | None,
    ) -> None:
        """策略不可用时仅恢复 legacy TMDB 目录分类并标记失效状态。"""
        media.set_library_category("")
        legacy_category = resolve_legacy_tmdb_category(
            self._runtime.legacy_config(),
            media_type=_enum_text(getattr(media, "type", None)),
            media_source=_enum_text(getattr(media, "media_source", None)),
            tmdb_info=getattr(media, "tmdb_info", None),
        )
        selection = (
            ClassificationSelection(
                category_path=[legacy_category],
                source="legacy",
            )
            if legacy_category
            else None
        )
        media.classification = ClassificationResult(
            recommended=selection,
            effective=(
                effective_override.model_copy(deep=True)
                if effective_override
                else selection
            ),
            state="invalid_policy",
        )
        effective = effective_override or selection
        if effective:
            media.set_library_category(_category_path_snapshot(effective))


def _classification_extensions(
    policy: ClassificationPolicy,
    media: ClassificationSubject,
    supplied: Mapping[str, Mapping[str, ClassificationFactValue]] | None,
) -> dict[str, dict[str, ClassificationFactValue]]:
    """合并受控调用方扩展事实和策略实际需要的 legacy TMDB 事实。"""
    merged = {
        str(source): {str(key): value for key, value in values.items()}
        for source, values in (supplied or {}).items()
    }
    tmdb_info = getattr(media, "tmdb_info", None)
    if isinstance(tmdb_info, Mapping):
        for source, values in build_legacy_tmdb_extension_facts(
            policy,
            tmdb_info,
        ).items():
            merged.setdefault(source, {}).update(values)
    return merged


def _merge_supplied_extensions(
    registered: Mapping[str, Mapping[str, ClassificationFactValue]],
    supplied: Mapping[str, Mapping[str, ClassificationFactValue]] | None,
) -> dict[str, dict[str, ClassificationFactValue]]:
    """合并已校验插件事实和受信调用方事实，显式调用方值具有最终优先级。"""
    merged = {
        str(source): {str(key): value for key, value in values.items()}
        for source, values in registered.items()
    }
    for source, values in (supplied or {}).items():
        merged.setdefault(str(source), {}).update(
            {str(key): value for key, value in values.items()}
        )
    return merged


def _explicit_effective_override(
    media: ClassificationSubject,
) -> ClassificationSelection | None:
    """保留已装配的人工覆盖，避免补充元数据时被自动推荐覆盖。"""
    classification = getattr(media, "classification", None)
    if not isinstance(classification, ClassificationResult):
        return None
    effective = classification.effective
    if effective is None or effective.source not in {"manual", "subscription"}:
        return None
    return deepcopy(effective)


def _enum_text(value: object) -> str:
    """把字符串枚举或普通值转换为去空白文本。"""
    if isinstance(value, Enum):
        value = value.value
    return str(value or "").strip()


def _category_path_snapshot(selection: ClassificationSelection) -> str:
    """把已校验路径投影为过渡期 library_category 兼容字符串。"""
    return "/".join(selection.category_path)
