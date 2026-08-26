"""插件安装事务的持久化端口与可逆状态记录。"""

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
    """安装事务在持久化协调器中的两个数据库阶段。"""

    PREPARED = "prepared"
    COMMITTED = "committed"


class PluginInstallationConflictError(RuntimeError):
    """事务不存在、阶段竞争或实际状态发生漂移。"""


class PluginInstallationRecordError(ValueError):
    """事务记录不符合可持久化和恢复合同。"""


_TRANSACTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
INSTALLATION_JOURNAL_SCHEMA_VERSION = 1


def _validate_revision(value: int | None, *, field_name: str) -> None:
    """校验身份 CAS revision；``None`` 表示对应身份行不存在。"""
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PluginInstallationRecordError(
            f"{field_name} 必须是大于等于 1 的整数或 null"
        )


@dataclass(frozen=True, slots=True)
class PluginInstallationRecord:
    """可跨进程恢复的插件安装事务状态。"""

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

    def __post_init__(self) -> None:
        """拒绝不能作为 CAS 或崩溃恢复依据的事务记录。"""
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
    """安装 Gateway 使用的同步持久化端口。"""

    def create(self, record: PluginInstallationRecord) -> PluginInstallationRecord:
        """创建一条 PREPARED 安装事务。"""

    def get(self, transaction_id: str) -> PluginInstallationRecord | None:
        """按事务 ID 读取记录。"""

    def list(
        self,
        *,
        plugin_id: str | None = None,
    ) -> list[PluginInstallationRecord]:
        """列出事务记录。"""

    def set_target(
        self,
        transaction_id: str,
        *,
        membership_target: bool,
        identity_target: PluginIdentity | None,
        expected_phase: PluginInstallationPhase,
    ) -> PluginInstallationRecord:
        """在 PREPARED 阶段登记目标 membership 和身份 revision。"""

    def commit_target(
        self,
        transaction_id: str,
        *,
        identity_target: PluginIdentity | None,
        expected_phase: PluginInstallationPhase,
    ) -> PluginInstallationRecord:
        """在同一同步事务中完成身份、membership 和 COMMITTED phase。"""

    def delete(
        self,
        transaction_id: str,
        *,
        expected_phase: PluginInstallationPhase,
    ) -> bool:
        """按 phase CAS 删除已处理的事务记录。"""


class PluginIdentityPersistence(Protocol):
    """插件来源身份读取与存量迁移使用的同步窄端口。"""

    def get(self, plugin_id: str) -> PluginIdentity | None:
        """读取一个物理插件的来源身份。"""

    def list(self, plugin_ids: Sequence[str]) -> list[PluginIdentity]:
        """批量读取指定物理插件的来源身份。"""

    def compare_and_set(
        self,
        identity: PluginIdentity,
        *,
        expected_revision: int | None,
    ) -> PluginIdentity:
        """仅供存量迁移首次创建或按 revision 更新身份。"""

    def bind_online(
        self,
        identity: PluginIdentity,
        *,
        expected_revision: int,
    ) -> PluginIdentity:
        """把存量未绑定身份按 revision 绑定到在线来源。"""


class PluginPersistenceService:
    """通过有界数据库 worker 暴露插件专用异步持久化能力。"""

    def __init__(
        self,
        *,
        executor: AsyncDatabaseExecutor,
        identities: PluginIdentityPersistence,
        installations: PluginInstallationStore,
    ) -> None:
        """保存身份、安装事务和唯一同步数据库执行边界。"""
        self.__executor = executor
        self.__identities = identities
        self.__installations = installations

    async def get_identity(self, plugin_id: str) -> PluginIdentity | None:
        """在数据库 worker 中读取插件来源身份。"""
        return await self.__executor.run(partial(self.__identities.get, plugin_id))

    async def list_identities(
        self,
        plugin_ids: Sequence[str],
    ) -> list[PluginIdentity]:
        """在一次数据库任务中批量读取插件来源身份。"""
        return await self.__executor.run(
            partial(self.__identities.list, tuple(plugin_ids))
        )

    async def migrate_identity(
        self,
        identity: PluginIdentity,
        *,
        expected_revision: int | None,
    ) -> PluginIdentity:
        """在数据库 worker 中提交存量身份迁移。"""
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
        """在数据库 worker 中绑定存量身份的可信在线来源。"""
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
        """创建 PREPARED journal。"""
        return await self.__executor.run(
            partial(self.__installations.create, record)
        )

    async def list_installations(self) -> list[PluginInstallationRecord]:
        """列出全部待恢复或待清理的安装 journal。"""
        return await self.__executor.run(self.__installations.list)

    async def get_installation(
        self,
        transaction_id: str,
    ) -> PluginInstallationRecord | None:
        """读取一次提交结果确认所需的安装 journal。"""
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
        """在 PREPARED journal 中登记最终数据库目标。"""
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
        """原子提交 membership、身份和 COMMITTED phase。"""
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
        """按 phase 删除已恢复或已收尾的 journal。"""
        return await self.__executor.run(
            partial(
                self.__installations.delete,
                transaction_id,
                expected_phase=expected_phase,
            )
        )


_PLUGIN_PERSISTENCE: list[PluginPersistenceService] = []


def configure_plugin_persistence(service: PluginPersistenceService) -> None:
    """由启动组合根登记当前 lifespan 的插件持久化服务。"""
    _PLUGIN_PERSISTENCE.clear()
    _PLUGIN_PERSISTENCE.append(service)


def get_plugin_persistence() -> PluginPersistenceService:
    """返回当前插件持久化服务，未装配时拒绝数据库操作。"""
    if not _PLUGIN_PERSISTENCE:
        raise RuntimeError("插件持久化服务尚未完成初始化")
    return _PLUGIN_PERSISTENCE[0]


def reset_plugin_persistence() -> None:
    """清除当前 lifespan 的插件持久化服务。"""
    _PLUGIN_PERSISTENCE.clear()
