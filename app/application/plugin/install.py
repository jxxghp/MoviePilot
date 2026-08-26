"""插件载荷安装、数据库提交和运行态切换的统一应用用例。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ContextManager, Protocol, TypeVar

from app.application.plugin.admission import PluginInstallAdmission
from app.application.plugin.identity import PluginIdentity, PluginPayloadSourceType
from app.application.plugin.source import PluginLocalCandidate
from app.application.plugin.transaction import (
    PluginInstallationConflictError,
    PluginInstallationPhase,
    PluginInstallationRecord,
    PluginPersistenceService,
)
from app.runtime.execution import await_task_to_terminal
from app.runtime.log import logger
from app.schemas.exception import (
    PersistenceUnavailableError,
    PluginMutationRejectedError,
)
from app.schemas.plugin import PluginRuntimeStatus

InstalledPluginsReader = Callable[[], list[str]]
PluginIdsProvider = Callable[[], list[str]]
InstallReporter = Callable[[str, str | None], Awaitable[object]]
PluginReloader = Callable[[str], Awaitable[PluginRuntimeStatus]]
PluginRegistrationRefresher = Callable[[str], Awaitable[object]]
PluginMutationAdmission = Callable[[str], ContextManager[None]]
PluginPackageWriteGuard = Callable[[str], ContextManager[None]]
T = TypeVar("T")


class PluginPackageCheckpoint(Protocol):
    """安装 journal 构造和崩溃恢复所需的最小文件快照事实。"""

    plugin_existed: bool
    persistent_backup_existed: bool


class PluginPackageTransactionPort(Protocol):
    """插件安装用例可见的唯一文件与持久备份事务端口。"""

    async def async_checkpoint(
        self,
        plugin_id: str,
        transaction_id: str | None = None,
    ) -> PluginPackageCheckpoint:
        """创建运行目录恢复快照。"""

    async def async_install(
        self,
        plugin_id: str,
        repo_url: str,
        package_version: str | None = None,
        release_version: str | None = None,
        force_install: bool = False,
    ) -> tuple[bool, str]:
        """执行已经通过来源准入的原始包安装。"""

    async def async_restore(self, checkpoint: PluginPackageCheckpoint) -> None:
        """恢复数据库提交前的运行目录和持久备份。"""

    async def async_cleanup(self, checkpoint: PluginPackageCheckpoint) -> None:
        """清理已不再被 journal 引用的恢复材料。"""

    async def async_stage_persistent_backup(
        self,
        checkpoint: PluginPackageCheckpoint,
    ) -> None:
        """准备新的容器持久恢复备份。"""

    async def async_activate_persistent_backup(
        self,
        checkpoint: PluginPackageCheckpoint,
    ) -> None:
        """激活新备份并保留旧备份用于提交前补偿。"""

    async def async_finalize_persistent_backup(
        self,
        checkpoint: PluginPackageCheckpoint,
    ) -> None:
        """数据库提交后删除旧持久备份。"""

    async def async_commit(self, checkpoint: PluginPackageCheckpoint) -> None:
        """清理已提交事务的运行目录快照。"""

    async def async_payload_receipt(self, plugin_id: str) -> str:
        """读取当前运行目录的稳定载荷收据。"""


@dataclass(frozen=True, slots=True)
class PluginInstallRollback:
    """描述提交前失败中各类可补偿副作用的恢复结果。"""

    file_attempted: bool = False
    file_restored: bool = False
    runtime_attempted: bool = False
    runtime_restored: bool = False
    registrations_attempted: bool = False
    registrations_restored: bool = False
    journal_deleted: bool = False
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PluginInstallResult:
    """描述插件安装结果、失败阶段和可观察补偿状态。"""

    success: bool
    message: str = ""
    refreshed_only: bool = False
    package_installed: bool = False
    installed_list_persisted: bool = False
    runtime_reloaded: bool = False
    registrations_refreshed: bool = False
    reported: bool = False
    report_error: str = ""
    failure_stage: str | None = None
    checkpoint_cleanup_error: str = ""
    rollback: PluginInstallRollback = field(default_factory=PluginInstallRollback)


@dataclass(slots=True)
class _InstallState:
    """记录取消补偿和数据库提交边界所需的事务状态。"""

    transaction_id: str
    checkpoint: Any = None
    journal_created: bool = False
    journal_unknown: bool = False
    target_identity: PluginIdentity | None = None
    stage: str = "package_checkpoint"
    package_installed: bool = False
    runtime_touched: bool = False
    registrations_touched: bool = False
    committed: bool = False
    commit_unknown: bool = False


class PluginInstallCommand:
    """以一个 Gateway 后端协调来源、文件、数据库与运行态提交。"""

    def __init__(
        self,
        *,
        persistence: PluginPersistenceService,
        installed_plugins_reader: InstalledPluginsReader,
        plugin_ids_provider: PluginIdsProvider,
        packages: PluginPackageTransactionPort,
        install_reporter: InstallReporter,
        target_reloader: PluginReloader,
        rollback_reloader: PluginReloader,
        registration_refresher: PluginRegistrationRefresher,
        mutation: PluginMutationAdmission,
        package_write_guard: PluginPackageWriteGuard,
        clock: Callable[[], datetime],
        transaction_id_factory: Callable[[], str],
    ) -> None:
        """保存单一安装事务所需的窄端口。"""
        self.__persistence = persistence
        self.__installed_plugins_reader = installed_plugins_reader
        self.__plugin_ids_provider = plugin_ids_provider
        self.__packages = packages
        self.__install_reporter = install_reporter
        self.__target_reloader = target_reloader
        self.__rollback_reloader = rollback_reloader
        self.__registration_refresher = registration_refresher
        self.__mutation = mutation
        self.__package_write_guard = package_write_guard
        self.__clock = clock
        self.__transaction_id_factory = transaction_id_factory

    async def execute(
        self,
        *,
        admission: PluginInstallAdmission,
        release_version: str | None,
        force: bool,
        local_sync: bool = False,
    ) -> PluginInstallResult:
        """执行已准入事务；共享 Python 依赖不属于文件与数据库补偿边界。"""
        plugin_id = admission.candidate.plugin_id
        state = _InstallState(transaction_id=self.__transaction_id_factory())
        try:
            with self.__mutation(f"安装插件 {plugin_id}"):
                with self.__package_write_guard(plugin_id):
                    try:
                        return await self.__execute_locked(
                            admission=admission,
                            release_version=release_version,
                            force=force,
                            local_sync=local_sync,
                            state=state,
                        )
                    except asyncio.CancelledError:
                        await self.__rollback_cancelled(
                            plugin_id=plugin_id,
                            state=state,
                        )
                        raise
        except PluginMutationRejectedError as error:
            return PluginInstallResult(
                success=False,
                message=str(error),
                failure_stage="admission",
            )

    async def __execute_locked(
        self,
        *,
        admission: PluginInstallAdmission,
        release_version: str | None,
        force: bool,
        local_sync: bool,
        state: _InstallState,
    ) -> PluginInstallResult:
        """执行文件准备、运行态验证和数据库最终提交。"""
        candidate = admission.candidate
        plugin_id = candidate.plugin_id
        membership_before = any(
            item.lower() == plugin_id.lower()
            for item in (self.__installed_plugins_reader() or [])
        )
        if (
            not force
            and not local_sync
            and release_version is None
            and any(
                item.lower() == plugin_id.lower()
                for item in self.__plugin_ids_provider()
            )
            and await self.__payload_matches(admission)
        ):
            return await self.__refresh_existing(
                plugin_id,
                candidate.repo_url,
                report=not isinstance(candidate, PluginLocalCandidate),
            )

        try:
            async def create_checkpoint() -> None:
                """在取消传播前保存已经创建完成的恢复材料引用。"""
                state.checkpoint = await self.__packages.async_checkpoint(
                    plugin_id,
                    state.transaction_id,
                )

            await self.__await_side_effect(create_checkpoint())
        except Exception as error:
            return PluginInstallResult(
                success=False,
                message=f"创建插件安装快照失败：{error}",
                failure_stage="package_checkpoint",
            )

        now = self.__clock()
        record = PluginInstallationRecord(
            transaction_id=state.transaction_id,
            plugin_id=plugin_id,
            phase=PluginInstallationPhase.PREPARED,
            membership_before=membership_before,
            membership_target=None,
            identity_before_revision=admission.expected_revision,
            identity_target_revision=None,
            package_existed=bool(state.checkpoint.plugin_existed),
            persistent_backup_existed=bool(
                state.checkpoint.persistent_backup_existed
            ),
            created_at=now,
            updated_at=now,
        )
        try:
            async def create_journal() -> None:
                """在取消传播前记录 PREPARED 已持久化。"""
                await self.__persistence.create_installation(record)
                state.journal_created = True

            await self.__await_side_effect(create_journal())
        except asyncio.CancelledError:
            if not state.journal_created:
                await self.__resolve_journal_created(state)
            raise
        except PluginInstallationConflictError as error:
            rollback = await self.__rollback_without_journal(state.checkpoint)
            return PluginInstallResult(
                success=False,
                message=str(error),
                failure_stage="journal_prepare_conflict",
                rollback=rollback,
            )
        except Exception as error:
            journal_created = await self.__resolve_journal_created(state)
            if journal_created is None:
                return PluginInstallResult(
                    success=False,
                    message=(
                        "插件安装事务创建结果暂时无法确认，已保留恢复材料，"
                        "重启后将自动核对"
                    ),
                    failure_stage="journal_prepare_unknown",
                )
            rollback = (
                await self.__fail_prepared(plugin_id=plugin_id, state=state)
                if journal_created
                else await self.__rollback_without_journal(state.checkpoint)
            )
            if isinstance(error, PersistenceUnavailableError):
                raise
            return PluginInstallResult(
                success=False,
                message=f"创建插件安装事务失败：{error}",
                failure_stage="journal_prepare",
                rollback=rollback,
            )

        state.stage = "package_install"
        try:
            package_installed, message = await self.__await_side_effect(
                self.__packages.async_install(
                    plugin_id=plugin_id,
                    repo_url=candidate.repo_url,
                    package_version=candidate.package_generation,
                    release_version=release_version,
                    force_install=True,
                )
            )
            state.package_installed = package_installed
        except Exception as error:
            if isinstance(error, PersistenceUnavailableError):
                await self.__fail_prepared(plugin_id=plugin_id, state=state)
                raise
            return await self.__failure_result(
                plugin_id=plugin_id,
                state=state,
                stage="package_install",
                message=str(error),
            )
        if not package_installed:
            return await self.__failure_result(
                plugin_id=plugin_id,
                state=state,
                stage="package_install",
                message=message,
            )

        try:
            state.stage = "payload_receipt"
            receipt = await self.__await_side_effect(
                self.__packages.async_payload_receipt(plugin_id)
            )
            state.target_identity = admission.build_identity(
                payload_receipt=receipt,
                applied_at=self.__clock(),
                declared_version=release_version,
                manifest_matches_payload=(
                    release_version is None
                    or release_version == candidate.plugin_version
                ),
            )
            await self.__await_side_effect(
                self.__persistence.set_installation_target(
                    state.transaction_id,
                    membership_target=True,
                    identity_target=state.target_identity,
                )
            )

            state.stage = "persistent_backup_stage"
            await self.__await_side_effect(
                self.__packages.async_stage_persistent_backup(state.checkpoint)
            )
            state.stage = "persistent_backup_activate"
            await self.__await_side_effect(
                self.__packages.async_activate_persistent_backup(state.checkpoint)
            )

            state.stage = "runtime_reload"
            state.runtime_touched = True
            await self.__reload_active(plugin_id)
            state.stage = "registration_refresh"
            state.registrations_touched = True
            await self.__await_side_effect(
                self.__registration_refresher(plugin_id)
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if isinstance(error, PersistenceUnavailableError):
                await self.__fail_prepared(plugin_id=plugin_id, state=state)
                raise
            return await self.__failure_result(
                plugin_id=plugin_id,
                state=state,
                stage=state.stage,
                message=str(error),
            )

        state.stage = "database_commit"
        try:
            async def commit_database() -> None:
                """仅在数据库调用明确返回后标记提交已确认。"""
                await self.__persistence.commit_installation(
                    state.transaction_id,
                    identity_target=state.target_identity,
                )
                state.committed = True

            await self.__await_side_effect(commit_database())
        except asyncio.CancelledError:
            await self.__resolve_commit_outcome(state)
            raise
        except Exception as error:
            outcome = await self.__resolve_commit_outcome(state)
            if outcome is None:
                return PluginInstallResult(
                    success=False,
                    message=(
                        "插件数据库提交结果暂时无法确认，已保留安装事务，"
                        "重启后将自动恢复"
                    ),
                    package_installed=True,
                    runtime_reloaded=True,
                    registrations_refreshed=True,
                    failure_stage="database_commit_unknown",
                )
            if not outcome:
                if isinstance(error, PersistenceUnavailableError):
                    await self.__fail_prepared(plugin_id=plugin_id, state=state)
                    raise
                return await self.__failure_result(
                    plugin_id=plugin_id,
                    state=state,
                    stage="database_commit",
                    message=str(error),
                )
            logger.warning(
                "插件安装事务 %s 提交返回异常，但数据库已确认 COMMITTED：%s",
                state.transaction_id,
                error,
            )

        checkpoint_cleanup_error = await self.__finish_committed(state)
        reported, report_error = await self.__report(
            plugin_id,
            candidate.repo_url,
            enabled=(
                not local_sync
                and not isinstance(candidate, PluginLocalCandidate)
            ),
        )
        result_message = message or "插件安装成功"
        if checkpoint_cleanup_error:
            result_message = f"{result_message}；安装事务待下次启动继续清理"
        if report_error:
            result_message = f"{result_message}；安装上报失败，不影响本地安装"
        return PluginInstallResult(
            success=True,
            message=result_message,
            package_installed=True,
            installed_list_persisted=True,
            runtime_reloaded=True,
            registrations_refreshed=True,
            reported=reported,
            report_error=report_error,
            checkpoint_cleanup_error=checkpoint_cleanup_error,
        )

    async def __payload_matches(self, admission: PluginInstallAdmission) -> bool:
        """确认身份元数据与当前运行目录收据都描述同一载荷。"""
        identity = admission.identity_before
        candidate = admission.candidate
        if identity is None or identity.payload_source_type is PluginPayloadSourceType.UNKNOWN:
            return False
        if (
            identity.declared_version != candidate.plugin_version
            or identity.package_generation != candidate.package_generation
            or identity.payload_source_type is not candidate.payload_source_type
        ):
            return False
        source_matches = (
            identity.payload_source_key is None
            if isinstance(candidate, PluginLocalCandidate)
            else identity.payload_source_key == candidate.source_key
        )
        if not source_matches or identity.payload_receipt is None:
            return False
        try:
            current_receipt = await self.__await_side_effect(
                self.__packages.async_payload_receipt(candidate.plugin_id)
            )
        except Exception as error:  # noqa: BLE001 - 无法证明相同就走完整安装
            logger.warning(
                "读取插件 %s 当前载荷收据失败，将重新安装：%s",
                candidate.plugin_id,
                error,
            )
            return False
        return current_receipt == identity.payload_receipt

    async def __refresh_existing(
        self,
        plugin_id: str,
        repo_url: str | None,
        *,
        report: bool,
    ) -> PluginInstallResult:
        """刷新已经提交且载荷事实未变化的插件运行态。"""
        failure_stage = "runtime_reload"
        try:
            await self.__reload_active(plugin_id)
            failure_stage = "registration_refresh"
            await self.__await_side_effect(
                self.__registration_refresher(plugin_id)
            )
        except Exception as error:
            return PluginInstallResult(
                success=False,
                message=f"刷新插件运行态失败：{error}",
                refreshed_only=True,
                failure_stage=failure_stage,
            )
        reported, report_error = await self.__report(
            plugin_id,
            repo_url,
            enabled=report,
        )
        return PluginInstallResult(
            success=True,
            message=(
                "插件已存在，已刷新加载"
                if not report_error
                else "插件已存在，已刷新加载；安装上报失败，不影响本地刷新"
            ),
            refreshed_only=True,
            runtime_reloaded=True,
            registrations_refreshed=True,
            reported=reported,
            report_error=report_error,
        )

    async def __reload_active(self, plugin_id: str) -> None:
        """重载只有进入 ACTIVE 才能作为安装或刷新成功继续提交。"""
        runtime_status = await self.__await_side_effect(
            self.__target_reloader(plugin_id)
        )
        if runtime_status is not PluginRuntimeStatus.ACTIVE:
            raise RuntimeError("插件加载失败，请查看插件日志")

    async def __finish_committed(self, state: _InstallState) -> str:
        """幂等清理 COMMITTED 事务；失败时保留 journal 供启动回放。"""
        try:
            await self.__await_side_effect(
                self.__packages.async_finalize_persistent_backup(state.checkpoint)
            )
            await self.__await_side_effect(
                self.__packages.async_commit(state.checkpoint)
            )
            await self.__await_side_effect(
                self.__persistence.delete_installation(
                    state.transaction_id,
                    expected_phase=PluginInstallationPhase.COMMITTED,
                )
            )
        except Exception as error:  # noqa: BLE001 - 已提交终态不能反向回滚
            logger.warning(
                "插件安装事务 %s 已提交但清理未完成：%s",
                state.transaction_id,
                error,
            )
            return str(error)
        return ""

    async def __resolve_journal_created(
        self,
        state: _InstallState,
    ) -> bool | None:
        """PREPARED 创建确认异常时核对事务是否已经持久化。"""
        if state.journal_created:
            return True
        task = asyncio.create_task(
            self.__persistence.get_installation(state.transaction_id)
        )
        try:
            record = await await_task_to_terminal(task)
        except BaseException as error:
            state.journal_unknown = True
            logger.error(
                "插件安装事务 %s 无法确认 PREPARED 创建结果：%s",
                state.transaction_id,
                error,
            )
            return None
        if record is None:
            state.journal_unknown = False
            return False
        if record.phase is not PluginInstallationPhase.PREPARED:
            state.journal_unknown = True
            logger.error(
                "插件安装事务 %s 在 PREPARED 创建确认时阶段异常：%s",
                state.transaction_id,
                record.phase.value,
            )
            return None
        state.journal_created = True
        state.journal_unknown = False
        return True

    async def __resolve_commit_outcome(
        self,
        state: _InstallState,
    ) -> bool | None:
        """提交确认异常时读取 journal，拒绝猜测数据库最终状态。"""
        if state.committed:
            return True
        task = asyncio.create_task(
            self.__persistence.get_installation(state.transaction_id)
        )
        try:
            record = await await_task_to_terminal(task)
        except BaseException as error:  # 数据库仍不可用时只能留待启动恢复
            state.commit_unknown = True
            logger.error(
                "插件安装事务 %s 无法确认数据库提交结果：%s",
                state.transaction_id,
                error,
            )
            return None
        if record is None:
            state.commit_unknown = True
            logger.error(
                "插件安装事务 %s 在提交确认时已不存在",
                state.transaction_id,
            )
            return None
        if record.phase is PluginInstallationPhase.COMMITTED:
            state.committed = True
            state.commit_unknown = False
            return True
        state.commit_unknown = False
        return False

    async def __failure_result(
        self,
        *,
        plugin_id: str,
        state: _InstallState,
        stage: str,
        message: str,
    ) -> PluginInstallResult:
        """补偿 PREPARED 事务，并把失败阶段与恢复结果返回调用方。"""
        rollback = await self.__fail_prepared(plugin_id=plugin_id, state=state)
        return PluginInstallResult(
            success=False,
            message=message,
            package_installed=state.package_installed,
            failure_stage=stage,
            rollback=rollback,
        )

    async def __fail_prepared(
        self,
        *,
        plugin_id: str,
        state: _InstallState,
    ) -> PluginInstallRollback:
        """恢复数据库提交前的文件和运行态，成功后删除 PREPARED journal。"""
        errors: list[str] = []
        file_restored = False
        journal_deleted = False
        try:
            await self.__packages.async_restore(state.checkpoint)
            file_restored = True
        except Exception as error:  # noqa: BLE001 - 保留 journal 供下次启动重试
            errors.append(f"插件文件恢复失败：{error}")

        runtime_restored = False
        registrations_restored = False
        if file_restored and state.runtime_touched:
            try:
                await self.__rollback_reloader(plugin_id)
                runtime_restored = True
            except Exception as error:  # noqa: BLE001 - 返回完整补偿诊断
                errors.append(f"插件运行态恢复失败：{error}")
            if runtime_restored:
                try:
                    await self.__registration_refresher(plugin_id)
                    registrations_restored = True
                except Exception as error:  # noqa: BLE001
                    errors.append(f"插件注册恢复失败：{error}")

        rollback_complete = file_restored and (
            not state.runtime_touched
            or (runtime_restored and registrations_restored)
        )
        if rollback_complete and state.journal_created:
            try:
                await self.__persistence.delete_installation(
                    state.transaction_id,
                    expected_phase=PluginInstallationPhase.PREPARED,
                )
                journal_deleted = True
            except Exception as error:  # noqa: BLE001 - marker 让恢复可安全重试
                errors.append(f"插件安装事务删除失败：{error}")
        if journal_deleted:
            try:
                await self.__packages.async_cleanup(state.checkpoint)
            except Exception as error:  # noqa: BLE001 - orphan 不再影响业务终态
                errors.append(f"插件恢复材料清理失败：{error}")

        return PluginInstallRollback(
            file_attempted=state.checkpoint is not None,
            file_restored=file_restored,
            runtime_attempted=state.runtime_touched,
            runtime_restored=runtime_restored,
            registrations_attempted=(
                state.runtime_touched or state.registrations_touched
            ),
            registrations_restored=registrations_restored,
            journal_deleted=journal_deleted,
            errors=tuple(errors),
        )

    async def __rollback_without_journal(self, checkpoint: Any) -> PluginInstallRollback:
        """journal 创建失败时恢复文件并立即清理无主快照。"""
        errors: list[str] = []
        try:
            await self.__packages.async_restore(checkpoint)
        except Exception as error:  # noqa: BLE001
            return PluginInstallRollback(
                file_attempted=True,
                errors=(f"插件文件恢复失败：{error}",),
            )
        try:
            await self.__packages.async_cleanup(checkpoint)
        except Exception as error:  # noqa: BLE001 - 无 journal 的孤儿不影响旧载荷
            errors.append(f"插件恢复材料清理失败：{error}")
        return PluginInstallRollback(
            file_attempted=True,
            file_restored=True,
            errors=tuple(errors),
        )

    async def __rollback_cancelled(
        self,
        *,
        plugin_id: str,
        state: _InstallState,
    ) -> None:
        """保留取消语义，但不在补偿完成前释放插件生命周期 owner。"""
        if state.committed:
            logger.warning(
                "插件 %s 在数据库提交后被取消，安装终态将在启动时继续清理",
                plugin_id,
            )
            return
        if state.commit_unknown:
            logger.error(
                "插件 %s 在数据库提交结果未知时被取消，保留 journal 和当前载荷等待启动恢复",
                plugin_id,
            )
            return
        if state.journal_unknown:
            logger.error(
                "插件 %s 在 PREPARED 创建结果未知时被取消，保留恢复材料等待人工或启动核对",
                plugin_id,
            )
            return
        if state.checkpoint is None:
            return
        cleanup_task = asyncio.create_task(
            self.__fail_prepared(plugin_id=plugin_id, state=state)
            if state.journal_created
            else self.__rollback_without_journal(state.checkpoint)
        )
        try:
            rollback = await await_task_to_terminal(cleanup_task)
        except BaseException as error:
            logger.error("插件 %s 取消后的补偿失败：%s", plugin_id, error)
            return
        if rollback.errors:
            logger.error(
                "插件 %s 取消后的补偿存在错误：%s",
                plugin_id,
                "；".join(rollback.errors),
            )

    @staticmethod
    async def __await_side_effect(operation: Awaitable[T]) -> T:
        """让不可安全中断的副作用进入终态后再传播调用方取消。"""
        task = asyncio.ensure_future(operation)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as cancellation:
            try:
                await await_task_to_terminal(task)
            except BaseException as error:
                raise cancellation from error
            raise

    async def __report(
        self,
        plugin_id: str,
        repo_url: str | None,
        *,
        enabled: bool,
    ) -> tuple[bool, str]:
        """执行非关键远程上报，不改变本地安装终态。"""
        if not enabled:
            return False, ""
        try:
            result = await self.__install_reporter(plugin_id, repo_url)
            return result is not False, "" if result is not False else "安装上报未确认"
        except Exception as error:  # noqa: BLE001 - 远程上报不回滚本地安装
            return False, str(error)
