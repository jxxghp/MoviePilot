"""已安装物理插件的来源身份合同与存量迁移决策。"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Protocol

_PLUGIN_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,127}$")
_ONLINE_SOURCE_KEY_PATTERN = re.compile(
    r"^github:[a-z0-9](?:[a-z0-9-]{0,38})/"
    r"[a-z0-9._-]{1,100}$"
)
OFFICIAL_PLUGIN_SOURCE_KEY = "github:jxxghp/moviepilot-plugins"


class TrustedPluginSourceType(StrEnum):
    """可被在线自动更新信任的来源类型。"""

    UNKNOWN = "unknown"
    OFFICIAL = "official"
    THIRD_PARTY = "third_party"


class PluginPayloadSourceType(StrEnum):
    """最近一次已提交插件载荷的来源类型。"""

    UNKNOWN = "unknown"
    OFFICIAL = "official"
    THIRD_PARTY = "third_party"
    LOCAL = "local"


class PluginBindingBasis(StrEnum):
    """物理插件来源身份的建立依据。"""

    LEGACY_UNBOUND = "legacy_unbound"
    LOCAL_ONLY = "local_only"
    OFFICIAL_DEFAULT = "official_default"
    TOFU = "tofu"
    EXPLICIT_INSTALL = "explicit_install"
    EXPLICIT_SOURCE_CHANGE = "explicit_source_change"


class PluginMarketAvailability(StrEnum):
    """存量迁移观察市场候选时的可用状态。"""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class PluginIdentityConflictError(RuntimeError):
    """来源身份的首次创建或 revision 条件更新已失去竞争。"""


def normalize_physical_plugin_id(plugin_id: str) -> str:
    """校验物理插件 ID，并返回大小写无关的数据库身份键。"""
    if plugin_id != plugin_id.strip() or not _PLUGIN_ID_PATTERN.fullmatch(plugin_id):
        raise ValueError("插件 ID 必须以字母开头且只能包含 ASCII 字母或数字")
    return plugin_id.lower()


def validate_online_source_key(source_key: str) -> str:
    """校验由市场边界生成的稳定 GitHub 仓库身份键。"""
    value = source_key.strip().lower()
    if not _ONLINE_SOURCE_KEY_PATTERN.fullmatch(value):
        raise ValueError("在线插件来源必须使用 github:<owner>/<repository> 规范键")
    return value


def _validate_online_source_classification(
    source_key: str,
    source_type: TrustedPluginSourceType | PluginPayloadSourceType,
) -> None:
    """保证官方仓库键与官方来源类型始终双向一致。"""
    is_official_key = source_key == OFFICIAL_PLUGIN_SOURCE_KEY
    is_official_type = source_type.value == TrustedPluginSourceType.OFFICIAL.value
    if is_official_key != is_official_type:
        raise ValueError("官方来源类型只能对应 MoviePilot 官方插件仓库")


def _validate_optional_text(
    value: str | None,
    *,
    field_name: str,
    max_length: int,
) -> None:
    """让应用层字符串合同与跨数据库列长度保持一致。"""
    if value is not None and len(value) > max_length:
        raise ValueError(f"{field_name} 长度不能超过 {max_length}")


def _validate_receipt(receipt: str | None) -> str | None:
    """校验内容收据为带算法前缀的十六进制摘要。"""
    if receipt is None:
        return None
    value = receipt.strip().lower()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError("插件载荷收据必须为 sha256:<64 hex>")
    return value


@dataclass(frozen=True, slots=True)
class PluginIdentity:
    """一份物理插件的可信更新来源与当前载荷审计事实。"""

    plugin_id: str
    normalized_plugin_id: str
    trusted_source_type: TrustedPluginSourceType
    trusted_source_key: str | None
    binding_basis: PluginBindingBasis
    payload_source_type: PluginPayloadSourceType
    payload_source_key: str | None
    declared_version: str | None
    package_generation: str | None
    system_version: str | None
    supports_v3: bool | None
    supports_v3t: bool | None
    payload_receipt: str | None
    revision: int
    created_at: datetime
    updated_at: datetime
    bound_at: datetime | None
    payload_applied_at: datetime | None

    def __post_init__(self) -> None:
        """拒绝不能作为后续来源门禁事实的矛盾状态。"""
        normalized_id = normalize_physical_plugin_id(self.plugin_id)
        if self.normalized_plugin_id != normalized_id:
            raise ValueError("normalized_plugin_id 必须是 plugin_id 的规范化物理身份")
        if self.revision < 1:
            raise ValueError("来源身份 revision 必须从 1 开始")
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("来源身份审计时间必须包含时区")
        if self.updated_at < self.created_at:
            raise ValueError("来源身份更新时间不能早于创建时间")
        for audit_time in (self.bound_at, self.payload_applied_at):
            if audit_time is not None and audit_time.tzinfo is None:
                raise ValueError("来源身份审计时间必须包含时区")
            if audit_time is not None and not self.created_at <= audit_time <= self.updated_at:
                raise ValueError("来源身份审计时间必须位于创建与更新时间之间")

        trusted_key = (
            validate_online_source_key(self.trusted_source_key)
            if self.trusted_source_key
            else None
        )
        if self.trusted_source_type is TrustedPluginSourceType.UNKNOWN:
            if trusted_key is not None or self.bound_at is not None:
                raise ValueError("未绑定身份不能携带可信来源或绑定时间")
            if self.binding_basis not in {
                PluginBindingBasis.LEGACY_UNBOUND,
                PluginBindingBasis.LOCAL_ONLY,
            }:
                raise ValueError("未绑定身份只能使用存量迁移或本地插件依据")
        else:
            if trusted_key is None or self.bound_at is None:
                raise ValueError("已绑定身份必须携带规范来源和绑定时间")
            if self.binding_basis in {
                PluginBindingBasis.LEGACY_UNBOUND,
                PluginBindingBasis.LOCAL_ONLY,
            }:
                raise ValueError("已绑定身份不能使用未绑定来源依据")
            _validate_online_source_classification(
                trusted_key,
                self.trusted_source_type,
            )
        if (
            self.binding_basis is PluginBindingBasis.OFFICIAL_DEFAULT
            and self.trusted_source_type is not TrustedPluginSourceType.OFFICIAL
        ):
            raise ValueError("official_default 只能绑定官方来源")
        if (
            self.binding_basis is PluginBindingBasis.TOFU
            and self.trusted_source_type is not TrustedPluginSourceType.THIRD_PARTY
        ):
            raise ValueError("TOFU 只能绑定唯一第三方在线来源")

        payload_key = (
            validate_online_source_key(self.payload_source_key)
            if self.payload_source_key
            else None
        )
        if self.payload_source_type in {
            PluginPayloadSourceType.OFFICIAL,
            PluginPayloadSourceType.THIRD_PARTY,
        }:
            if payload_key is None:
                raise ValueError("在线载荷必须携带规范来源")
            _validate_online_source_classification(
                payload_key,
                self.payload_source_type,
            )
        elif payload_key is not None:
            raise ValueError("未知或本地载荷不能携带在线来源键")
        if self.payload_source_type is PluginPayloadSourceType.UNKNOWN:
            if any((
                self.declared_version,
                self.package_generation,
                self.system_version,
                self.supports_v3 is not None,
                self.supports_v3t is not None,
                self.payload_receipt,
                self.payload_applied_at,
            )):
                raise ValueError("未知载荷不能携带版本、兼容声明、收据或应用时间")
        else:
            if not self.declared_version or not self.package_generation:
                raise ValueError("已知载荷必须携带声明版本和包代际")
            if self.payload_applied_at is None or self.payload_receipt is None:
                raise ValueError("已知载荷必须携带应用时间和内容收据")
            if (
                self.payload_source_type is not PluginPayloadSourceType.LOCAL
                and (
                    payload_key != trusted_key
                    or self.payload_source_type.value
                    != self.trusted_source_type.value
                )
            ):
                raise ValueError("在线载荷来源必须与可信更新来源一致")
        if (
            self.binding_basis is PluginBindingBasis.LOCAL_ONLY
            and self.payload_source_type is not PluginPayloadSourceType.LOCAL
        ):
            raise ValueError("本地插件身份必须携带已提交的本地载荷事实")

        if self.package_generation not in {None, "v1", "v2", "v3"}:
            raise ValueError("插件包代际必须为 v1、v2 或 v3")
        _validate_optional_text(
            self.declared_version,
            field_name="插件声明版本",
            max_length=64,
        )
        _validate_optional_text(
            self.system_version,
            field_name="插件系统版本要求",
            max_length=128,
        )
        object.__setattr__(self, "trusted_source_key", trusted_key)
        object.__setattr__(self, "payload_source_key", payload_key)
        object.__setattr__(self, "payload_receipt", _validate_receipt(self.payload_receipt))


@dataclass(frozen=True, slots=True)
class PluginSourceCandidate:
    """存量迁移可观察到的一个在线候选来源。"""

    source_type: TrustedPluginSourceType
    source_key: str

    def __post_init__(self) -> None:
        """候选只接受可绑定的规范在线来源。"""
        if self.source_type is TrustedPluginSourceType.UNKNOWN:
            raise ValueError("未知来源不能作为在线候选")
        object.__setattr__(self, "source_key", validate_online_source_key(self.source_key))
        _validate_online_source_classification(self.source_key, self.source_type)


def plan_legacy_plugin_identity(
    *,
    plugin_id: str,
    market_availability: PluginMarketAvailability,
    online_candidates: tuple[PluginSourceCandidate, ...],
    is_virtual_instance: bool,
    now: datetime,
) -> PluginIdentity | None:
    """为存量物理插件建立更新绑定，不冒充当前载荷来源。"""
    if is_virtual_instance:
        return None
    normalized_id = normalize_physical_plugin_id(plugin_id)
    candidates = {
        (candidate.source_type, candidate.source_key): candidate
        for candidate in online_candidates
    }
    official = sorted(
        (
            candidate
            for candidate in candidates.values()
            if candidate.source_type is TrustedPluginSourceType.OFFICIAL
        ),
        key=lambda candidate: candidate.source_key,
    )
    third_party = sorted(
        (
            candidate
            for candidate in candidates.values()
            if candidate.source_type is TrustedPluginSourceType.THIRD_PARTY
        ),
        key=lambda candidate: candidate.source_key,
    )

    trusted_type = TrustedPluginSourceType.UNKNOWN
    trusted_key = None
    basis = PluginBindingBasis.LEGACY_UNBOUND
    bound_at = None
    if market_availability is PluginMarketAvailability.AVAILABLE and official:
        trusted_type = TrustedPluginSourceType.OFFICIAL
        trusted_key = official[0].source_key
        basis = PluginBindingBasis.OFFICIAL_DEFAULT
        bound_at = now
    elif (
        market_availability is PluginMarketAvailability.AVAILABLE
        and len(third_party) == 1
    ):
        trusted_type = TrustedPluginSourceType.THIRD_PARTY
        trusted_key = third_party[0].source_key
        basis = PluginBindingBasis.TOFU
        bound_at = now

    return PluginIdentity(
        plugin_id=plugin_id,
        normalized_plugin_id=normalized_id,
        trusted_source_type=trusted_type,
        trusted_source_key=trusted_key,
        binding_basis=basis,
        payload_source_type=PluginPayloadSourceType.UNKNOWN,
        payload_source_key=None,
        declared_version=None,
        package_generation=None,
        system_version=None,
        supports_v3=None,
        supports_v3t=None,
        payload_receipt=None,
        revision=1,
        created_at=now,
        updated_at=now,
        bound_at=bound_at,
        payload_applied_at=None,
    )


class PluginIdentityRepository(Protocol):
    """来源身份条件写命令使用的无提交仓储端口。"""

    def get(self, plugin_id: str) -> PluginIdentity | None:
        """按规范化物理插件 ID 读取身份。"""

    def stage_create(self, identity: PluginIdentity) -> None:
        """暂存首次身份；唯一键竞争时抛出冲突错误。"""

    def stage_replace(
        self,
        identity: PluginIdentity,
        *,
        expected_revision: int,
    ) -> bool:
        """按 revision 条件暂存替换，并返回是否赢得竞争。"""


class PluginIdentityUnitOfWork(Protocol):
    """来源身份条件写使用的事务端口。"""

    def commit(self) -> None:
        """提交当前条件写。"""

    def rollback(self) -> None:
        """回滚当前条件写。"""


class WritePluginIdentityCommand:
    """以数据库 revision 原子创建或替换一份插件来源身份。"""

    def __init__(
        self,
        repository: PluginIdentityRepository,
        unit_of_work: PluginIdentityUnitOfWork,
    ) -> None:
        """保存仓储与事务所有者。"""
        self._repository = repository
        self._unit_of_work = unit_of_work

    def execute(
        self,
        identity: PluginIdentity,
        *,
        expected_revision: int | None,
    ) -> PluginIdentity:
        """首次写要求记录不存在，后续写要求 revision 精确匹配。"""
        candidate = replace(
            identity,
            normalized_plugin_id=normalize_physical_plugin_id(identity.plugin_id),
            revision=1 if expected_revision is None else expected_revision + 1,
        )
        try:
            if expected_revision is None:
                if (
                    candidate.binding_basis
                    is PluginBindingBasis.EXPLICIT_SOURCE_CHANGE
                ):
                    raise PluginIdentityConflictError(
                        "首次插件身份不能伪装成已确认的来源变更"
                    )
                self._repository.stage_create(candidate)
            else:
                current = self._repository.get(candidate.normalized_plugin_id)
                if current is None or current.revision != expected_revision:
                    raise PluginIdentityConflictError(
                        f"插件 {candidate.plugin_id} 的来源身份已被其他任务更新"
                    )
                immutable_binding = (
                    "plugin_id",
                    "normalized_plugin_id",
                    "trusted_source_type",
                    "trusted_source_key",
                    "binding_basis",
                    "created_at",
                    "bound_at",
                )
                if any(
                    getattr(candidate, field_name) != getattr(current, field_name)
                    for field_name in immutable_binding
                ):
                    raise PluginIdentityConflictError(
                        "普通插件身份更新不能改变物理 ID 或可信来源绑定"
                    )
                if candidate.updated_at < current.updated_at:
                    raise PluginIdentityConflictError(
                        "插件身份更新时间不能早于已提交记录"
                    )
                if not self._repository.stage_replace(
                    candidate,
                    expected_revision=expected_revision,
                ):
                    raise PluginIdentityConflictError(
                        f"插件 {candidate.plugin_id} 的来源身份已被其他任务更新"
                    )
            self._unit_of_work.commit()
            return candidate
        except Exception:
            self._unit_of_work.rollback()
            raise


class ChangePluginIdentitySourceCommand:
    """以独立 CAS 合同提交一次明确的在线插件来源转换。"""

    def __init__(
        self,
        repository: PluginIdentityRepository,
        unit_of_work: PluginIdentityUnitOfWork,
    ) -> None:
        """保存仓储与事务所有者。"""
        self._repository = repository
        self._unit_of_work = unit_of_work

    def execute(
        self,
        identity: PluginIdentity,
        *,
        expected_revision: int,
    ) -> PluginIdentity:
        """只允许已有身份按精确 revision 切换到不同在线来源。"""
        return _execute_identity_transition(
            self._repository,
            self._unit_of_work,
            identity,
            expected_revision=expected_revision,
            validate=_validate_identity_source_change,
        )


class BindOnlinePluginIdentityCommand:
    """以独立 CAS 合同为未绑定身份建立在线可信来源。"""

    def __init__(
        self,
        repository: PluginIdentityRepository,
        unit_of_work: PluginIdentityUnitOfWork,
    ) -> None:
        """保存仓储与事务所有者。"""
        self._repository = repository
        self._unit_of_work = unit_of_work

    def execute(
        self,
        identity: PluginIdentity,
        *,
        expected_revision: int,
    ) -> PluginIdentity:
        """只允许未绑定身份按精确 revision 首次绑定在线来源。"""
        return _execute_identity_transition(
            self._repository,
            self._unit_of_work,
            identity,
            expected_revision=expected_revision,
            validate=_validate_online_binding,
        )


class BindLocalPluginIdentityCommand:
    """以独立 CAS 合同把存量未绑定身份转换为本地专属身份。"""

    def __init__(
        self,
        repository: PluginIdentityRepository,
        unit_of_work: PluginIdentityUnitOfWork,
    ) -> None:
        """保存仓储与事务所有者。"""
        self._repository = repository
        self._unit_of_work = unit_of_work

    def execute(
        self,
        identity: PluginIdentity,
        *,
        expected_revision: int,
    ) -> PluginIdentity:
        """只允许 legacy_unbound 身份按精确 revision 绑定本地载荷。"""
        return _execute_identity_transition(
            self._repository,
            self._unit_of_work,
            identity,
            expected_revision=expected_revision,
            validate=_validate_local_binding,
        )


def _execute_identity_transition(
    repository: PluginIdentityRepository,
    unit_of_work: PluginIdentityUnitOfWork,
    identity: PluginIdentity,
    *,
    expected_revision: int,
    validate: Callable[[PluginIdentity, PluginIdentity], None],
) -> PluginIdentity:
    """在一个数据库事务中校验并提交专用身份转换。"""
    try:
        current = _prepare_identity_transition(
            repository,
            identity,
            expected_revision=expected_revision,
        )
        validate(current, identity)
        candidate = replace(
            identity,
            normalized_plugin_id=current.normalized_plugin_id,
            revision=current.revision + 1,
            created_at=current.created_at,
        )
        _stage_identity_transition(
            repository,
            unit_of_work,
            candidate,
            expected_revision=expected_revision,
        )
        return candidate
    except Exception:
        unit_of_work.rollback()
        raise


def _prepare_identity_transition(
    repository: PluginIdentityRepository,
    identity: PluginIdentity,
    *,
    expected_revision: int,
) -> PluginIdentity:
    """读取转换基线并保证目标使用同一物理插件和精确 revision。"""
    if expected_revision < 1:
        raise PluginIdentityConflictError("插件来源身份 expected_revision 必须从 1 开始")
    normalized_plugin_id = normalize_physical_plugin_id(identity.plugin_id)
    current = repository.get(normalized_plugin_id)
    if current is None or current.revision != expected_revision:
        raise PluginIdentityConflictError(
            f"插件 {identity.plugin_id} 的来源身份 revision 已被其他任务更新"
        )
    if (
        identity.plugin_id != current.plugin_id
        or identity.normalized_plugin_id != current.normalized_plugin_id
    ):
        raise PluginIdentityConflictError(
            "插件来源转换不能改变物理插件 ID"
        )
    if identity.created_at != current.created_at:
        raise PluginIdentityConflictError(
            "插件来源转换必须保留身份创建时间"
        )
    if identity.updated_at < current.updated_at:
        raise PluginIdentityConflictError(
            "插件身份更新时间不能早于已提交记录"
        )
    return current


def _validate_identity_source_change(
    current: PluginIdentity,
    candidate: PluginIdentity,
) -> None:
    """校验显式换源的来源、载荷和实际变化边界。"""
    if candidate.binding_basis is not PluginBindingBasis.EXPLICIT_SOURCE_CHANGE:
        raise PluginIdentityConflictError(
            "显式换源目标必须使用 explicit_source_change 依据"
        )
    if candidate.trusted_source_type is TrustedPluginSourceType.UNKNOWN:
        raise PluginIdentityConflictError("显式换源目标必须是在线可信来源")
    if candidate.payload_source_type not in {
        PluginPayloadSourceType.OFFICIAL,
        PluginPayloadSourceType.THIRD_PARTY,
    }:
        raise PluginIdentityConflictError("显式换源目标必须携带在线载荷")
    if (
        candidate.trusted_source_type.value != candidate.payload_source_type.value
        or candidate.trusted_source_key != candidate.payload_source_key
    ):
        raise PluginIdentityConflictError(
            "显式换源目标的 trusted 与 payload 来源必须一致"
        )
    if (
        current.trusted_source_type is candidate.trusted_source_type
        and current.trusted_source_key == candidate.trusted_source_key
    ):
        raise PluginIdentityConflictError("显式换源的实际来源必须变化")


def _validate_online_binding(
    current: PluginIdentity,
    candidate: PluginIdentity,
) -> None:
    """校验未绑定身份首次建立在线可信来源的转换边界。"""
    if (
        current.trusted_source_type is not TrustedPluginSourceType.UNKNOWN
        or current.binding_basis not in {
            PluginBindingBasis.LEGACY_UNBOUND,
            PluginBindingBasis.LOCAL_ONLY,
        }
    ):
        raise PluginIdentityConflictError(
            "在线绑定只允许当前未绑定的存量或本地身份"
        )
    if candidate.binding_basis not in {
        PluginBindingBasis.OFFICIAL_DEFAULT,
        PluginBindingBasis.TOFU,
        PluginBindingBasis.EXPLICIT_INSTALL,
    }:
        raise PluginIdentityConflictError(
            "在线绑定目标必须说明官方、TOFU 或显式安装依据"
        )
    if candidate.trusted_source_type is TrustedPluginSourceType.UNKNOWN:
        raise PluginIdentityConflictError("在线绑定目标必须携带可信来源")
    if candidate.payload_source_type not in {
        PluginPayloadSourceType.OFFICIAL,
        PluginPayloadSourceType.THIRD_PARTY,
    }:
        raise PluginIdentityConflictError("在线绑定目标必须携带在线载荷")
    if (
        candidate.trusted_source_type.value != candidate.payload_source_type.value
        or candidate.trusted_source_key != candidate.payload_source_key
    ):
        raise PluginIdentityConflictError(
            "在线绑定目标的 trusted 与 payload 来源必须一致"
        )


def _validate_local_binding(
    current: PluginIdentity,
    candidate: PluginIdentity,
) -> None:
    """校验存量未绑定身份到本地身份的唯一转换方向。"""
    if (
        current.trusted_source_type is not TrustedPluginSourceType.UNKNOWN
        or current.binding_basis is not PluginBindingBasis.LEGACY_UNBOUND
    ):
        raise PluginIdentityConflictError(
            "本地绑定只允许当前 unknown + legacy_unbound 身份"
        )
    if (
        candidate.trusted_source_type is not TrustedPluginSourceType.UNKNOWN
        or candidate.binding_basis is not PluginBindingBasis.LOCAL_ONLY
        or candidate.payload_source_type is not PluginPayloadSourceType.LOCAL
    ):
        raise PluginIdentityConflictError(
            "本地绑定目标必须是 unknown + local_only 且携带本地载荷"
        )


def _stage_identity_transition(
    repository: PluginIdentityRepository,
    unit_of_work: PluginIdentityUnitOfWork,
    candidate: PluginIdentity,
    *,
    expected_revision: int,
) -> None:
    """按数据库 revision 条件暂存转换并提交事务。"""
    if not repository.stage_replace(
        candidate,
        expected_revision=expected_revision,
    ):
        raise PluginIdentityConflictError(
            f"插件 {candidate.plugin_id} 的来源身份 revision 已被其他任务更新"
        )
    unit_of_work.commit()
