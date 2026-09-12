"""插件依赖检查与安装运行时服务。"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, cast

from app.runtime.extensions.plugin.registry import PluginRegistry
from app.runtime.extensions.plugin.system import PluginSystemServices
from app.schemas.plugin import PluginInstance, PluginRuntimeStatus


@dataclass(frozen=True)
class PluginDependencyInstallResult:
    """记录插件依赖检查结果，区分无缺失、安装成功和安装失败。"""

    missing: list[str]
    success: bool


@dataclass(frozen=True)
class PluginDependencyClassification:
    """按当前源码和 Python 环境划分已安装插件。"""

    ready: tuple[str, ...]
    missing_dependencies: tuple[str, ...]
    missing_source: tuple[str, ...]


PluginInstanceDirectoryProvider = Callable[
    [str, Optional[PluginInstance]],
    Optional[Path],
]
PluginHostInstanceProvider = Callable[[str], Optional[PluginInstance]]


class PluginDependencyService:
    """执行缺失插件依赖的发现和安装，不参与插件生命周期。"""

    def __init__(
        self,
        *,
        system: Callable[[], PluginSystemServices],
        instances: Optional[Callable[[], dict[str, PluginInstance]]] = None,
        registry: Optional[PluginRegistry] = None,
        log: Any,
        instance_directory: Optional[PluginInstanceDirectoryProvider] = None,
        host_instance: Optional[PluginHostInstanceProvider] = None,
    ) -> None:
        """保存插件系统、虚拟实例和运行状态端口。

        ``instance_directory`` 是启动层按实际版本绑定解析的源码目录端口。提供
        后，启动分类会逐个检查本体和分身的目录，避免把同一源码插件的多版本依赖
        合并成一个 source 结论再复制给所有实例。
        """
        self._system = system
        self._instances = instances or (lambda: {})
        self._registry = registry
        self._logger = log
        self._instance_directory = instance_directory
        self._host_instance = host_instance or (lambda _plugin_id: None)

    def _begin_missing_install(self, missing: list[str]) -> Optional[float]:
        """统一无缺失短路、安装清单日志和耗时起点。"""
        if not missing:
            return None
        self._logger.debug(f"检测到缺失的依赖项: {missing}")
        self._logger.info(f"开始安装缺失的依赖项，共 {len(missing)} 个...")
        return time.time()

    def _complete_missing_install(
        self,
        missing: list[str],
        success: bool,
        started_at: float,
    ) -> PluginDependencyInstallResult:
        """统一安装结果、耗时和成功或失败日志分类。"""
        elapsed = time.time() - started_at
        if success:
            self._logger.info(
                f"已完成 {len(missing)} 个依赖项安装，总耗时：{elapsed:.2f} 秒"
            )
        else:
            self._logger.warning(
                f"存在缺失依赖项安装失败，请尝试手动安装，总耗时：{elapsed:.2f} 秒"
            )
        return PluginDependencyInstallResult(missing=missing, success=success)

    def install_missing_with_status(self) -> PluginDependencyInstallResult:
        """安装缺失依赖并返回安装器的明确结果。"""
        installer = self._system().dependency
        missing = installer.find_missing()
        started_at = self._begin_missing_install(missing)
        if started_at is None:
            return PluginDependencyInstallResult(missing=[], success=True)
        success, _message = installer.install(missing)
        return self._complete_missing_install(missing, success, started_at)

    def install_missing(self) -> list[str]:
        """安装当前环境缺失的插件依赖并保持历史列表返回合同。"""
        return self.install_missing_with_status().missing

    async def async_install_missing_with_status(self) -> PluginDependencyInstallResult:
        """在异步启动链中恢复缺失依赖，确保安装子进程可取消。"""
        installer = self._system().dependency
        missing = await installer.async_find_missing()
        started_at = self._begin_missing_install(missing)
        if started_at is None:
            return PluginDependencyInstallResult(missing=[], success=True)
        success, _message = await installer.async_install(missing)
        return self._complete_missing_install(missing, success, started_at)

    def classify_plugins(self) -> PluginDependencyClassification:
        """分类物理插件，并把源码结论映射到全部虚拟实例。"""
        installer = self._system().dependency
        ready, missing_dependencies, missing_source = installer.classify_plugins()
        ready = list(ready)
        missing_dependencies = list(missing_dependencies)
        missing_source = list(missing_source)

        if self._instance_directory is not None and hasattr(
            installer, "classify_plugin_directory"
        ):
            return self._classify_bound_instances(
                installer,
                self._instance_directory,
                ready,
                missing_dependencies,
                missing_source,
            )

        source_ready = set(ready)
        source_pending = set(missing_dependencies)
        for instance in self._instances().values():
            if instance.source_plugin_id in source_ready:
                ready.append(instance.instance_id)
            elif instance.source_plugin_id in source_pending:
                missing_dependencies.append(instance.instance_id)
            else:
                missing_source.append(instance.instance_id)
        return PluginDependencyClassification(
            ready=tuple(ready),
            missing_dependencies=tuple(missing_dependencies),
            missing_source=tuple(missing_source),
        )

    def _classify_bound_instances(
        self,
        installer: Any,
        instance_directory: PluginInstanceDirectoryProvider,
        ready: list[str],
        missing_dependencies: list[str],
        missing_source: list[str],
    ) -> PluginDependencyClassification:
        """按实例绑定的真实源码目录分别生成启动状态。"""
        instances = list(self._instances().values())
        source_ids = list(dict.fromkeys((*ready, *missing_dependencies, *missing_source)))
        source_ids.extend(
            instance.source_plugin_id
            for instance in instances
            if instance.source_plugin_id not in source_ids
        )
        ready_sources = {plugin_id.casefold() for plugin_id in ready}
        pending_sources = {
            plugin_id.casefold() for plugin_id in missing_dependencies
        }
        missing_sources = {plugin_id.casefold() for plugin_id in missing_source}
        result_ready: list[str] = []
        result_pending: list[str] = []
        result_missing: list[str] = []

        def classify(
            source_plugin_id: str,
            instance: Optional[PluginInstance],
        ) -> tuple[bool, bool]:
            """读取单个绑定目录的源码与依赖状态。"""
            try:
                directory = instance_directory(source_plugin_id, instance)
            except Exception as error:  # noqa: BLE001 - 状态分类必须失败关闭
                self._logger.error(
                    f"解析插件 {source_plugin_id} 的实例版本目录失败：{error}"
                )
                return False, False
            if directory is None:
                return False, False
            try:
                return cast(
                    tuple[bool, bool],
                    installer.classify_plugin_directory(directory),
                )
            except Exception as error:  # noqa: BLE001 - 状态分类必须失败关闭
                self._logger.error(
                    f"检查插件 {source_plugin_id} 的实例依赖失败：{error}"
                )
                return True, False

        for source_plugin_id in source_ids:
            source_key = source_plugin_id.casefold()
            host = self._host_instance(source_plugin_id)
            if source_key in missing_sources:
                host_exists, host_ready = False, False
            elif source_key in ready_sources or source_key in pending_sources:
                host_exists, host_ready = classify(source_plugin_id, host)
            else:
                host_exists, host_ready = classify(source_plugin_id, host)
            if not host_exists:
                result_missing.append(source_plugin_id)
            elif host_ready:
                result_ready.append(source_plugin_id)
            else:
                result_pending.append(source_plugin_id)

            for instance in instances:
                if instance.source_plugin_id.casefold() != source_key:
                    continue
                instance_exists, instance_ready = classify(source_plugin_id, instance)
                if not instance_exists:
                    result_missing.append(instance.instance_id)
                elif instance_ready:
                    result_ready.append(instance.instance_id)
                else:
                    result_pending.append(instance.instance_id)

        return PluginDependencyClassification(
            ready=tuple(result_ready),
            missing_dependencies=tuple(result_pending),
            missing_source=tuple(result_missing),
        )

    def apply_classification(
        self,
        classification: PluginDependencyClassification,
    ) -> None:
        """把依赖分类写入唯一注册表，已激活插件保持当前状态。"""
        if self._registry is None:
            raise RuntimeError("插件依赖状态注册表尚未装配")
        running_ids = set(self._registry.running_ids())
        for plugin_id in classification.missing_source:
            self._registry.set_runtime_status(
                plugin_id,
                PluginRuntimeStatus.SOURCE_MISSING,
            )
        for plugin_id in classification.missing_dependencies:
            self._registry.set_runtime_status(
                plugin_id,
                PluginRuntimeStatus.DEPENDENCY_PENDING,
            )
        for plugin_id in classification.ready:
            current_status = self._registry.runtime_status(plugin_id)
            if (
                plugin_id in running_ids
                and current_status is not PluginRuntimeStatus.DEPENDENCY_PENDING
            ):
                continue
            self._registry.set_runtime_status(
                plugin_id,
                PluginRuntimeStatus.READY,
            )
