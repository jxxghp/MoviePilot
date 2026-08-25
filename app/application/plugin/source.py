"""插件市场候选事实与来源选择策略。"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypeAlias

from app.application.plugin.identity import (
    PluginIdentity,
    PluginPayloadSourceType,
    TrustedPluginSourceType,
    normalize_physical_plugin_id,
    validate_online_source_key,
)
from app.application.plugin.identity import (
    PluginSourceCandidate as IdentitySourceCandidate,
)
from app.foundation.version import compare_version

PLUGIN_GENERATIONS = ("v1", "v2", "v3")


class MarketReadStatus(StrEnum):
    """一次市场索引读取的最终状态。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"

class PluginSelectionStatus(StrEnum):
    """插件候选选择的可观察结果。"""

    SELECTED = "selected"
    UNAVAILABLE = "unavailable"
    CONFLICT = "conflict"
    INCOMPLETE = "incomplete"


class PluginSourceSelectionError(RuntimeError):
    """插件来源选择的策略错误。"""


@dataclass(frozen=True, slots=True)
class PluginMarketCandidate:
    """一个在线市场条目的原始候选事实。"""

    plugin_id: str
    source_key: str
    source_type: TrustedPluginSourceType
    repo_url: str
    package_generation: str
    plugin_version: str | None
    dto: Any = None
    normalized_plugin_id: str = field(init=False)

    def __post_init__(self) -> None:
        """校验候选身份，并把外部来源键归一为持久化合同使用的形式。"""
        normalized_id = normalize_physical_plugin_id(self.plugin_id)
        source_type = _coerce_online_source_type(self.source_type)
        source_key = validate_online_source_key(self.source_key)
        # IdentitySourceCandidate 复用官方仓库与来源类型的双向约束。
        IdentitySourceCandidate(source_type=source_type, source_key=source_key)
        repo_url = _normalize_repo_url(self.repo_url)
        package_generation = normalize_package_generation(self.package_generation)
        plugin_version = _normalize_plugin_version(self.plugin_version)
        object.__setattr__(self, "normalized_plugin_id", normalized_id)
        object.__setattr__(self, "source_type", source_type)
        object.__setattr__(self, "source_key", source_key)
        object.__setattr__(self, "repo_url", repo_url)
        object.__setattr__(self, "package_generation", package_generation)
        object.__setattr__(self, "plugin_version", plugin_version)

    @property
    def payload_source_type(self) -> PluginPayloadSourceType:
        """返回用于载荷审计的在线来源类型。"""
        return PluginPayloadSourceType(self.source_type.value)

    def public_dict(self) -> dict[str, Any]:
        """生成不携带原始元数据的公共候选投影。"""
        return {
            "plugin_id": self.plugin_id,
            "source_key": self.source_key,
            "source_type": self.source_type.value,
            "repo_url": self.repo_url,
            "package_generation": self.package_generation,
            "plugin_version": self.plugin_version,
        }

@dataclass(frozen=True, slots=True)
class PluginLocalCandidate:
    """一个本地插件载荷候选，与在线来源身份保持独立。"""

    plugin_id: str
    repo_url: str
    package_generation: str
    plugin_version: str | None
    dto: Any = None
    normalized_plugin_id: str = field(init=False)

    def __post_init__(self) -> None:
        """校验本地载荷的插件与版本事实。"""
        normalized_id = normalize_physical_plugin_id(self.plugin_id)
        repo_url = _normalize_repo_url(self.repo_url)
        package_generation = normalize_package_generation(self.package_generation)
        plugin_version = _normalize_plugin_version(self.plugin_version)
        object.__setattr__(self, "normalized_plugin_id", normalized_id)
        object.__setattr__(self, "repo_url", repo_url)
        object.__setattr__(self, "package_generation", package_generation)
        object.__setattr__(self, "plugin_version", plugin_version)

    @property
    def payload_source_type(self) -> PluginPayloadSourceType:
        """返回本地载荷类型，不把本地路径伪装成在线来源。"""
        return PluginPayloadSourceType.LOCAL

    @property
    def source_type(self) -> PluginPayloadSourceType:
        """返回独立的本地载荷类型，不伪造在线可信来源。"""
        return PluginPayloadSourceType.LOCAL

    @property
    def source_key(self) -> None:
        """本地载荷没有可绑定的在线来源键。"""
        return None

    def public_dict(self) -> dict[str, Any]:
        """生成本地候选的公共投影，永不暴露仓库路径或原始 metadata。"""
        return {
            "plugin_id": self.plugin_id,
            "source_type": PluginPayloadSourceType.LOCAL.value,
            "package_generation": self.package_generation,
            "plugin_version": self.plugin_version,
        }

