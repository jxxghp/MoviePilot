"""插件依赖检查与安装结果的运行时数据结构。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PluginDependencyInstallResult:
    """记录插件依赖检查结果，区分无缺失、安装成功和安装失败。"""

    missing: list[str]  # 本轮检查到的缺失依赖项
    success: bool  # 缺失依赖是否已全部安装成功


@dataclass(frozen=True, slots=True)
class PluginDependencyClassification:
    """按当前源码和 Python 环境划分已安装插件。"""

    ready: tuple[str, ...]  # 源码和依赖都就绪，可以立即加载
    missing_dependencies: tuple[str, ...]  # 源码就绪但依赖尚未满足
    missing_source: tuple[str, ...]  # 运行目录下没有源码
