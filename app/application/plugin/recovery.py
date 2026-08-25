"""插件安装 journal 的启动恢复与已提交事务收尾。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.application.plugin.install import (
    PluginPackageCheckpoint,
    PluginPackageTransactionPort,
)
from app.application.plugin.transaction import (
    PluginInstallationPhase,
    PluginInstallationRecord,
    PluginPersistenceService,
)
from app.runtime.log import logger


class PluginInstallationRecoveryError(RuntimeError):
    """恢复材料不足或已提交载荷事实不一致，不能继续导入插件。"""


class PluginRecoveryPackagePort(PluginPackageTransactionPort, Protocol):
    """启动恢复在安装包事务端口之上需要的重建与核验能力。"""

    def restore_checkpoint(
        self,
        *,
        plugin_id: str,
        transaction_id: str,
        plugin_existed: bool,
        persistent_backup_existed: bool,
    ) -> PluginPackageCheckpoint:
        """只根据 journal 中的受限事实重建恢复路径。"""

    async def async_committed_payload_receipt(
        self,
        checkpoint: PluginPackageCheckpoint,
    ) -> str:
        """读取当前部署模式下一次已提交载荷的恢复收据。"""


@dataclass(frozen=True, slots=True)
class PluginInstallationRecoveryResult:
    """启动恢复批次的 PREPARED 回滚和 COMMITTED 收尾数量。"""

    restored: int = 0
    finalized: int = 0
    cleanup_pending: int = 0


class PluginInstallationRecoveryService:
    """在插件导入前把跨进程 journal 收敛到完整旧状态或新状态。"""

    def __init__(
        self,
        *,
        persistence: PluginPersistenceService,
        packages: PluginRecoveryPackagePort,
    ) -> None:
        """保存数据库最终事实和文件恢复端口。"""
        self.__persistence = persistence
        self.__packages = packages

    async def replay(self) -> PluginInstallationRecoveryResult:
        """按创建顺序恢复全部 journal；关键事实不一致时阻止插件启动。"""
        restored = 0
        finalized = 0
        cleanup_pending = 0
        for record in await self.__persistence.list_installations():
            checkpoint = self.__checkpoint(record)
            if record.phase is PluginInstallationPhase.PREPARED:
                await self.__restore_prepared(record, checkpoint)
                restored += 1
                continue
            if await self.__finish_committed(record, checkpoint):
                finalized += 1
            else:
                cleanup_pending += 1
        return PluginInstallationRecoveryResult(
            restored=restored,
            finalized=finalized,
            cleanup_pending=cleanup_pending,
        )

    def __checkpoint(
        self,
        record: PluginInstallationRecord,
    ) -> PluginPackageCheckpoint:
        """把 journal 映射为受控恢复路径，不读取任意持久化路径。"""
        return self.__packages.restore_checkpoint(
            plugin_id=record.plugin_id,
            transaction_id=record.transaction_id,
            plugin_existed=record.package_existed,
            persistent_backup_existed=record.persistent_backup_existed,
        )

    async def __restore_prepared(
        self,
        record: PluginInstallationRecord,
        checkpoint: PluginPackageCheckpoint,
    ) -> None:
        """恢复数据库提交前的文件状态，再释放 journal 所有权。"""
        try:
            await self.__packages.async_restore(checkpoint)
            await self.__persistence.delete_installation(
                record.transaction_id,
                expected_phase=PluginInstallationPhase.PREPARED,
            )
        except Exception as error:
            raise PluginInstallationRecoveryError(
                f"插件 {record.plugin_id} 的未提交安装恢复失败：{error}"
            ) from error
        try:
            await self.__packages.async_cleanup(checkpoint)
        except Exception as error:  # journal 已删除，孤儿材料不改变业务终态
            logger.warning(
                "插件安装事务 %s 已恢复，但恢复材料清理失败：%s",
                record.transaction_id,
                error,
            )

    async def __finish_committed(
        self,
        record: PluginInstallationRecord,
        checkpoint: PluginPackageCheckpoint,
    ) -> bool:
        """核验并收尾已提交载荷；非关键清理失败留待下一次启动。"""
        identity = await self.__persistence.get_identity(record.plugin_id)
        if identity is None or identity.revision != record.identity_target_revision:
            raise PluginInstallationRecoveryError(
                f"插件 {record.plugin_id} 的已提交身份与安装 journal 不一致"
            )
        try:
            receipt = await self.__packages.async_committed_payload_receipt(
                checkpoint
            )
        except Exception as error:
            raise PluginInstallationRecoveryError(
                f"插件 {record.plugin_id} 的已提交载荷无法核验：{error}"
            ) from error
        if receipt != identity.payload_receipt:
            raise PluginInstallationRecoveryError(
                f"插件 {record.plugin_id} 的已提交载荷收据不一致"
            )
        try:
            await self.__packages.async_finalize_persistent_backup(checkpoint)
        except Exception as error:
            raise PluginInstallationRecoveryError(
                f"插件 {record.plugin_id} 的持久备份终态不完整：{error}"
            ) from error
        try:
            await self.__packages.async_commit(checkpoint)
            await self.__persistence.delete_installation(
                record.transaction_id,
                expected_phase=PluginInstallationPhase.COMMITTED,
            )
        except Exception as error:
            logger.warning(
                "插件安装事务 %s 已提交但收尾仍待重试：%s",
                record.transaction_id,
                error,
            )
            return False
        return True


_RECOVERY_SERVICE: list[PluginInstallationRecoveryService] = []


def configure_plugin_installation_recovery(
    service: PluginInstallationRecoveryService,
) -> None:
    """由组合根登记当前 lifespan 的安装恢复服务。"""
    _RECOVERY_SERVICE.clear()
    _RECOVERY_SERVICE.append(service)


def get_plugin_installation_recovery() -> PluginInstallationRecoveryService:
    """返回已装配恢复服务；启动顺序错误时拒绝跳过恢复。"""
    if not _RECOVERY_SERVICE:
        raise RuntimeError("插件安装恢复服务尚未完成初始化")
    return _RECOVERY_SERVICE[0]


def reset_plugin_installation_recovery() -> None:
    """清除当前 lifespan 的恢复服务。"""
    _RECOVERY_SERVICE.clear()