@dataclass(frozen=True, slots=True)
class MarketRead:
    """记录一个配置市场的读取状态及其全部在线候选。"""

    market: str
    status: MarketReadStatus
    candidates: tuple[PluginMarketCandidate, ...] = ()
    error: str | None = None
    package_generation: str = "v1"

    def __post_init__(self) -> None:
        """拒绝把失败读取伪装成空成功结果。"""
        market = _normalize_market(self.market)
        status = MarketReadStatus(self.status)
        candidates = tuple(self.candidates)
        package_generation = normalize_package_generation(self.package_generation)
        if status is MarketReadStatus.FAILED:
            if candidates:
                raise ValueError("失败的插件市场读取不能携带候选")
            if not self.error or not self.error.strip():
                raise ValueError("失败的插件市场读取必须保留错误说明")
        elif self.error:
            raise ValueError("成功的插件市场读取不能携带错误说明")
        if any(not isinstance(candidate, PluginMarketCandidate) for candidate in candidates):
            raise TypeError("市场读取候选必须是在线插件候选")
        object.__setattr__(self, "market", market)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "error", self.error.strip() if self.error else None)
        object.__setattr__(self, "package_generation", package_generation)

    @classmethod
    def success(
        cls,
        market: str,
        candidates: Iterable[PluginMarketCandidate] = (),
        *,
        package_generation: str = "v1",
    ) -> "MarketRead":
        """构造成功读取，包括成功但没有条目的市场。"""
        return cls(
            market=market,
            status=MarketReadStatus.SUCCEEDED,
            candidates=tuple(candidates),
            package_generation=package_generation,
        )

    @classmethod
    def failure(
        cls,
        market: str,
        error: str,
        *,
        package_generation: str = "v1",
    ) -> "MarketRead":
        """构造失败读取，并保留可诊断但不用于选择的错误说明。"""
        return cls(
            market=market,
            status=MarketReadStatus.FAILED,
            error=error,
            package_generation=package_generation,
        )

    @property
    def succeeded(self) -> bool:
        """判断该市场是否已确定读取成功。"""
        return self.status is MarketReadStatus.SUCCEEDED

    def public_dict(self) -> dict[str, Any]:
        """生成市场读取的脱敏投影，保留状态、代际和候选事实。"""
        return {
            "market": self.market,
            "package_generation": self.package_generation,
            "status": self.status.value,
            "error": self.error,
            "candidates": [candidate.public_dict() for candidate in self.candidates],
        }

