"""插件依赖检查与安装运行时服务。"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.runtime.extensions.plugin.system import PluginSystemServices


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
        log: Any,
    ) -> None:
        """保存插件系统适配器和日志端口。"""
        self._system = system
        self._logger = log

    def install_missing_with_status(self) -> PluginDependencyInstallResult:
        """安装缺失依赖并返回安装器的明确结果。"""
        installer = self._system().dependency
        missing = installer.find_missing()
        if not missing:
            return PluginDependencyInstallResult(missing=[], success=True)
        self._logger.debug(f"检测到缺失的依赖项: {missing}")
        self._logger.info(f"开始安装缺失的依赖项，共 {len(missing)} 个...")
        started = time.time()
        success, _message = installer.install(missing)
        elapsed = time.time() - started
        if success:
            self._logger.info(
                f"已完成 {len(missing)} 个依赖项安装，总耗时：{elapsed:.2f} 秒"
            )
        else:
            self._logger.warning(
                f"存在缺失依赖项安装失败，请尝试手动安装，总耗时：{elapsed:.2f} 秒"
            )
        return PluginDependencyInstallResult(missing=missing, success=success)

    def install_missing(self) -> list[str]:
        """安装当前环境缺失的插件依赖并保持历史列表返回合同。"""
        return self.install_missing_with_status().missing

    async def async_install_missing_with_status(self) -> PluginDependencyInstallResult:
        """在异步启动链中恢复缺失依赖，确保安装子进程可取消。"""
        installer = self._system().dependency
        missing = await installer.async_find_missing()
        if not missing:
            return PluginDependencyInstallResult(missing=[], success=True)
        self._logger.debug(f"检测到缺失的依赖项: {missing}")
        self._logger.info(f"开始安装缺失的依赖项，共 {len(missing)} 个...")
        started = time.time()
        success, _message = await installer.async_install(missing)
        elapsed = time.time() - started
        if success:
            self._logger.info(
                f"已完成 {len(missing)} 个依赖项安装，总耗时：{elapsed:.2f} 秒"
            )
        else:
            self._logger.warning(
                f"存在缺失依赖项安装失败，请尝试手动安装，总耗时：{elapsed:.2f} 秒"
            )
        return PluginDependencyInstallResult(missing=missing, success=success)

    def classify_plugins(self) -> PluginDependencyClassification:
        """返回启动编排使用的轻量插件分类。"""
        ready, missing_dependencies, missing_source = (
            self._system().dependency.classify_plugins()
        )
        return PluginDependencyClassification(
            ready=tuple(ready),
            missing_dependencies=tuple(missing_dependencies),
            missing_source=tuple(missing_source),
        )
