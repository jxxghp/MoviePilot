"""插件市场候选事实与来源选择策略。"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypeAlias
from urllib.parse import unquote, urlsplit

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

    PRESENT = "present"
    ABSENT = "absent"
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
        else:
            if self.error:
                raise ValueError("已判定的插件市场读取不能携带错误说明")
            if status is MarketReadStatus.ABSENT and candidates:
                raise ValueError("不存在的插件市场索引不能携带候选")
        if any(not isinstance(candidate, PluginMarketCandidate) for candidate in candidates):
            raise TypeError("市场读取候选必须是在线插件候选")
        object.__setattr__(self, "market", market)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "error", self.error.strip() if self.error else None)
        object.__setattr__(self, "package_generation", package_generation)

    @classmethod
    def present(
        cls,
        market: str,
        candidates: Iterable[PluginMarketCandidate] = (),
        *,
        package_generation: str = "v1",
    ) -> "MarketRead":
        """构造存在的索引读取；真实空索引可以没有候选。"""
        return cls(
            market=market,
            status=MarketReadStatus.PRESENT,
            candidates=tuple(candidates),
            package_generation=package_generation,
        )

    @classmethod
    def absent(
        cls,
        market: str,
        *,
        package_generation: str = "v1",
    ) -> "MarketRead":
        """构造已确认不存在的代际索引。"""
        return cls(
            market=market,
            status=MarketReadStatus.ABSENT,
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
        """判断该索引是否得到存在或不存在的确定结论。"""
        return self.status is not MarketReadStatus.FAILED

    @property
    def present_index(self) -> bool:
        """判断该代际索引是否真实存在。"""
        return self.status is MarketReadStatus.PRESENT

    def public_dict(self) -> dict[str, Any]:
        """生成市场读取的脱敏投影，保留状态、代际和候选事实。"""
        return {
            "market": self.market,
            "package_generation": self.package_generation,
            "status": self.status.value,
            "error": self.error,
            "candidates": [candidate.public_dict() for candidate in self.candidates],
        }


class LocalCandidateReadStatus(StrEnum):
    """一次本地插件仓库扫描的可观察终态。"""

    PRESENT = "present"
    ABSENT = "absent"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class LocalCandidateRead:
    """记录本地候选扫描状态，避免扫描失败伪装成空仓库。"""

    status: LocalCandidateReadStatus
    candidates: tuple[PluginLocalCandidate, ...] = ()
    error: str | None = None

    def __post_init__(self) -> None:
        """保证本地扫描状态、候选和错误说明相互一致。"""
        status = LocalCandidateReadStatus(self.status)
        candidates = tuple(self.candidates)
        if any(not isinstance(candidate, PluginLocalCandidate) for candidate in candidates):
            raise TypeError("本地扫描候选必须是 PluginLocalCandidate")
        if status is LocalCandidateReadStatus.FAILED:
            if candidates:
                raise ValueError("失败的本地扫描不能携带候选")
            if not self.error or not self.error.strip():
                raise ValueError("失败的本地扫描必须保留错误说明")
        else:
            if self.error:
                raise ValueError("已判定的本地扫描不能携带错误说明")
            if status is LocalCandidateReadStatus.ABSENT and candidates:
                raise ValueError("不存在的本地扫描不能携带候选")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "error", self.error.strip() if self.error else None)

    @classmethod
    def present(
        cls,
        candidates: Iterable[PluginLocalCandidate] = (),
    ) -> "LocalCandidateRead":
        """构造扫描成功的本地候选快照，空结果仍表示扫描成功。"""
        return cls(
            status=LocalCandidateReadStatus.PRESENT,
            candidates=tuple(candidates),
        )

    @classmethod
    def absent(cls) -> "LocalCandidateRead":
        """构造没有配置本地仓库的结果。"""
        return cls(status=LocalCandidateReadStatus.ABSENT)

    @classmethod
    def failure(cls, error: str) -> "LocalCandidateRead":
        """构造无法完成本地扫描的结果。"""
        return cls(status=LocalCandidateReadStatus.FAILED, error=error)

    def public_dict(self) -> dict[str, Any]:
        """生成不泄漏本地路径的扫描投影。"""
        return {
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
    local_read: LocalCandidateRead | None = None

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
        local_read = self.local_read
        if local_read is None:
            local_read = (
                LocalCandidateRead.present(local_candidates)
                if local_candidates
                else LocalCandidateRead.absent()
            )
        if not isinstance(local_read, LocalCandidateRead):
            raise TypeError("候选清单的本地读取必须由 LocalCandidateRead 组成")
        if local_read.candidates != local_candidates:
            raise ValueError("候选清单的本地读取与本地候选必须一致")
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
        object.__setattr__(self, "local_read", local_read)

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
        """只有预期市场与代际均可证明已成功读取时才算完整。"""
        if not self.market_reads or not all(read.succeeded for read in self.market_reads):
            return False
        expected_markets = self.expected_markets
        expected_generations = self.expected_generations
        if (expected_markets is None) != (expected_generations is None):
            return False
        if expected_markets is None or expected_generations is None:
            return True
        expected = {
            (market, generation)
            for market in expected_markets
            for generation in expected_generations
        }
        actual = {(read.market, read.package_generation) for read in self.market_reads}
        return expected <= actual

    @property
    def can_use_for_tofu(self) -> bool:
        """判断快照是否足以证明唯一第三方来源。"""
        local_read = self.local_read
        return (
            self.complete
            and local_read is not None
            and local_read.status is not LocalCandidateReadStatus.FAILED
        )

    @property
    def online_candidates(self) -> tuple[PluginMarketCandidate, ...]:
        """按配置市场顺序返回全部在线候选，不按 ID 或版本去重。"""
        return tuple(
            candidate
            for read in self.market_reads
            if read.present_index
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
        local_read = self.local_read
        if local_read is None:
            raise RuntimeError("候选清单缺少本地读取终态")
        return {
            "markets": [read.public_dict() for read in self.market_reads],
            "local_candidates": [candidate.public_dict() for candidate in self.local_candidates],
            "local_read": local_read.public_dict(),
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


def _select_local_candidate(
    inventory: CandidateInventory,
    *,
    plugin_id: str,
    normalized_id: str,
    generation_order: tuple[str, ...],
    local_candidates: Iterable[PluginLocalCandidate] | None,
) -> PluginSelection | None:
    """优先选择本地载荷；读取失败时阻止自动降级到在线来源。"""
    local = (
        tuple(local_candidates)
        if local_candidates is not None
        else inventory.local_candidates_for(plugin_id)
    )
    if any(not isinstance(candidate, PluginLocalCandidate) for candidate in local):
        raise TypeError("本地候选必须是 PluginLocalCandidate")
    if any(candidate.normalized_plugin_id != normalized_id for candidate in local):
        raise ValueError("本地候选的插件 ID 必须与选择目标一致")
    local_read = inventory.local_read
    if (
        not local
        and local_read is not None
        and local_read.status is LocalCandidateReadStatus.FAILED
    ):
        return PluginSelection(
            status=PluginSelectionStatus.INCOMPLETE,
            reason="部分插件仓库暂时无法读取，请稍后重试",
        )
    if not local:
        return None
    selected_local = _select_best(local, generation_order)
    if selected_local is None:
        return PluginSelection(
            status=PluginSelectionStatus.UNAVAILABLE,
            reason="本地插件不支持当前 MoviePilot 版本",
        )
    return PluginSelection(
        status=PluginSelectionStatus.SELECTED,
        candidate=selected_local,
        reason="当前使用本地插件",
    )


def select_plugin_candidate(
    inventory: CandidateInventory,
    *,
    plugin_id: str,
    generations: Sequence[str],
    identity: PluginIdentity | None = None,
    local_candidates: Iterable[PluginLocalCandidate] | None = None,
    requested_source_key: str | None = None,
    explicit_source: bool = False,
    allow_source_change: bool = False,
) -> PluginSelection:
    """
    按在线绑定、本地候选、运行代际和版本选择一个插件载荷。

    :param inventory: 本轮市场读取快照
    :param plugin_id: 要选择的物理插件 ID
    :param generations: 调用方按优先级传入的代际顺序
    :param identity: 已安装插件来源身份；为空表示未安装
    :param local_candidates: 可选的本地载荷候选；已有在线绑定时参与代际和版本比较
    :param requested_source_key: 调用方提供的规范在线来源；非显式调用不能绕过本地载荷
    :param explicit_source: 本次调用是否代表管理员明确选源
    :param allow_source_change: 是否是带 revision 的显式换源命令
    :return: 带明确冲突或不完整状态的选择结果
    """
    normalized_id = normalize_physical_plugin_id(plugin_id)
    generation_order = _normalize_generation_order(generations)
    requested_source = (
        validate_online_source_key(requested_source_key)
        if requested_source_key is not None
        else None
    )
    local_selection: PluginSelection | None = None
    if requested_source is None or not (explicit_source or allow_source_change):
        local_selection = _select_local_candidate(
            inventory,
            plugin_id=plugin_id,
            normalized_id=normalized_id,
            generation_order=generation_order,
            local_candidates=local_candidates,
        )
        if (
            local_selection is not None
            and local_selection.status is PluginSelectionStatus.INCOMPLETE
        ):
            return local_selection

    online = inventory.candidates_for(plugin_id)
    if not online:
        if local_selection is not None:
            return local_selection
        return PluginSelection(
            status=PluginSelectionStatus.UNAVAILABLE,
            reason=f"没有找到插件 {plugin_id} 的可用安装包",
        )

    allowed_source = _allowed_source(identity, normalized_id)
    if requested_source is not None:
        requested_online = tuple(
            candidate
            for candidate in online
            if candidate.source_key == requested_source
        )
        if not requested_online:
            return PluginSelection(
                status=PluginSelectionStatus.UNAVAILABLE,
                reason="所选仓库中没有该插件的可用安装包",
            )
        if allowed_source is not None:
            _source_type, allowed_key = allowed_source
            if requested_source != allowed_key and not allow_source_change:
                return PluginSelection(
                    status=PluginSelectionStatus.CONFLICT,
                    conflict_source_keys=(allowed_key, requested_source),
                    reason="该插件已绑定其他仓库，请先确认更换",
                )
        if explicit_source or allow_source_change:
            selected_requested = _select_best(requested_online, generation_order)
            if selected_requested is None:
                return PluginSelection(
                    status=PluginSelectionStatus.UNAVAILABLE,
                    reason="所选仓库没有适用于当前 MoviePilot 版本的插件包",
                )
            return PluginSelection(
                status=PluginSelectionStatus.SELECTED,
                candidate=selected_requested,
                reason=(
                    "已选择目标仓库中的插件包"
                    if allow_source_change
                    else "已选择指定仓库中的插件包"
                ),
            )
    if allowed_source is not None:
        return _select_bound_source_candidate(
            online=online,
            allowed_source=allowed_source,
            local_selection=local_selection,
            identity=identity,
            generation_order=generation_order,
        )

    if identity is not None:
        if local_selection is not None:
            return local_selection
        return PluginSelection(
            status=PluginSelectionStatus.INCOMPLETE,
            reason="当前插件尚未绑定仓库",
        )

    if local_selection is not None:
        return local_selection

    source_pairs = {(candidate.source_type, candidate.source_key) for candidate in online}
    if len(source_pairs) > 1:
        return PluginSelection(
            status=PluginSelectionStatus.CONFLICT,
            conflict_source_keys=tuple(source_key for _source_type, source_key in source_pairs),
            reason="该插件存在于多个仓库，请选择仓库",
        )

    source_type = next(iter(source_pairs))[0]
    if source_type is TrustedPluginSourceType.THIRD_PARTY and not inventory.can_use_for_tofu:
        return PluginSelection(
            status=PluginSelectionStatus.INCOMPLETE,
            reason="部分插件仓库暂时无法读取，无法安全确认仓库",
        )
    selected_online = _select_best(online, generation_order)
    if selected_online is None:
        return PluginSelection(
            status=PluginSelectionStatus.UNAVAILABLE,
            reason="可用仓库中没有适用于当前 MoviePilot 版本的插件包",
        )
    return PluginSelection(
        status=PluginSelectionStatus.SELECTED,
        candidate=selected_online,
        reason="已找到唯一可用仓库",
    )


def _select_bound_source_candidate(
    *,
    online: tuple[PluginMarketCandidate, ...],
    allowed_source: tuple[TrustedPluginSourceType, str],
    local_selection: PluginSelection | None,
    identity: PluginIdentity | None,
    generation_order: tuple[str, ...],
) -> PluginSelection:
    """只在已绑定仓库范围内选取在线候选，并与可用本地载荷协调。"""
    if identity is None:
        raise PluginSourceSelectionError("已绑定来源选择缺少插件身份")
    source_type, source_key = allowed_source
    allowed_online = tuple(
        candidate
        for candidate in online
        if candidate.source_type is source_type and candidate.source_key == source_key
    )
    if not allowed_online:
        if (
            local_selection is not None
            and local_selection.status is PluginSelectionStatus.SELECTED
        ):
            return local_selection
        return PluginSelection(
            status=PluginSelectionStatus.UNAVAILABLE,
            reason="已绑定仓库中暂无可用插件包",
        )
    selected_online = _select_best(allowed_online, generation_order)
    if selected_online is None:
        if (
            local_selection is not None
            and local_selection.status is PluginSelectionStatus.SELECTED
        ):
            return local_selection
        return PluginSelection(
            status=PluginSelectionStatus.UNAVAILABLE,
            reason="已绑定仓库没有适用于当前 MoviePilot 版本的插件包",
        )
    return _select_bound_or_local_candidate(
        local_selection=local_selection,
        online_candidate=selected_online,
        identity=identity,
        generation_order=generation_order,
    )


def _select_bound_or_local_candidate(
    *,
    local_selection: PluginSelection | None,
    online_candidate: Candidate,
    identity: PluginIdentity,
    generation_order: tuple[str, ...],
) -> PluginSelection:
    """在已绑定在线候选和本地候选之间选择代际、版本更高的载荷。"""
    if (
        local_selection is None
        or local_selection.status is not PluginSelectionStatus.SELECTED
        or local_selection.candidate is None
    ):
        return PluginSelection(
            status=PluginSelectionStatus.SELECTED,
            candidate=online_candidate,
            reason="已使用绑定仓库中的插件包",
        )

    local_candidate = local_selection.candidate
    local_generation = generation_order.index(local_candidate.package_generation)
    online_generation = generation_order.index(online_candidate.package_generation)
    if local_generation < online_generation:
        return local_selection
    if online_generation < local_generation:
        return PluginSelection(
            status=PluginSelectionStatus.SELECTED,
            candidate=online_candidate,
            reason="绑定仓库提供了更高代际的插件包",
        )

    local_version = local_candidate.plugin_version or "0"
    online_version = online_candidate.plugin_version or "0"
    if compare_version(local_version, ">", online_version):
        return local_selection
    if compare_version(online_version, ">", local_version):
        return PluginSelection(
            status=PluginSelectionStatus.SELECTED,
            candidate=online_candidate,
            reason="绑定仓库提供了更高版本的插件包",
        )
    if identity.payload_source_type is PluginPayloadSourceType.LOCAL:
        return local_selection
    return PluginSelection(
        status=PluginSelectionStatus.SELECTED,
        candidate=online_candidate,
        reason="当前继续使用绑定仓库中的插件包",
    )


def list_effective_online_candidates(
    inventory: CandidateInventory,
    *,
    plugin_id: str,
    generations: Sequence[str],
) -> tuple[PluginMarketCandidate, ...]:
    """按来源列出当前运行代际实际可安装的最高版本候选，官方来源始终置顶。"""
    generation_order = _normalize_generation_order(generations)
    grouped: dict[
        tuple[TrustedPluginSourceType, str],
        list[PluginMarketCandidate],
    ] = {}
    for candidate in inventory.candidates_for(plugin_id):
        grouped.setdefault(
            (candidate.source_type, candidate.source_key),
            [],
        ).append(candidate)

    selected: list[PluginMarketCandidate] = []
    for candidates in grouped.values():
        selected_candidate = _select_best(candidates, generation_order)
        if isinstance(selected_candidate, PluginMarketCandidate):
            selected.append(selected_candidate)
    selected.sort(
        key=lambda candidate: candidate.source_type
        is not TrustedPluginSourceType.OFFICIAL
    )
    return tuple(selected)


def get_effective_local_candidate(
    inventory: CandidateInventory,
    *,
    plugin_id: str,
    generations: Sequence[str],
) -> PluginLocalCandidate | None:
    """返回本地插件目录中当前运行代际优先级最高的安全候选。"""
    candidate = _select_best(
        inventory.local_candidates_for(plugin_id),
        _normalize_generation_order(generations),
    )
    return candidate if isinstance(candidate, PluginLocalCandidate) else None


def parse_local_plugin_reference(repo_url: str) -> str | None:
    """从不透明本地来源标识中提取插件 ID，不读取或暴露宿主路径。"""
    if not str(repo_url).startswith("local://"):
        return None
    try:
        parsed = urlsplit(repo_url)
        plugin_id = unquote(parsed.netloc or parsed.path.strip("/"))
    except (TypeError, ValueError):
        return None
    return plugin_id or None


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