@dataclass(frozen=True, slots=True)
class CandidateInventory:
    """一次短生命周期市场快照，保留配置市场状态和全部候选。"""

    market_reads: tuple[MarketRead, ...]
    local_candidates: tuple[PluginLocalCandidate, ...] = ()
    expected_markets: tuple[str, ...] | None = None
    expected_generations: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        """冻结快照输入，避免后续市场刷新改变选择依据。"""
        market_reads = tuple(self.market_reads)
        local_candidates = tuple(self.local_candidates)
        expected_markets = (
            tuple(_normalize_market(market) for market in self.expected_markets)
            if self.expected_markets is not None
            else None
        )
        expected_generations = (
            tuple(normalize_package_generation(generation) for generation in self.expected_generations)
            if self.expected_generations is not None
            else None
        )
        if any(not isinstance(read, MarketRead) for read in market_reads):
            raise TypeError("候选清单必须由 MarketRead 组成")
        if any(not isinstance(candidate, PluginLocalCandidate) for candidate in local_candidates):
            raise TypeError("本地候选清单必须由 PluginLocalCandidate 组成")
        read_keys = [(read.market, read.package_generation) for read in market_reads]
        if len(read_keys) != len(set(read_keys)):
            raise ValueError("候选清单不能重复记录同一个市场代际")
        if expected_markets is not None and len(expected_markets) != len(set(expected_markets)):
            raise ValueError("候选清单的预期市场不能重复")
        if expected_generations is not None:
            if not expected_generations or len(expected_generations) != len(set(expected_generations)):
                raise ValueError("候选清单的预期代际必须唯一且非空")
        object.__setattr__(self, "market_reads", market_reads)
        object.__setattr__(self, "local_candidates", local_candidates)
        object.__setattr__(self, "expected_markets", expected_markets)
        object.__setattr__(self, "expected_generations", expected_generations)

    @property
    def configured_markets(self) -> tuple[str, ...]:
        """按配置顺序返回本轮预期读取的市场。"""
        if self.expected_markets is not None:
            return self.expected_markets
        return tuple(dict.fromkeys(read.market for read in self.market_reads))

    def reads_for(self, market: str) -> tuple[MarketRead, ...]:
        """返回一个市场的全部代际读取事实。"""
        normalized_market = _normalize_market(market)
        return tuple(read for read in self.market_reads if read.market == normalized_market)

    def read_for(self, market: str, package_generation: str) -> MarketRead | None:
        """返回一个市场和代际的读取事实。"""
        normalized_generation = normalize_package_generation(package_generation)
        return next(
            (
                read
                for read in self.reads_for(market)
                if read.package_generation == normalized_generation
            ),
            None,
        )

    @property
    def complete(self) -> bool:
        """只有至少一个市场且全部读取成功时才算完整。"""
        if not self.market_reads or not all(read.succeeded for read in self.market_reads):
            return False
        if self.expected_markets is None or self.expected_generations is None:
            return True
        expected = {
            (market, generation)
            for market in self.expected_markets
            for generation in self.expected_generations
        }
        actual = {(read.market, read.package_generation) for read in self.market_reads}
        return expected <= actual

    @property
    def can_use_for_tofu(self) -> bool:
        """判断快照是否足以证明唯一第三方来源。"""
        return self.complete

    @property
    def online_candidates(self) -> tuple[PluginMarketCandidate, ...]:
        """按配置市场顺序返回全部在线候选，不按 ID 或版本去重。"""
        return tuple(
            candidate
            for read in self.market_reads
            if read.succeeded
            for candidate in read.candidates
        )

    def candidates_for(self, plugin_id: str) -> tuple[PluginMarketCandidate, ...]:
        """读取一个插件 ID 的全部在线候选。"""
        normalized_id = normalize_physical_plugin_id(plugin_id)
        return tuple(
            candidate
            for candidate in self.online_candidates
            if candidate.normalized_plugin_id == normalized_id
        )

    def local_candidates_for(self, plugin_id: str) -> tuple[PluginLocalCandidate, ...]:
        """读取一个插件 ID 的全部本地候选。"""
        normalized_id = normalize_physical_plugin_id(plugin_id)
        return tuple(
            candidate
            for candidate in self.local_candidates
            if candidate.normalized_plugin_id == normalized_id
        )

    def public_dict(self) -> dict[str, Any]:
        """生成完整库存的脱敏投影，不泄漏本地路径或原始 DTO。"""
        return {
            "markets": [read.public_dict() for read in self.market_reads],
            "local_candidates": [candidate.public_dict() for candidate in self.local_candidates],
            "complete": self.complete,
        }

