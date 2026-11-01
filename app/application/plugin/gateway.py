"""
统一插件安装安装网关。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
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
from app.domain.plugin import PLUGIN_SYSTEM_VERSION_FIELD
from app.schemas.exception import PluginMutationRejectedError

InventoryProvider = Callable[[bool], Awaitable[CandidateInventory]]
IdentityReader = Callable[[str], Awaitable[PluginIdentity | None]]
CandidateCompatibility = Callable[[Candidate], tuple[bool, str]]
SourcePluginIdResolver = Callable[[str], str]


@dataclass(frozen=True, slots=True)
class PluginSourceInspection:
    """
    前端与 Agent 选择来源所需的只读候选和当前身份快照。

    :param plugin_id: 物理插件标识，不使用分身实例标识
    :param inventory_complete: 所需市场与代际是否均已得到可靠读取结果
    :param identity: 当前已持久化的来源身份；首次安装前可为 None
    :param selection: 按来源信任策略生成的候选选择结果
    :param online_candidates: 当前可供用户选择的在线候选
    :param local_candidate: 当前有效本地候选；没有可用候选时为 None
    """

    plugin_id: str
    inventory_complete: bool
    identity: PluginIdentity | None
    selection: PluginSelection
    online_candidates: tuple[PluginMarketCandidate, ...]
    local_candidate: PluginLocalCandidate | None


class PluginInstallExecutor(Protocol):
    """
    统一安装网关调用的可恢复安装执行端口。
    """

    async def execute(
        self,
        *,
        admission: PluginInstallAdmission,
        release_version: str | None,
        force: bool,
        local_sync: bool = False,
    ) -> PluginInstallResult:
        """
        执行已通过来源准入的插件载荷事务。

        :param admission: 已确认的唯一载荷候选与目标来源身份决策
        :param release_version: 指定的发布版本；None 使用候选默认版本
        :param force: 是否跳过已安装版本相同等短路检查
        :param local_sync: 是否只把本地载荷同步到运行目录，默认 False
        :return: 安装结果、数据库和运行态提交状态，以及失败补偿详情
        """


class PluginInstallGateway:
    """
    让全部插件载荷写入共享同一来源策略和事务执行器。
    """

    def __init__(
        self,
        *,
        inventory: InventoryProvider,
        identity: IdentityReader,
        candidate_compatibility: CandidateCompatibility,
        executor: PluginInstallExecutor,
        clock: Callable[[], datetime],
        source_plugin_id: SourcePluginIdResolver,
    ) -> None:
        """
        保存候选事实、身份读取、兼容校验、事务执行、时间和源身份归一端口。
        """
        self.__source_plugin_id = source_plugin_id
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
        """
        读取冻结库存并执行一次不能绕过来源身份的插件写入。

        先把分身归一到源插件，理由与来源勘察相同：安装包只登记在源插件名下，拿
        分身自身 ID 去查候选必然落空。界面上分身卡片的「版本历史」与「关于」两条
        入口都会把分身自己的 ID 送进来。

        归一必须早于生命周期占位：``hold`` 按 ID 互斥，用分身 ID 占位不会把同源
        分身的并发安装串起来，它们会同时改写同一份源插件载荷。
        """
        plugin_id = self.__source_plugin_id(plugin_id)
        try:
            # 安装 ``force`` 只控制载荷覆盖；远程市场索引由市场手动刷新和
            # 定时缓存任务维护，不能让单个插件更新放大全部仓库请求。
            inventory = await self.__inventory(False)
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
                    self._compatibility_candidate(admission.candidate, release_version)
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
                    local_sync=isinstance(
                        admission.candidate,
                        PluginLocalCandidate,
                    ),
                )
        except PluginMutationRejectedError as error:
            return PluginInstallResult(success=False, message=str(error), failure_stage="admission")
        except (TypeError, ValueError, PluginSourceAdmissionError) as error:
            return PluginInstallResult(
                success=False,
                message=str(error),
                failure_stage="source_admission",
            )

    @staticmethod
    def _compatibility_candidate(candidate: Candidate, release_version: str | None) -> Candidate:
        """
        历史远端Release的宿主范围交给实际版本选择，保留市场的来源和运行时要求。

        :param candidate: 已通过来源与代际准入的冻结候选
        :param release_version: 请求安装的Release版本
        :return: 仅兼容校验使用的候选视图，不修改事务使用的原候选
        """
        if (not release_version or release_version == candidate.plugin_version
                or isinstance(candidate, PluginLocalCandidate) or not isinstance(candidate.dto, Mapping)):
            return candidate
        metadata = dict(candidate.dto)
        metadata.pop(PLUGIN_SYSTEM_VERSION_FIELD, None)
        return replace(candidate, dto=metadata)

    async def inspect_source(
        self,
        *,
        plugin_id: str,
        package_version: str | None = None,
        force: bool = False,
    ) -> PluginSourceInspection:
        """
        读取与真实安装相同的库存和身份，返回脱敏来源选择快照。

        先把分身归一到源插件：分身只是共享同一份源码的运行实例，安装包与来源身份
        都只登记在源插件名下，拿分身自身 ID（源插件 ID 加后缀）去查库存必然落空，
        界面上表现为「没有找到插件 X 的可用安装包」。
        """
        plugin_id = self.__source_plugin_id(plugin_id)
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
    """
    由启动组合根发布当前 lifespan 的唯一插件安装安装网关。
    """
    global _plugin_install_gateway
    _plugin_install_gateway = gateway


def get_plugin_install_service() -> PluginInstallGateway:
    """
    返回已装配安装网关；启动未完成时拒绝任何载荷写入。
    """
    if _plugin_install_gateway is None:
        raise RuntimeError("插件安装服务尚未完成初始化")
    return _plugin_install_gateway


def reset_plugin_install_service() -> None:
    """
    清除当前 lifespan 的安装网关，供停机和隔离测试使用。
    """
    global _plugin_install_gateway
    _plugin_install_gateway = None


def _generation_order(package_version: str | None) -> tuple[str, ...]:
    """
    把兼容入口的首选代际转换为来源选择优先序。
    """
    normalized = (package_version or "v3").strip().lower()
    if normalized in {"", "v1"}:
        return ("v1",)
    if normalized == "v2":
        return ("v2", "v1")
    if normalized == "v3":
        return PLUGIN_V3_GENERATIONS
    raise ValueError("插件包代际必须为 v1、v2 或 v3")
