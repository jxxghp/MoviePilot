"""插件依赖检查与安装运行时服务。"""

import time
from collections.abc import Callable
from typing import Any

from app.runtime.extensions.plugin.system import PluginSystemServices


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

    def install_missing(self) -> list[str]:
        """安装当前环境缺失的插件依赖并返回检查到的依赖名。"""
        installer = self._system().dependency
        missing = installer.find_missing()
        if not missing:
            return missing
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
        return missing