@dataclass(frozen=True, slots=True)
class PluginSelection:
    """候选选择结果，冲突和不完整状态均不降级为静默空值。"""

    status: PluginSelectionStatus
    candidate: PluginMarketCandidate | PluginLocalCandidate | None = None
    conflict_source_keys: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        """保证选择状态与载荷及冲突信息相互一致。"""
        status = PluginSelectionStatus(self.status)
        conflict_source_keys = tuple(sorted(set(self.conflict_source_keys)))
        if status is PluginSelectionStatus.SELECTED and self.candidate is None:
            raise ValueError("selected 结果必须携带候选")
        if status is not PluginSelectionStatus.SELECTED and self.candidate is not None:
            raise ValueError("未选中结果不能携带候选")
        if status is not PluginSelectionStatus.CONFLICT and conflict_source_keys:
            raise ValueError("只有 conflict 结果能携带冲突来源")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "conflict_source_keys", conflict_source_keys)

    @property
    def selected(self) -> bool:
        """判断是否已经选择出一个载荷候选。"""
        return self.status is PluginSelectionStatus.SELECTED

    def public_dict(self) -> dict[str, Any]:
        """生成安全的选择结果投影，不透传本地路径或原始 DTO。"""
        result: dict[str, Any] = {
            "status": self.status.value,
            "reason": self.reason,
        }
        if self.conflict_source_keys:
            result["conflict_source_keys"] = list(self.conflict_source_keys)
        if self.candidate is not None:
            result["candidate"] = self.candidate.public_dict()
        return result

Candidate: TypeAlias = PluginMarketCandidate | PluginLocalCandidate


def normalize_package_generation(package_generation: str) -> str:
    """校验并归一插件包代际。"""
    value = str(package_generation).strip().lower()
    if value not in PLUGIN_GENERATIONS:
        raise ValueError("插件包代际必须为 v1、v2 或 v3")
    return value


def select_plugin_candidate(
    inventory: CandidateInventory,
    *,
    plugin_id: str,
    generations: Sequence[str],
    identity: PluginIdentity | None = None,
    local_candidates: Iterable[PluginLocalCandidate] | None = None,
) -> PluginSelection:
    """
    按允许来源、运行代际和同源版本选择一个插件载荷。

    :param inventory: 本轮市场读取快照
    :param plugin_id: 要选择的物理插件 ID
    :param generations: 调用方按优先级传入的代际顺序
    :param identity: 已安装插件来源身份；为空表示未安装
    :param local_candidates: 可选的本地载荷候选，优先于在线候选
    :return: 带明确冲突或不完整状态的选择结果
    """
    normalized_id = normalize_physical_plugin_id(plugin_id)
    generation_order = _normalize_generation_order(generations)
    local = tuple(local_candidates) if local_candidates is not None else inventory.local_candidates_for(plugin_id)
    if any(not isinstance(candidate, PluginLocalCandidate) for candidate in local):
        raise TypeError("本地候选必须是 PluginLocalCandidate")
    if any(candidate.normalized_plugin_id != normalized_id for candidate in local):
        raise ValueError("本地候选的插件 ID 必须与选择目标一致")
    if local:
        selected_local = _select_best(local, generation_order)
        if selected_local is None:
            return PluginSelection(
                status=PluginSelectionStatus.UNAVAILABLE,
                reason="本地候选没有符合当前运行代际的版本",
            )
        return PluginSelection(
            status=PluginSelectionStatus.SELECTED,
            candidate=selected_local,
            reason="优先使用本地载荷",
        )

    online = inventory.candidates_for(plugin_id)
    if not online:
        return PluginSelection(
            status=PluginSelectionStatus.UNAVAILABLE,
            reason=f"没有找到插件 {plugin_id} 的在线候选",
        )

    allowed_source = _allowed_source(identity, normalized_id)
    if allowed_source is not None:
        source_type, source_key = allowed_source
        online = tuple(
            candidate
            for candidate in online
            if candidate.source_type is source_type and candidate.source_key == source_key
        )
        if not online:
            return PluginSelection(
                status=PluginSelectionStatus.UNAVAILABLE,
                reason="当前来源身份没有可用候选",
            )
        selected_online = _select_best(online, generation_order)
        if selected_online is None:
            return PluginSelection(
                status=PluginSelectionStatus.UNAVAILABLE,
                reason="在线候选没有符合当前运行代际的版本",
            )
        return PluginSelection(
            status=PluginSelectionStatus.SELECTED,
            candidate=selected_online,
            reason="按已绑定来源选择在线载荷",
        )

    if identity is not None:
        return PluginSelection(
            status=PluginSelectionStatus.INCOMPLETE,
            reason="插件来源身份尚未绑定，不能自动选择在线载荷",
        )

    source_pairs = {(candidate.source_type, candidate.source_key) for candidate in online}
    if len(source_pairs) > 1:
        return PluginSelection(
            status=PluginSelectionStatus.CONFLICT,
            conflict_source_keys=tuple(source_key for _source_type, source_key in source_pairs),
            reason="未安装插件存在多个在线来源，不能静默选择",
        )

    source_type = next(iter(source_pairs))[0]
    if source_type is TrustedPluginSourceType.THIRD_PARTY and not inventory.can_use_for_tofu:
        return PluginSelection(
            status=PluginSelectionStatus.INCOMPLETE,
            reason="市场读取不完整，不能建立唯一第三方来源的 TOFU",
        )
    selected_online = _select_best(online, generation_order)
    if selected_online is None:
        return PluginSelection(
            status=PluginSelectionStatus.UNAVAILABLE,
            reason="在线候选没有符合当前运行代际的版本",
        )
    return PluginSelection(
        status=PluginSelectionStatus.SELECTED,
        candidate=selected_online,
        reason="唯一在线来源候选",
    )


