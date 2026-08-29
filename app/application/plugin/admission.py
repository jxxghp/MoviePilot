"""插件载荷来源准入与目标身份规划。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from app.application.plugin.declaration import PluginDeclaredMetadata
from app.application.plugin.identity import (
    PluginBindingBasis,
    PluginIdentity,
    TrustedPluginSourceType,
)
from app.application.plugin.inventory import normalize_github_plugin_source
from app.application.plugin.source import (
    Candidate,
    CandidateInventory,
    PluginLocalCandidate,
    PluginSelectionStatus,
    select_plugin_candidate,
)
from app.domain.plugin import parse_local_plugin_reference


class PluginSourceAdmissionError(RuntimeError):
    """插件来源冲突、库存不完整或换源授权无效。"""


@dataclass(frozen=True, slots=True)
class PluginInstallAdmissionRequest:
    """一次安装调用中会影响来源选择的显式业务参数。"""

    plugin_id: str
    generations: Sequence[str]
    requested_repo_url: str | None = None
    explicit_source: bool = False
    source_change: bool = False
    expected_revision: int | None = None


@dataclass(frozen=True, slots=True)
class PluginInstallAdmission:
    """下载前冻结的候选与身份转换决策。"""

    candidate: Candidate
    identity_before: PluginIdentity | None
    binding_basis: PluginBindingBasis
    trusted_source_type: TrustedPluginSourceType
    trusted_source_key: str | None
    bound_at: datetime | None

    @property
    def expected_revision(self) -> int | None:
        """返回最终数据库提交必须匹配的身份 revision。"""
        return self.identity_before.revision if self.identity_before else None

    def build_identity(
        self,
        *,
        payload_receipt: str,
        applied_at: datetime,
        declared_version: str | None = None,
        manifest_matches_payload: bool = True,
    ) -> PluginIdentity:
        """在载荷落盘并生成收据后构造唯一数据库提交目标。"""
        current = self.identity_before
        plugin_id = current.plugin_id if current else self.candidate.plugin_id
        metadata = self.candidate.dto if isinstance(self.candidate.dto, Mapping) else {}
        installed_version = declared_version or self.candidate.plugin_version
        source_binding_changed = (
            self.trusted_source_type is not TrustedPluginSourceType.UNKNOWN
            and (
                current is None
                or current.trusted_source_type is TrustedPluginSourceType.UNKNOWN
                or current.trusted_source_type is not self.trusted_source_type
                or current.trusted_source_key != self.trusted_source_key
            )
        )
        return PluginIdentity(
            plugin_id=plugin_id,
            normalized_plugin_id=plugin_id.lower(),
            trusted_source_type=self.trusted_source_type,
            trusted_source_key=self.trusted_source_key,
            binding_basis=self.binding_basis,
            payload_source_type=self.candidate.payload_source_type,
            payload_source_key=(
                None
                if isinstance(self.candidate, PluginLocalCandidate)
                else self.candidate.source_key
            ),
            declared_version=installed_version,
            package_generation=self.candidate.package_generation,
            declared_metadata=PluginDeclaredMetadata.from_package(
                metadata,
                declaration_version=self.candidate.plugin_version,
                manifest_matches_payload=manifest_matches_payload,
            ),
            payload_receipt=payload_receipt,
            revision=(current.revision + 1) if current else 1,
            created_at=current.created_at if current else applied_at,
            updated_at=applied_at,
            bound_at=applied_at if source_binding_changed else self.bound_at,
            payload_applied_at=applied_at,
        )


def admit_plugin_install(
    inventory: CandidateInventory,
    *,
    request: PluginInstallAdmissionRequest,
    identity: PluginIdentity | None,
    now: datetime,
) -> PluginInstallAdmission:
    """选择唯一允许载荷，并冻结最终身份转换的可信来源边界。"""
    if now.tzinfo is None:
        raise PluginSourceAdmissionError("插件安装准入时间必须包含时区")
    if identity is not None and now < identity.updated_at:
        raise PluginSourceAdmissionError("插件安装准入时间不能早于当前身份更新时间")
    bound_at: datetime | None
    if request.source_change:
        if not request.explicit_source or not request.requested_repo_url:
            raise PluginSourceAdmissionError("请选择要更换到的插件仓库")
        if request.requested_repo_url.startswith("local://"):
            raise PluginSourceAdmissionError("只能更换为在线插件仓库")
        if identity is None or identity.trusted_source_type is TrustedPluginSourceType.UNKNOWN:
            raise PluginSourceAdmissionError("当前插件尚未绑定仓库")
        if request.expected_revision != identity.revision:
            raise PluginSourceAdmissionError("插件状态已变化，请重新打开页面后再试")
    elif request.expected_revision is not None:
        raise PluginSourceAdmissionError("插件操作参数无效，请重新打开页面后再试")

    requested_source_key = None
    local_candidates = None
    if request.requested_repo_url:
        if request.requested_repo_url.startswith("local://"):
            referenced_plugin_id = parse_local_plugin_reference(
                request.requested_repo_url
            )
            if (
                referenced_plugin_id is None
                or referenced_plugin_id.lower() != request.plugin_id.lower()
            ):
                raise PluginSourceAdmissionError(
                    "所选本地插件与安装目标不一致"
                )
            available_local_candidates = inventory.local_candidates_for(
                request.plugin_id
            )
            exact_candidates = tuple(
                candidate
                for candidate in available_local_candidates
                if candidate.repo_url == request.requested_repo_url
            )
            local_candidates = exact_candidates or available_local_candidates
            if not local_candidates:
                raise PluginSourceAdmissionError("所选本地仓库中没有该插件")
        else:
            requested_source_key, _repo_url = normalize_github_plugin_source(
                request.requested_repo_url
            )

    selection = select_plugin_candidate(
        inventory,
        plugin_id=request.plugin_id,
        generations=request.generations,
        identity=identity,
        local_candidates=local_candidates,
        requested_source_key=requested_source_key,
        explicit_source=request.explicit_source,
        allow_source_change=request.source_change,
    )
    if selection.status is not PluginSelectionStatus.SELECTED or selection.candidate is None:
        raise PluginSourceAdmissionError(selection.reason or "当前无法确认插件仓库")
    candidate = selection.candidate
    if not candidate.plugin_version:
        raise PluginSourceAdmissionError("插件包缺少版本信息，无法安装")

    if isinstance(candidate, PluginLocalCandidate):
        if identity is not None and identity.trusted_source_type is not TrustedPluginSourceType.UNKNOWN:
            return PluginInstallAdmission(
                candidate=candidate,
                identity_before=identity,
                binding_basis=identity.binding_basis,
                trusted_source_type=identity.trusted_source_type,
                trusted_source_key=identity.trusted_source_key,
                bound_at=identity.bound_at,
            )
        return PluginInstallAdmission(
            candidate=candidate,
            identity_before=identity,
            binding_basis=PluginBindingBasis.LOCAL_ONLY,
            trusted_source_type=TrustedPluginSourceType.UNKNOWN,
            trusted_source_key=None,
            bound_at=None,
        )

    if request.source_change:
        if (
            identity is not None
            and identity.trusted_source_type is candidate.source_type
            and identity.trusted_source_key == candidate.source_key
        ):
            raise PluginSourceAdmissionError("所选仓库与当前绑定仓库相同")
        basis = PluginBindingBasis.EXPLICIT_SOURCE_CHANGE
        bound_at = now
    elif identity is not None and identity.trusted_source_type is not TrustedPluginSourceType.UNKNOWN:
        basis = identity.binding_basis
        bound_at = identity.bound_at
    elif request.explicit_source:
        basis = PluginBindingBasis.EXPLICIT_INSTALL
        bound_at = now
    elif candidate.source_type is TrustedPluginSourceType.OFFICIAL:
        basis = PluginBindingBasis.OFFICIAL_DEFAULT
        bound_at = now
    else:
        basis = PluginBindingBasis.TOFU
        bound_at = now

    return PluginInstallAdmission(
        candidate=candidate,
        identity_before=identity,
        binding_basis=basis,
        trusted_source_type=candidate.source_type,
        trusted_source_key=candidate.source_key,
        bound_at=bound_at,
    )
