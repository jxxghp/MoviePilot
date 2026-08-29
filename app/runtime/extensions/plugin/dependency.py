"""插件依赖检查与安装运行时服务。"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional

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


class PluginDependencyService:
    """执行缺失插件依赖的发现和安装，不参与插件生命周期。"""

    def __init__(
        self,
        *,
        system: Callable[[], PluginSystemServices],
        instances: Optional[Callable[[], dict[str, PluginInstance]]] = None,
        registry: Optional[PluginRegistry] = None,
        log: Any,
    ) -> None:
        """保存插件系统、虚拟实例和运行状态端口。"""
        self._system = system
        self._instances = instances or (lambda: {})
        self._registry = registry
        self._logger = log

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
        ready, missing_dependencies, missing_source = (
            self._system().dependency.classify_plugins()
        )
        ready = list(ready)
        missing_dependencies = list(missing_dependencies)
        missing_source = list(missing_source)
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