def _coerce_online_source_type(source_type: TrustedPluginSourceType) -> TrustedPluginSourceType:
    """把外部字符串来源类型转换为可信在线来源枚举。"""
    value = TrustedPluginSourceType(source_type)
    if value is TrustedPluginSourceType.UNKNOWN:
        raise ValueError("未知来源不能作为在线市场候选")
    return value


def _normalize_market(market: str) -> str:
    """校验市场标识，保留其作为本轮快照的显示值。"""
    value = str(market).strip()
    if not value:
        raise ValueError("插件市场标识不能为空")
    return value


def _normalize_repo_url(repo_url: str) -> str:
    """保留仓库地址作为安装事实，但移除无意义的外围空白。"""
    value = str(repo_url).strip()
    if not value:
        raise ValueError("插件仓库地址不能为空")
    return value


def _normalize_plugin_version(plugin_version: str | None) -> str | None:
    """标准化可选插件声明版本，并保持缺失版本可观察。"""
    if plugin_version is None:
        return None
    value = str(plugin_version).strip()
    if not value:
        raise ValueError("插件声明版本不能为空字符串")
    if len(value) > 64:
        raise ValueError("插件声明版本长度不能超过 64")
    return value


def _normalize_generation_order(generations: Sequence[str]) -> tuple[str, ...]:
    """校验调用方提供的代际优先序，并拒绝重复项。"""
    normalized = tuple(normalize_package_generation(generation) for generation in generations)
    if not normalized:
        raise ValueError("至少需要一个当前运行代际")
    if len(normalized) != len(set(normalized)):
        raise ValueError("当前运行代际优先序不能重复")
    return normalized


def _allowed_source(
    identity: PluginIdentity | None,
    normalized_plugin_id: str,
) -> tuple[TrustedPluginSourceType, str] | None:
    """从已安装身份读取不可变的允许在线来源。"""
    if identity is None:
        return None
    if identity.normalized_plugin_id != normalized_plugin_id:
        raise ValueError("来源身份的插件 ID 与选择目标不一致")
    if identity.trusted_source_type is TrustedPluginSourceType.UNKNOWN:
        return None
    if not identity.trusted_source_key:
        raise PluginSourceSelectionError("已绑定来源身份缺少规范来源键")
    return identity.trusted_source_type, identity.trusted_source_key


def _select_best(
    candidates: Sequence[Candidate],
    generation_order: Sequence[str],
) -> Candidate | None:
    """在已完成来源过滤后按代际和同源版本选择最高候选。"""
    for generation in generation_order:
        generation_candidates = tuple(
            candidate
            for candidate in candidates
            if candidate.package_generation == generation
        )
        if generation_candidates:
            return _select_highest_version(generation_candidates)
    return None


def _select_highest_version(candidates: Sequence[Candidate]) -> Candidate:
    """使用宿主既有版本比较语义选择同源最高版本，平级保留先读候选。"""
    selected = candidates[0]
    for candidate in candidates[1:]:
        selected_version = selected.plugin_version or "0"
        candidate_version = candidate.plugin_version or "0"
        if compare_version(candidate_version, ">", selected_version):
            selected = candidate
    return selected
