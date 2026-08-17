"""插件市场查询客户端。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from app.adapters.external.market import PluginHelper as _PluginHelper
from app.runtime.cache import async_fresh, fresh


class PluginMarketClient:
    """把插件市场、版本元数据和本地仓库查询隔离为只读客户端。"""

    def __init__(self, helper: Optional[_PluginHelper] = None) -> None:
        """复用旧 PluginHelper 实现，保持缓存和弱单例身份不变。"""
        self._helper = helper or _PluginHelper()

    def get_plugins(
        self,
        repo_url: str,
        package_version: Optional[str] = None,
        force: bool = False,
    ) -> Optional[dict[str, dict]]:
        """同步读取指定仓库和代际的插件索引。"""
        with fresh(force):
            return self._helper.get_plugins(repo_url, package_version)

    async def async_get_plugins(
        self,
        repo_url: str,
        package_version: Optional[str] = None,
        force: bool = False,
    ) -> Optional[dict[str, dict]]:
        """异步读取指定仓库和代际的插件索引。"""
        async with async_fresh(force):
            return await self._helper.async_get_plugins(repo_url, package_version)

    def get_local_candidates(self) -> dict[str, dict]:
        """返回全部本地插件仓库候选。"""
        return self._helper.get_local_plugin_candidates()

    def get_local_candidate(
        self,
        plugin_id: str,
        package_version: Optional[str] = None,
        repo_path: Optional[Path] = None,
        **kwargs: Any,
    ) -> Optional[dict]:
        """返回指定插件的本地仓库候选。"""
        return self._helper.get_local_plugin_candidate(
            pid=plugin_id,
            package_version=package_version,
            repo_path=repo_path,
            **kwargs,
        )

    @staticmethod
    def get_local_repo_paths() -> list[Path]:
        """返回配置中有效的本地插件仓库目录。"""
        return _PluginHelper.get_local_repo_paths()

    @staticmethod
    def make_local_repo_url(
        plugin_id: str,
        repo_path: Optional[object] = None,
        package_version: Optional[str] = None,
    ) -> str:
        """生成兼容旧入口的本地插件来源标识。"""
        return _PluginHelper.make_local_repo_url(
            plugin_id,
            repo_path,
            package_version,
        )

    @staticmethod
    def is_local_repo_url(repo_url: Optional[str]) -> bool:
        """判断插件来源是否为本地仓库标识。"""
        return _PluginHelper.is_local_repo_url(repo_url)

    @staticmethod
    def annotate_system_version(plugin_info: dict) -> dict:
        """补充插件所需 MoviePilot 版本兼容状态。"""
        return _PluginHelper.annotate_plugin_system_version(plugin_info)

    @staticmethod
    def is_package_compatible(
        plugin_info: dict,
        package_version: Optional[str],
    ) -> bool:
        """判断插件条目是否兼容目标插件包代际。"""
        return _PluginHelper.is_package_plugin_compatible(
            plugin_info,
            package_version,
        )
