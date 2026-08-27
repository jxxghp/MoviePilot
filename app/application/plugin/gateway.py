"""统一插件安装 Gateway。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.application.plugin.admission import (
    PluginInstallAdmission,
    PluginInstallAdmissionRequest,
    PluginSourceAdmissionError,
    admit_plugin_install,
)
from app.application.plugin.identity import PluginIdentity
from app.application.plugin.install import PluginInstallResult
from app.application.plugin.inventory import PLUGIN_V3_GENERATIONS
from app.application.plugin.lifecycle import PluginStartupLease, plugin_lifecycle
from app.application.plugin.source import (
    Candidate,
    CandidateInventory,
    PluginLocalCandidate,
    PluginMarketCandidate,
    PluginSelection,
    get_effective_local_candidate,
    list_effective_online_candidates,
    select_plugin_candidate,
)

InventoryProvider = Callable[[bool], Awaitable[CandidateInventory]]
IdentityReader = Callable[[str], Awaitable[PluginIdentity | None]]
CandidateCompatibility = Callable[[Candidate], tuple[bool, str]]


@dataclass(frozen=True, slots=True)
class PluginSourceInspection:
    """前端与 Agent 选择来源所需的只读候选和当前身份快照。"""

    plugin_id: str
    inventory_complete: bool
    identity: PluginIdentity | None
    selection: PluginSelection
    online_candidates: tuple[PluginMarketCandidate, ...]
    local_candidate: PluginLocalCandidate | None


class PluginInstallExecutor(Protocol):
    """统一 Gateway 调用的可恢复安装执行端口。"""

    async def execute(
        self,
        *,
        admission: PluginInstallAdmission,
        release_version: str | None,
        force: bool,
        local_sync: bool = False,
    ) -> PluginInstallResult:
        """执行已通过来源准入的插件载荷事务。"""


class PluginInstallGateway:
    """让全部插件载荷写入共享同一来源策略和事务执行器。"""

    def __init__(
        self,
        *,
        inventory: InventoryProvider,
        identity: IdentityReader,
        candidate_compatibility: CandidateCompatibility,
        executor: PluginInstallExecutor,
        clock: Callable[[], datetime],
    ) -> None:
        """保存候选事实、身份读取、兼容校验、事务执行和时间端口。"""
        self.__inventory = inventory
        self.__identity = identity
        self.__candidate_compatibility = candidate_compatibility
        self.__executor = executor
        self.__clock = clock

    async def install(
        self,
        *,
        plugin_id: str,
        repo_url: str | None,
        package_version: str | None = None,
        release_version: str | None = None,
        force: bool = False,
        explicit_source: bool = False,
        source_change: bool = False,
        expected_revision: int | None = None,
        startup_token: PluginStartupLease | None = None,
        local_sync: bool = False,
    ) -> PluginInstallResult:
        """读取冻结库存并执行一次不能绕过来源身份的插件写入。"""
        try:
            inventory = await self.__inventory(force)
            async with plugin_lifecycle.hold(plugin_id, startup_token):
                identity = await self.__identity(plugin_id)
                admission = admit_plugin_install(
                    inventory,
                    request=PluginInstallAdmissionRequest(
                        plugin_id=plugin_id,
                        generations=_generation_order(package_version),
                        requested_repo_url=repo_url,
                        explicit_source=explicit_source,
                        source_change=source_change,
                        expected_revision=expected_revision,
                    ),
                    identity=identity,
                    now=self.__clock(),
                )
                compatible, message = self.__candidate_compatibility(
                    admission.candidate
                )
                if not compatible:
                    raise PluginSourceAdmissionError(
                        message or "插件包与当前 MoviePilot 版本不兼容"
                    )
                # 本地同步是最终准入候选的执行属性，不能由调用前的 URL 推断。
                return await self.__executor.execute(
                    admission=admission,
                    release_version=release_version,
                    force=force,
                    local_sync=(
                        local_sync
                        or isinstance(admission.candidate, PluginLocalCandidate)
                    ),
                )
        except (TypeError, ValueError, PluginSourceAdmissionError) as error:
            return PluginInstallResult(
                success=False,
                message=str(error),
                failure_stage="source_admission",
            )

    async def inspect_source(
        self,
        *,
        plugin_id: str,
        package_version: str | None = None,
        force: bool = False,
    ) -> PluginSourceInspection:
        """读取与真实安装相同的库存和身份，返回脱敏来源选择快照。"""
        inventory = await self.__inventory(force)
        identity = await self.__identity(plugin_id)
        generations = _generation_order(package_version)
        selection = select_plugin_candidate(
            inventory,
            plugin_id=plugin_id,
            generations=generations,
            identity=identity,
        )
        return PluginSourceInspection(
            plugin_id=plugin_id,
            inventory_complete=inventory.complete,
            identity=identity,
            selection=selection,
            online_candidates=list_effective_online_candidates(
                inventory,
                plugin_id=plugin_id,
                generations=generations,
            ),
            local_candidate=get_effective_local_candidate(
                inventory,
                plugin_id=plugin_id,
                generations=generations,
            ),
        )


_plugin_install_gateway: PluginInstallGateway | None = None


def configure_plugin_install_service(gateway: PluginInstallGateway) -> None:
    """由启动组合根发布当前 lifespan 的唯一插件安装 Gateway。"""
    global _plugin_install_gateway
    _plugin_install_gateway = gateway


def get_plugin_install_service() -> PluginInstallGateway:
    """返回已装配 Gateway；启动未完成时拒绝任何载荷写入。"""
    if _plugin_install_gateway is None:
        raise RuntimeError("插件安装服务尚未完成初始化")
    return _plugin_install_gateway


def reset_plugin_install_service() -> None:
    """清除当前 lifespan 的 Gateway，供停机和隔离测试使用。"""
    global _plugin_install_gateway
    _plugin_install_gateway = None


def _generation_order(package_version: str | None) -> tuple[str, ...]:
    """把兼容入口的首选代际转换为来源选择优先序。"""
    normalized = (package_version or "v3").strip().lower()
    if normalized in {"", "v1"}:
        return ("v1",)
    if normalized == "v2":
        return ("v2", "v1")
    if normalized == "v3":
        return PLUGIN_V3_GENERATIONS
    raise ValueError("插件包代际必须为 v1、v2 或 v3")
