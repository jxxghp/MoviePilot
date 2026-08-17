"""插件安装应用用例。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Optional


InstalledPluginsReader = Callable[[], list[str]]
InstalledPluginsWriter = Callable[[list[str]], Awaitable[object]]
PluginIdsProvider = Callable[[], list[str]]
CompatibilityChecker = Callable[[str, str], Awaitable[Optional[str]]]
PackageInstaller = Callable[
    [str, str, Optional[str], bool],
    Awaitable[tuple[bool, str]],
]
PackageCheckpointer = Callable[[str], Awaitable[Any]]
PackageCheckpointAction = Callable[[Any], Awaitable[object]]
InstallReporter = Callable[[str, Optional[str]], Awaitable[object]]
PluginReloader = Callable[[str], Awaitable[object]]
PluginRegistrationRefresher = Callable[[str], Awaitable[object]]


@dataclass(frozen=True, slots=True)
class PluginInstallRollback:
    """描述失败安装中各类可补偿副作用的恢复结果。"""

    file_attempted: bool = False
    file_restored: bool = False
    installed_list_attempted: bool = False
    installed_list_restored: bool = False
    runtime_attempted: bool = False
    runtime_restored: bool = False
    registrations_attempted: bool = False
    registrations_restored: bool = False
    dependency_supported: bool = False
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
    failure_stage: Optional[str] = None
    checkpoint_cleanup_error: str = ""
    rollback: PluginInstallRollback = field(default_factory=PluginInstallRollback)


class PluginInstallCommand:
    """协调插件检查、包事务、持久化、运行态刷新和安装上报。"""

    def __init__(
        self,
        *,
        installed_plugins_reader: InstalledPluginsReader,
        installed_plugins_writer: InstalledPluginsWriter,
        plugin_ids_provider: PluginIdsProvider,
        compatibility_checker: CompatibilityChecker,
        package_installer: PackageInstaller,
        package_checkpointer: PackageCheckpointer,
        package_committer: PackageCheckpointAction,
        package_rollback: PackageCheckpointAction,
        install_reporter: InstallReporter,
        plugin_reloader: PluginReloader,
        registration_refresher: PluginRegistrationRefresher,
    ) -> None:
        """保存安装用例所需端口，不绑定数据库、网络或运行时实现。"""
        self._installed_plugins_reader = installed_plugins_reader
        self._installed_plugins_writer = installed_plugins_writer
        self._plugin_ids_provider = plugin_ids_provider
        self._compatibility_checker = compatibility_checker
        self._package_installer = package_installer
        self._package_checkpointer = package_checkpointer
        self._package_committer = package_committer
        self._package_rollback = package_rollback
        self._install_reporter = install_reporter
        self._plugin_reloader = plugin_reloader
        self._registration_refresher = registration_refresher

    async def execute(
        self,
        *,
        plugin_id: str,
        repo_url: Optional[str],
        release_version: Optional[str] = None,
        force: bool = False,
    ) -> PluginInstallResult:
        """执行插件安装，并在关键阶段失败时恢复可补偿状态。"""
        installed_plugins = list(self._installed_plugins_reader() or [])
        refreshed_only = not force and plugin_id in self._plugin_ids_provider()
        if refreshed_only:
            return await self._refresh_existing(
                plugin_id=plugin_id,
                repo_url=repo_url,
            )
        if not repo_url:
            return PluginInstallResult(
                success=False,
                message="没有传入仓库地址，无法正确安装插件，请检查配置",
                failure_stage="validation",
            )

        try:
            checkpoint = await self._package_checkpointer(plugin_id)
        except Exception as err:
            return PluginInstallResult(
                success=False,
                message=f"创建插件安装快照失败：{err}",
                failure_stage="package_checkpoint",
            )

        try:
            state, message = await self._package_installer(
                plugin_id,
                repo_url,
                release_version,
                force,
            )
        except Exception as err:
            return await self._failure(
                plugin_id=plugin_id,
                original_plugins=installed_plugins,
                checkpoint=checkpoint,
                stage="package_install",
                message=str(err),
                package_installed=False,
            )
        if not state:
            return await self._failure(
                plugin_id=plugin_id,
                original_plugins=installed_plugins,
                checkpoint=checkpoint,
                stage="package_install",
                message=message,
                package_installed=False,
            )

        installed_list_persisted = False
        if plugin_id not in installed_plugins:
            updated_plugins = [*installed_plugins, plugin_id]
            try:
                await self._installed_plugins_writer(updated_plugins)
                installed_list_persisted = True
            except Exception as err:
                return await self._failure(
                    plugin_id=plugin_id,
                    original_plugins=installed_plugins,
                    checkpoint=checkpoint,
                    stage="installed_list_persistence",
                    message=str(err),
                    package_installed=True,
                )

        try:
            await self._plugin_reloader(plugin_id)
        except Exception as err:
            return await self._failure(
                plugin_id=plugin_id,
                original_plugins=installed_plugins,
                checkpoint=checkpoint,
                stage="runtime_reload",
                message=str(err),
                package_installed=True,
                installed_list_persisted=installed_list_persisted,
                runtime_touched=True,
            )

        try:
            await self._registration_refresher(plugin_id)
        except Exception as err:
            return await self._failure(
                plugin_id=plugin_id,
                original_plugins=installed_plugins,
                checkpoint=checkpoint,
                stage="registration_refresh",
                message=str(err),
                package_installed=True,
                installed_list_persisted=installed_list_persisted,
                runtime_touched=True,
                registrations_touched=True,
            )

        checkpoint_cleanup_error = ""
        try:
            await self._package_committer(checkpoint)
        except Exception as err:
            checkpoint_cleanup_error = str(err)

        reported = False
        report_error = ""
        try:
            report_result = await self._install_reporter(plugin_id, repo_url)
            reported = report_result is not False
            if not reported:
                report_error = "安装上报未确认"
        except Exception as err:
            report_error = str(err)

        result_message = message or "插件安装成功"
        if checkpoint_cleanup_error:
            result_message = f"{result_message}；临时安装快照清理失败"
        if report_error:
            result_message = f"{result_message}；安装上报失败，不影响本地安装"
        return PluginInstallResult(
            success=True,
            message=result_message,
            package_installed=True,
            installed_list_persisted=installed_list_persisted,
            runtime_reloaded=True,
            registrations_refreshed=True,
            reported=reported,
            report_error=report_error,
            checkpoint_cleanup_error=checkpoint_cleanup_error,
        )

    async def _refresh_existing(
        self,
        *,
        plugin_id: str,
        repo_url: Optional[str],
    ) -> PluginInstallResult:
        """刷新已存在插件，不触碰包文件和已安装列表。"""
        if repo_url:
            compatible_message = await self._compatibility_checker(
                plugin_id,
                repo_url,
            )
            if compatible_message:
                return PluginInstallResult(
                    success=False,
                    message=compatible_message,
                    refreshed_only=True,
                    failure_stage="compatibility",
                )
        failure_stage = "runtime_reload"
        try:
            await self._plugin_reloader(plugin_id)
            failure_stage = "registration_refresh"
            await self._registration_refresher(plugin_id)
        except Exception as err:
            rollback_errors = []
            runtime_restored = False
            registrations_restored = False
            try:
                await self._plugin_reloader(plugin_id)
                runtime_restored = True
            except Exception as rollback_err:
                rollback_errors.append(f"运行态恢复失败：{rollback_err}")
            if runtime_restored:
                try:
                    await self._registration_refresher(plugin_id)
                    registrations_restored = True
                except Exception as rollback_err:
                    rollback_errors.append(f"路由和服务注册恢复失败：{rollback_err}")
            return PluginInstallResult(
                success=False,
                message=f"刷新插件运行态失败：{err}",
                refreshed_only=True,
                failure_stage=failure_stage,
                rollback=PluginInstallRollback(
                    runtime_attempted=True,
                    runtime_restored=runtime_restored,
                    registrations_attempted=True,
                    registrations_restored=registrations_restored,
                    errors=tuple(rollback_errors),
                ),
            )

        reported = False
        report_error = ""
        try:
            report_result = await self._install_reporter(plugin_id, repo_url)
            reported = report_result is not False
            if not reported:
                report_error = "安装上报未确认"
        except Exception as err:
            report_error = str(err)
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

    async def _failure(
        self,
        *,
        plugin_id: str,
        original_plugins: list[str],
        checkpoint: Any,
        stage: str,
        message: str,
        package_installed: bool,
        installed_list_persisted: bool = False,
        runtime_touched: bool = False,
        registrations_touched: bool = False,
    ) -> PluginInstallResult:
        """按持久化、文件、运行态顺序补偿失败安装并记录结果。"""
        errors = []
        installed_list_restored = False
        if installed_list_persisted:
            try:
                await self._installed_plugins_writer(list(original_plugins))
                installed_list_restored = True
            except Exception as err:
                errors.append(f"已安装列表恢复失败：{err}")

        file_restored = False
        try:
            await self._package_rollback(checkpoint)
            file_restored = True
        except Exception as err:
            errors.append(f"插件文件恢复失败：{err}")

        runtime_restored = False
        registrations_restored = False
        if runtime_touched:
            try:
                await self._plugin_reloader(plugin_id)
                runtime_restored = True
            except Exception as err:
                errors.append(f"插件运行态恢复失败：{err}")
        if runtime_restored:
            try:
                await self._registration_refresher(plugin_id)
                registrations_restored = True
            except Exception as err:
                errors.append(f"插件路由和服务注册恢复失败：{err}")

        rollback = PluginInstallRollback(
            file_attempted=True,
            file_restored=file_restored,
            installed_list_attempted=installed_list_persisted,
            installed_list_restored=installed_list_restored,
            runtime_attempted=runtime_touched,
            runtime_restored=runtime_restored,
            registrations_attempted=runtime_touched or registrations_touched,
            registrations_restored=registrations_restored,
            dependency_supported=False,
            errors=tuple(errors),
        )
        rollback_message = []
        rollback_message.append("插件文件已恢复" if file_restored else "插件文件恢复失败")
        if installed_list_persisted:
            rollback_message.append(
                "已安装列表已恢复"
                if installed_list_restored
                else "已安装列表恢复失败"
            )
        if runtime_touched:
            rollback_message.append(
                "旧运行态已恢复" if runtime_restored else "旧运行态恢复失败"
            )
            rollback_message.append(
                "旧路由和服务注册已恢复"
                if registrations_restored
                else "旧路由和服务注册恢复失败"
            )
        rollback_message.append("Python依赖变更不支持自动回滚")
        return PluginInstallResult(
            success=False,
            message=f"{message}；{'；'.join(rollback_message)}",
            package_installed=package_installed,
            installed_list_persisted=installed_list_persisted,
            failure_stage=stage,
            rollback=rollback,
        )
