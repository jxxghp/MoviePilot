"""
插件安装事务的持久化端口与可逆状态记录。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from functools import partial
from typing import Protocol, TypeVar

from app.application.database import AsyncDatabaseExecutor
from app.application.plugin.identity import PluginIdentity

T = TypeVar("T")


class PluginInstallationPhase(StrEnum):
    """
    安装事务在持久化协调器中的两个数据库阶段。
    """

    # 已记录安装前事实，数据库目标尚未提交
    PREPARED = "prepared"
    # 身份和已安装清单目标已提交，剩余恢复材料可收尾
    COMMITTED = "committed"


class PluginInstallationConflictError(RuntimeError):
    """
    事务不存在、阶段竞争或实际状态发生漂移。
    """


class PluginInstallationRecordError(ValueError):
    """
    事务记录不符合可持久化和恢复合同。
    """


_TRANSACTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
INSTALLATION_JOURNAL_SCHEMA_VERSION = 1


def _validate_revision(value: int | None, *, field_name: str) -> None:
    """
    校验身份 CAS 版本号；``None`` 表示对应身份行不存在。
    """
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PluginInstallationRecordError(
            f"{field_name} 必须是大于等于 1 的整数或 null"
        )


@dataclass(frozen=True, slots=True)
class PluginInstallationRecord:
    """
    可跨进程恢复的插件安装事务状态。

    :param transaction_id: 可跨重启定位恢复材料的安装事务标识
    :param plugin_id: 物理插件标识，不使用分身实例标识
    :param phase: 数据库协调阶段，区分已准备和已提交
    :param membership_before: 安装前插件是否属于已安装清单
    :param membership_target: 拟提交的已安装清单成员状态；尚未确定时为 None
    :param identity_before_revision: 安装前来源身份版本；无身份行时为 None
    :param identity_target_revision: 拟提交的来源身份版本；无身份目标时为 None
    :param package_existed: 安装前插件运行目录是否存在
    :param persistent_backup_existed: 安装前是否存在容器持久恢复备份
    :param created_at: 审计事实首次创建时间，必须包含时区
    :param updated_at: 审计事实最近更新时间，必须包含时区
    :param schema_version: 安装日志结构版本，必须为当前支持版本
    :param host_enabled_before: 安装前本体启用位；旧日志未知时为None，不能推测恢复值
    """

    transaction_id: str
    plugin_id: str
    phase: PluginInstallationPhase
    membership_before: bool
    membership_target: bool | None
    identity_before_revision: int | None
    identity_target_revision: int | None
    package_existed: bool
    persistent_backup_existed: bool
    created_at: datetime
    updated_at: datetime
    schema_version: int = INSTALLATION_JOURNAL_SCHEMA_VERSION
    host_enabled_before: bool | None = None

    def __post_init__(self) -> None:
        """
        拒绝不能作为 CAS 或崩溃恢复依据的事务记录。
        """
        if not _TRANSACTION_ID_PATTERN.fullmatch(self.transaction_id):
            raise PluginInstallationRecordError("transaction_id 格式不合法")
        if not self.plugin_id or self.plugin_id != self.plugin_id.strip():
            raise PluginInstallationRecordError("plugin_id 不能为空或带首尾空格")
        if not isinstance(self.phase, PluginInstallationPhase):
            try:
                object.__setattr__(self, "phase", PluginInstallationPhase(self.phase))
            except ValueError as error:
                raise PluginInstallationRecordError("未知安装事务 phase") from error

        if not isinstance(self.membership_before, bool):
            raise PluginInstallationRecordError("membership_before 必须是布尔值")
        if self.host_enabled_before is not None and not isinstance(self.host_enabled_before, bool):
            raise PluginInstallationRecordError("host_enabled_before 必须是布尔值或null")
        if not isinstance(self.package_existed, bool):
            raise PluginInstallationRecordError("package_existed 必须是布尔值")
        if not isinstance(self.persistent_backup_existed, bool):
            raise PluginInstallationRecordError(
                "persistent_backup_existed 必须是布尔值"
            )
        if self.membership_target is not None and not isinstance(
            self.membership_target,
            bool,
        ):
            raise PluginInstallationRecordError("membership_target 必须是布尔值或 null")
        if (
            self.phase is PluginInstallationPhase.COMMITTED
            and self.membership_target is None
        ):
            raise PluginInstallationRecordError(
                "COMMITTED 事务必须包含 membership target"
            )

        _validate_revision(
            self.identity_before_revision,
            field_name="identity_before_revision",
        )
        _validate_revision(
            self.identity_target_revision,
            field_name="identity_target_revision",
        )
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise PluginInstallationRecordError("事务时间必须包含时区")
        if self.updated_at < self.created_at:
            raise PluginInstallationRecordError("事务更新时间不能早于创建时间")
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != INSTALLATION_JOURNAL_SCHEMA_VERSION
        ):
            raise PluginInstallationRecordError(
                f"不支持的插件安装事务快照版本: {self.schema_version}"
            )


class PluginInstallationStore(Protocol):
    """
    安装安装网关使用的同步持久化端口。
    """

    def create(self, record: PluginInstallationRecord) -> PluginInstallationRecord:
        """
        创建一条 PREPARED 安装事务。

        :param record: 包含安装前事实的 PREPARED 事务记录
        :return: 已持久化的准备阶段安装记录
        """

    def get(self, transaction_id: str) -> PluginInstallationRecord | None:
        """
        按事务 ID 读取记录。

        :param transaction_id: 安装事务的稳定标识，用于定位日志和恢复材料
        :return: 安装事务记录；不存在时为 None
        """

    def list(
        self,
        *,
        plugin_id: str | None = None,
    ) -> list[PluginInstallationRecord]:
        """
        列出事务记录。

        :param plugin_id: 只读取该物理插件的安装记录；None 表示读取全部插件
        :return: 满足插件筛选的安装记录列表
        """

    def set_target(
        self,
        transaction_id: str,
        *,
        membership_target: bool,
        identity_target: PluginIdentity | None,
        expected_phase: PluginInstallationPhase,
    ) -> PluginInstallationRecord:
        """
        在 PREPARED 阶段登记目标已安装清单成员状态和身份版本号。

        :param transaction_id: 安装事务的稳定标识，用于定位日志和恢复材料
        :param membership_target: 提交后插件是否属于已安装清单
        :param identity_target: 与安装事务一起提交的来源身份；None 表示无身份更新目标
        :param expected_phase: 调用方预期的当前安装阶段，用于拒绝并发漂移
        :return: 已保存目标成员状态和身份版本的事务记录
        """

    def commit_target(
        self,
        transaction_id: str,
        *,
        identity_target: PluginIdentity | None,
        expected_phase: PluginInstallationPhase,
    ) -> PluginInstallationRecord:
        """
        在同一同步事务中完成身份、已安装清单成员状态和 COMMITTED phase。

        :param transaction_id: 安装事务的稳定标识，用于定位日志和恢复材料
        :param identity_target: 与安装事务一起提交的来源身份；None 表示无身份更新目标
        :param expected_phase: 调用方预期的当前安装阶段，用于拒绝并发漂移
        :return: 身份、已安装清单和日志阶段共同提交后的记录
        """

    def delete(
        self,
        transaction_id: str,
        *,
        expected_phase: PluginInstallationPhase,
    ) -> bool:
        """
        按阶段条件比较删除已处理的事务记录。

        :param transaction_id: 安装事务的稳定标识，用于定位日志和恢复材料
        :param expected_phase: 调用方预期的当前安装阶段，用于拒绝并发漂移
        :return: 日志存在且按期望阶段删除时为 True；不存在时为 False
        """


class PluginIdentityPersistence(Protocol):
    """
    插件来源身份读取与存量迁移使用的同步窄端口。
    """

    def get(self, plugin_id: str) -> PluginIdentity | None:
        """
        读取一个物理插件的来源身份。

        :param plugin_id: 目标插件的物理标识
        :return: 该物理插件的来源身份；尚未建立时为 None
        """

    def list(self, plugin_ids: Sequence[str]) -> list[PluginIdentity]:
        """
        批量读取指定物理插件的来源身份。

        :param plugin_ids: 需要批量读取身份的物理插件标识
        :return: 已存在的来源身份列表；未找到的插件不产生条目
        """

    def compare_and_set(
        self,
        identity: PluginIdentity,
        *,
        expected_revision: int | None,
    ) -> PluginIdentity:
        """
        仅供存量迁移首次创建或按版本号更新身份。

        :param identity: 拟创建或替换的完整来源身份
        :param expected_revision: 预期的当前身份版本；None 表示只允许首次创建
        :return: 提交后的来源身份；版本竞争通过身份冲突异常报告
        """

    def bind_online(
        self,
        identity: PluginIdentity,
        *,
        expected_revision: int,
    ) -> PluginIdentity:
        """
        把存量未绑定身份按版本号绑定到在线来源。

        :param identity: 拟创建或替换的完整来源身份
        :param expected_revision: 当前已存在身份的预期版本，用于拒绝并发更新
        :return: 从未绑定状态完成在线来源绑定后的身份
        """


class PluginPersistenceService:
    """
    通过有界数据库工作线程暴露插件专用异步持久化能力。
    """

    def __init__(
        self,
        *,
        executor: AsyncDatabaseExecutor,
        identities: PluginIdentityPersistence,
        installations: PluginInstallationStore,
    ) -> None:
        """
        保存身份、安装事务和唯一同步数据库执行边界。
        """
        self.__executor = executor
        self.__identities = identities
        self.__installations = installations

    async def get_identity(self, plugin_id: str) -> PluginIdentity | None:
        """
        在数据库工作线程中读取插件来源身份。
        """
        return await self.__executor.run(partial(self.__identities.get, plugin_id))

    async def list_identities(
        self,
        plugin_ids: Sequence[str],
    ) -> list[PluginIdentity]:
        """
        在一次数据库任务中批量读取插件来源身份。
        """
        return await self.__executor.run(
            partial(self.__identities.list, tuple(plugin_ids))
        )

    async def migrate_identity(
        self,
        identity: PluginIdentity,
        *,
        expected_revision: int | None,
    ) -> PluginIdentity:
        """
        在数据库工作线程中提交存量身份迁移。
        """
        return await self.__executor.run(
            partial(
                self.__identities.compare_and_set,
                identity,
                expected_revision=expected_revision,
            )
        )

    async def bind_online_identity(
        self,
        identity: PluginIdentity,
        *,
        expected_revision: int,
    ) -> PluginIdentity:
        """
        在数据库工作线程中绑定存量身份的可信在线来源。
        """
        return await self.__executor.run(
            partial(
                self.__identities.bind_online,
                identity,
                expected_revision=expected_revision,
            )
        )

    async def create_installation(
        self,
        record: PluginInstallationRecord,
    ) -> PluginInstallationRecord:
        """
        创建 PREPARED 安装日志。
        """
        return await self.__executor.run(
            partial(self.__installations.create, record)
        )

    async def list_installations(self) -> list[PluginInstallationRecord]:
        """
        列出全部待恢复或待清理的安装安装日志。
        """
        return await self.__executor.run(self.__installations.list)

    async def get_installation(
        self,
        transaction_id: str,
    ) -> PluginInstallationRecord | None:
        """
        读取一次提交结果确认所需的安装安装日志。
        """
        return await self.__executor.run(
            partial(self.__installations.get, transaction_id)
        )

    async def set_installation_target(
        self,
        transaction_id: str,
        *,
        membership_target: bool,
        identity_target: PluginIdentity | None,
    ) -> PluginInstallationRecord:
        """
        在 PREPARED 安装日志中登记最终数据库目标。
        """
        return await self.__executor.run(
            partial(
                self.__installations.set_target,
                transaction_id,
                membership_target=membership_target,
                identity_target=identity_target,
                expected_phase=PluginInstallationPhase.PREPARED,
            )
        )

    async def commit_installation(
        self,
        transaction_id: str,
        *,
        identity_target: PluginIdentity | None,
    ) -> PluginInstallationRecord:
        """
        原子提交已安装清单成员状态、身份和 COMMITTED phase。
        """
        return await self.__executor.run(
            partial(
                self.__installations.commit_target,
                transaction_id,
                identity_target=identity_target,
                expected_phase=PluginInstallationPhase.PREPARED,
            )
        )

    async def delete_installation(
        self,
        transaction_id: str,
        *,
        expected_phase: PluginInstallationPhase,
    ) -> bool:
        """
        按 phase 删除已恢复或已收尾的安装日志。
        """
        return await self.__executor.run(
            partial(
                self.__installations.delete,
                transaction_id,
                expected_phase=expected_phase,
            )
        )


_PLUGIN_PERSISTENCE: list[PluginPersistenceService] = []


def configure_plugin_persistence(service: PluginPersistenceService) -> None:
    """
    由启动组合根登记当前 lifespan 的插件持久化服务。
    """
    _PLUGIN_PERSISTENCE.clear()
    _PLUGIN_PERSISTENCE.append(service)


def get_plugin_persistence() -> PluginPersistenceService:
    """
    返回当前插件持久化服务，未装配时拒绝数据库操作。
    """
    if not _PLUGIN_PERSISTENCE:
        raise RuntimeError("插件持久化服务尚未完成初始化")
    return _PLUGIN_PERSISTENCE[0]


def reset_plugin_persistence() -> None:
    """
    清除当前 lifespan 的插件持久化服务。
    """
    _PLUGIN_PERSISTENCE.clear()
