"""插件包文件安装、快照恢复和目录删除适配器。"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.adapters.external.market import PluginHelper as _PluginHelper
from app.runtime.config import settings
from app.runtime.log import logger

# 插件已装版本元信息文件名，位于插件目录下，源码下沉到版本目录时原地保留
PLUGIN_VERSIONS_MANIFEST_NAME = "versions.json"


@dataclass(frozen=True, slots=True)
class PluginPackageCheckpoint:
    """记录一次插件包变更前可用于补偿恢复的文件快照。"""

    plugin_id: str
    plugin_dir: Path
    transaction_dir: Path
    existed: bool


class PluginPackageManager:
    """隔离插件包安装、本地同步、目录删除和文件补偿能力。"""

    _COPY_IGNORE = ("__pycache__", "*.pyc", ".DS_Store", "node_modules")

    def __init__(self, helper: Optional[_PluginHelper] = None) -> None:
        """保存市场下载实现；文件事务由本适配器独立负责。"""
        self._helper = helper or _PluginHelper()

    @staticmethod
    def _plugin_dir(plugin_id: str, version_dir: Optional[str] = None) -> Path:
        """解析插件运行目录并拒绝越出宿主插件根目录的标识。

        :param plugin_id: 插件ID
        :param version_dir: 版本目录名，给定时返回插件目录下的该版本目录
        :return: 插件目录或其版本目录的绝对路径
        :raises ValueError: 插件ID或版本目录名越出宿主插件根目录
        """
        plugins_root = (Path(settings.ROOT_PATH) / "app" / "plugins").resolve()
        plugin_dir = (plugins_root / plugin_id.lower()).resolve()
        if plugin_dir == plugins_root or not plugin_dir.is_relative_to(plugins_root):
            raise ValueError(f"非法插件ID：{plugin_id}")
        if version_dir is None:
            return plugin_dir
        target = (plugin_dir / version_dir).resolve()
        if target == plugin_dir or target.parent != plugin_dir:
            raise ValueError(f"非法插件版本目录：{version_dir}")
        return target

    def checkpoint(self, plugin_id: str) -> PluginPackageCheckpoint:
        """在包变更前创建独立快照，供后续提交或补偿恢复。"""
        plugin_dir = self._plugin_dir(plugin_id)
        transaction_dir = (
            Path(settings.TEMP_PATH)
            / "plugin_transactions"
            / f"{plugin_id.lower()}-{uuid.uuid4().hex}"
        )
        existed = plugin_dir.exists()
        try:
            transaction_dir.mkdir(parents=True, exist_ok=False)
            if existed:
                shutil.copytree(plugin_dir, transaction_dir / "package")
        except Exception:
            shutil.rmtree(transaction_dir, ignore_errors=True)
            raise
        return PluginPackageCheckpoint(
            plugin_id=plugin_id,
            plugin_dir=plugin_dir,
            transaction_dir=transaction_dir,
            existed=existed,
        )

    async def async_checkpoint(self, plugin_id: str) -> PluginPackageCheckpoint:
        """在线程池中创建插件包文件快照。"""
        return await asyncio.to_thread(self.checkpoint, plugin_id)

    @staticmethod
    def commit(checkpoint: PluginPackageCheckpoint) -> None:
        """确认包变更成功并清理临时快照。"""
        shutil.rmtree(checkpoint.transaction_dir, ignore_errors=False)

    async def async_commit(self, checkpoint: PluginPackageCheckpoint) -> None:
        """在线程池中清理已提交的插件包快照。"""
        await asyncio.to_thread(self.commit, checkpoint)

    @staticmethod
    def rollback(checkpoint: PluginPackageCheckpoint) -> None:
        """删除当前包并把变更前文件快照恢复到运行目录。"""
        if checkpoint.plugin_dir.exists():
            shutil.rmtree(checkpoint.plugin_dir)
        snapshot_dir = checkpoint.transaction_dir / "package"
        if checkpoint.existed:
            if not snapshot_dir.is_dir():
                raise FileNotFoundError(
                    f"插件 {checkpoint.plugin_id} 的补偿快照不存在：{snapshot_dir}"
                )
            shutil.copytree(snapshot_dir, checkpoint.plugin_dir)
        shutil.rmtree(checkpoint.transaction_dir, ignore_errors=False)

    async def async_rollback(self, checkpoint: PluginPackageCheckpoint) -> None:
        """在线程池中恢复插件包文件快照。"""
        await asyncio.to_thread(self.rollback, checkpoint)

    @classmethod
    def _promote_to_version_dir(cls, plugin_id: str, version_dir: str) -> None:
        """把落在插件目录下的平铺源码搬进指定版本目录。

        市场下载按仓库树结构写入插件目录本身，没有版本层概念；调用方给出目标
        版本目录后由本方法完成下沉。市场安装会整体替换插件目录，因此下沉时目录
        下除元信息文件外只有本次下载的内容。

        :param plugin_id: 插件ID
        :param version_dir: 目标版本目录名
        """
        plugin_dir = cls._plugin_dir(plugin_id)
        target = cls._plugin_dir(plugin_id, version_dir)
        if not (plugin_dir / "__init__.py").is_file():
            return
        staging = plugin_dir.parent / f"{plugin_dir.name}-{version_dir}-{uuid.uuid4().hex}"
        staging.mkdir(parents=True, exist_ok=False)
        try:
            for entry in list(plugin_dir.iterdir()):
                if entry.name == PLUGIN_VERSIONS_MANIFEST_NAME or entry == target:
                    continue
                entry.rename(staging / entry.name)
            if target.exists():
                shutil.rmtree(target)
            staging.rename(target)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def install(
        self,
        plugin_id: str,
        repo_url: str,
        package_version: Optional[str] = None,
        release_version: Optional[str] = None,
        force_install: bool = False,
        version_dir: Optional[str] = None,
    ) -> tuple[bool, str]:
        """同步安装插件包，下载过程继续复用既有市场兼容策略。

        :param plugin_id: 插件ID
        :param repo_url: 插件仓库地址
        :param package_version: 首选插件包版本
        :param release_version: 指定安装的 Release 资产版本
        :param force_install: 是否强制安装
        :param version_dir: 目标版本目录名，给定时把下载内容下沉到该目录
        :return: (是否成功, 说明信息)
        """
        state, message = self._helper.install(
            pid=plugin_id,
            repo_url=repo_url,
            package_version=package_version,
            release_version=release_version,
            force_install=force_install,
        )
        if state and version_dir:
            try:
                self._promote_to_version_dir(plugin_id, version_dir)
            except Exception as err:
                logger.error(f"插件 {plugin_id} 源码下沉到版本目录 {version_dir} 失败：{err}")
                return False, f"源码下沉到版本目录失败：{err}"
        return state, message

    async def async_install(
        self,
        plugin_id: str,
        repo_url: str,
        package_version: Optional[str] = None,
        release_version: Optional[str] = None,
        force_install: bool = False,
        version_dir: Optional[str] = None,
    ) -> tuple[bool, str]:
        """异步安装插件包，下载过程继续复用既有市场兼容策略。

        :param plugin_id: 插件ID
        :param repo_url: 插件仓库地址
        :param package_version: 首选插件包版本
        :param release_version: 指定安装的 Release 资产版本
        :param force_install: 是否强制安装
        :param version_dir: 目标版本目录名，给定时把下载内容下沉到该目录
        :return: (是否成功, 说明信息)
        """
        state, message = await self._helper.async_install(
            pid=plugin_id,
            repo_url=repo_url,
            package_version=package_version,
            release_version=release_version,
            force_install=force_install,
        )
        if state and version_dir:
            try:
                await asyncio.to_thread(self._promote_to_version_dir, plugin_id, version_dir)
            except Exception as err:
                logger.error(f"插件 {plugin_id} 源码下沉到版本目录 {version_dir} 失败：{err}")
                return False, f"源码下沉到版本目录失败：{err}"
        return state, message

    def sync_local(
        self,
        plugin_id: str,
        source_dir: Path,
        version_dir: Optional[str] = None,
    ) -> bool:
        """用本地仓库内容原子替换运行副本，失败时恢复原目录。

        :param plugin_id: 插件ID
        :param source_dir: 本地仓库中的插件源码目录
        :param version_dir: 目标版本目录名，给定时复制到插件目录下的该版本目录
        :return: 是否同步成功
        """
        source_dir = source_dir.resolve()
        plugin_dir = self._plugin_dir(plugin_id, version_dir)
        if source_dir == plugin_dir:
            return True
        checkpoint = self.checkpoint(plugin_id)
        try:
            if plugin_dir.exists():
                shutil.rmtree(plugin_dir)
            shutil.copytree(
                source_dir,
                plugin_dir,
                ignore=shutil.ignore_patterns(*self._COPY_IGNORE),
            )
            self.commit(checkpoint)
            return True
        except Exception as err:
            logger.error(f"同步本地插件 {plugin_id} 失败：{err}")
            try:
                self.rollback(checkpoint)
            except Exception as rollback_err:
                logger.error(
                    f"恢复本地插件 {plugin_id} 原目录失败：{rollback_err}",
                    exc_info=True,
                )
            return False

    def remove(self, plugin_id: str) -> tuple[bool, str]:
        """删除插件源码目录，不可逆操作。

        :param plugin_id: 插件ID
        :return: (是否成功, 说明信息)
        """
        try:
            plugin_dir = self._plugin_dir(plugin_id)
        except ValueError as err:
            return False, str(err)
        if not plugin_dir.exists():
            return True, f"插件目录 {plugin_dir} 不存在，无需删除"
        try:
            shutil.rmtree(plugin_dir)
            return True, "插件目录删除成功"
        except Exception as err:
            logger.error(f"删除插件目录 {plugin_dir} 失败：{err}")
            return False, f"删除插件目录失败：{err}"
