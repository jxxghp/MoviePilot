"""插件文件夹查询、变更和插件归属维护用例。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from app.application.configuration import get_configured_system_config
from app.application.plugin.runtime import get_plugin_manager
from app.runtime.log import logger
from app.schemas.exception import (
    PersistenceUnavailableError,
    PluginMutationRejectedError,
)
from app.schemas.types import SystemConfigKey

PluginFolders = dict[str, Any]
FolderReader = Callable[[], PluginFolders]
FolderWriter = Callable[[PluginFolders], Awaitable[object]]
FolderSyncWriter = Callable[[PluginFolders], object]
FolderMutation = Callable[[str], AbstractContextManager[None]]


@dataclass(frozen=True, slots=True)
class PluginFolderResult:
    """描述一次插件文件夹变更结果和用户提示。"""

    success: bool
    message: str = ""


class PluginFolderService:
    """集中管理插件文件夹快照和带准入的持久化变更。"""

    def __init__(
        self,
        *,
        read: FolderReader,
        write: FolderWriter,
        write_sync: FolderSyncWriter,
        mutation: FolderMutation,
    ) -> None:
        """保存配置读写和插件运行态变更准入端口。"""
        self._read = read
        self._write = write
        self._write_sync = write_sync
        self._mutation = mutation

    def get(self) -> PluginFolders:
        """返回与配置存储隔离的当前文件夹快照。"""
        return deepcopy(self._read())

    def get_or_empty(self) -> PluginFolders:
        """读取失败时记录诊断并返回兼容的空文件夹快照。"""
        try:
            return self.get()
        except Exception as error:  # noqa: BLE001 - 查询入口保留旧空结果语义
            logger.error(f"[文件夹API] 获取文件夹配置失败: {error}")
            return {}

    async def save(self, folders: PluginFolders) -> PluginFolderResult:
        """完整替换文件夹配置并取得插件变更准入。"""
        try:
            with self._mutation("保存插件文件夹配置"):
                await self._write(deepcopy(folders))
            return PluginFolderResult(True)
        except PersistenceUnavailableError:
            raise
        except PluginMutationRejectedError as error:
            return PluginFolderResult(False, str(error))
        except Exception as error:  # noqa: BLE001 - 兼容入口以结果表达保存失败
            logger.error(f"[文件夹API] 保存文件夹配置失败: {error}")
            return PluginFolderResult(False, str(error))

    async def create(self, folder_name: str) -> PluginFolderResult:
        """创建不存在的文件夹，保留旧列表格式的兼容形态。"""
        try:
            with self._mutation(f"创建插件文件夹 {folder_name}"):
                folders = self.get()
                if folder_name in folders:
                    return PluginFolderResult(False, f"文件夹 '{folder_name}' 已存在")
                folders[folder_name] = []
                await self._write(folders)
            return PluginFolderResult(True, f"文件夹 '{folder_name}' 创建成功")
        except PluginMutationRejectedError as error:
            return PluginFolderResult(False, str(error))

    async def delete(self, folder_name: str) -> PluginFolderResult:
        """删除存在的文件夹并返回稳定业务结果。"""
        try:
            with self._mutation(f"删除插件文件夹 {folder_name}"):
                folders = self.get()
                if folder_name not in folders:
                    return PluginFolderResult(False, f"文件夹 '{folder_name}' 不存在")
                del folders[folder_name]
                await self._write(folders)
            return PluginFolderResult(True, f"文件夹 '{folder_name}' 删除成功")
        except PluginMutationRejectedError as error:
            return PluginFolderResult(False, str(error))

    async def update_plugins(
        self, folder_name: str, plugin_ids: list[str]
    ) -> PluginFolderResult:
        """更新指定文件夹的插件顺序和成员。"""
        try:
            with self._mutation(f"更新插件文件夹 {folder_name}"):
                folders = self.get()
                folders[folder_name] = list(plugin_ids)
                await self._write(folders)
            return PluginFolderResult(
                True,
                f"文件夹 '{folder_name}' 中的插件已更新",
            )
        except PluginMutationRejectedError as error:
            return PluginFolderResult(False, str(error))

    def remove_plugin(self, plugin_id: str) -> None:
        """从当前和旧版文件夹形态中移除插件且不阻断卸载。"""
        try:
            folders = self.get()
            modified = False
            for folder_name, folder_data in folders.items():
                plugins = _folder_plugins(folder_data)
                if plugins is None or plugin_id not in plugins:
                    continue
                plugins.remove(plugin_id)
                logger.info(f"已从文件夹 '{folder_name}' 中移除插件 {plugin_id}")
                modified = True
            if modified:
                self._write_sync(folders)
            else:
                logger.debug(f"插件 {plugin_id} 不在任何文件夹中，无需移除")
        except Exception as error:  # noqa: BLE001
            # 文件夹配置损坏不应阻断插件代码、数据和定时任务的卸载流程。
            logger.error(f"从文件夹中移除插件时出错：{error}")

    def add_clone(self, original_plugin_id: str, clone_plugin_id: str) -> None:
        """让分身继承原插件所在的首个文件夹且不阻断创建。"""
        try:
            folders = self.get()
            for folder_name, folder_data in folders.items():
                plugins = _folder_plugins(folder_data)
                if plugins is None or original_plugin_id not in plugins:
                    continue
                if clone_plugin_id not in plugins:
                    plugins.append(clone_plugin_id)
                    self._write_sync(folders)
                    logger.info(
                        f"已将分身插件 {clone_plugin_id} 添加到文件夹 "
                        f"'{folder_name}' 中"
                    )
                return
            logger.info(
                f"原插件 {original_plugin_id} 不在任何文件夹中，"
                f"分身插件 {clone_plugin_id} 将保持独立"
            )
        except Exception as error:  # noqa: BLE001
            # 文件夹处理失败不影响插件分身创建的整体流程。
            logger.error(f"处理插件文件夹时出错：{error}")


def get_plugin_folder_service() -> PluginFolderService:
    """基于当前 lifespan 的 Application 配置和插件 Runtime 构造文件夹服务。"""
    return PluginFolderService(
        read=lambda: (
            get_configured_system_config().get(SystemConfigKey.PluginFolders) or {}
        ),
        write=lambda folders: get_configured_system_config().async_set(
            SystemConfigKey.PluginFolders, folders
        ),
        write_sync=lambda folders: get_configured_system_config().set(
            SystemConfigKey.PluginFolders, folders
        ),
        mutation=lambda operation: get_plugin_manager().mutation(operation),
    )


def remove_plugin_from_folders(plugin_id: str) -> None:
    """保留卸载调用方使用的稳定 Application 入口。"""
    get_plugin_folder_service().remove_plugin(plugin_id)


def add_clone_to_plugin_folder(original_plugin_id: str, clone_plugin_id: str) -> None:
    """保留分身创建调用方使用的稳定 Application 入口。"""
    get_plugin_folder_service().add_clone(original_plugin_id, clone_plugin_id)


def _folder_plugins(folder_data: Any) -> list[str] | None:
    """返回当前字典形态或旧列表形态中的可变插件列表。"""
    if isinstance(folder_data, dict):
        plugins = folder_data.get("plugins")
        return plugins if isinstance(plugins, list) else None
    return folder_data if isinstance(folder_data, list) else None
