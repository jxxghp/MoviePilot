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


FolderChange = Callable[[PluginFolders], tuple[PluginFolderResult, PluginFolders]]
FolderAtomicWriter = Callable[[FolderChange], Awaitable[PluginFolderResult]]


class PluginFolderService:
    """集中管理插件文件夹快照和带准入的持久化变更。"""

    def __init__(
        self,
        *,
        read: FolderReader,
        write: FolderWriter,
        write_sync: FolderSyncWriter,
        mutation: FolderMutation,
        update: FolderAtomicWriter | None = None,
    ) -> None:
        """保存配置读写和插件运行态变更准入端口。"""
        self._read = read
        self._write = write
        self._write_sync = write_sync
        self._mutation = mutation
        self._update = update

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
        folder_name = folder_name.strip()
        if not folder_name:
            return PluginFolderResult(False, "文件夹名称不能为空")

        def change(folders: PluginFolders) -> tuple[PluginFolderResult, PluginFolders]:
            """只在名称尚未占用时向最新快照追加空文件夹。"""
            if folder_name in folders:
                return PluginFolderResult(False, f"文件夹 '{folder_name}' 已存在"), folders
            folders[folder_name] = []
            return PluginFolderResult(True, f"文件夹 '{folder_name}' 创建成功"), folders

        return await self._change(f"创建插件文件夹 {folder_name}", change)

    async def delete(self, folder_name: str) -> PluginFolderResult:
        """删除存在的文件夹并返回稳定业务结果。"""
        def change(folders: PluginFolders) -> tuple[PluginFolderResult, PluginFolders]:
            """只从最新快照移除目标文件夹。"""
            if folder_name not in folders:
                return PluginFolderResult(False, f"文件夹 '{folder_name}' 不存在"), folders
            del folders[folder_name]
            return PluginFolderResult(True, f"文件夹 '{folder_name}' 删除成功"), folders

        return await self._change(f"删除插件文件夹 {folder_name}", change)

    async def update_folder(
        self,
        folder_name: str,
        *,
        new_name: str | None = None,
        changes: dict[str, Any] | None = None,
    ) -> PluginFolderResult:
        """增量更新文件夹名称或展示配置，同时保留成员与未修改字段。"""
        normalized_name = new_name.strip() if new_name is not None else folder_name
        if not normalized_name:
            return PluginFolderResult(False, "文件夹名称不能为空")
        folder_changes = deepcopy(changes or {})

        def change(folders: PluginFolders) -> tuple[PluginFolderResult, PluginFolders]:
            """在最新快照中合并展示字段，并保持重命名前的字典位置。"""
            if folder_name not in folders:
                return PluginFolderResult(False, f"文件夹 '{folder_name}' 不存在"), folders
            if normalized_name != folder_name and normalized_name in folders:
                return PluginFolderResult(False, f"文件夹 '{normalized_name}' 已存在"), folders

            current = folders[folder_name]
            if folder_changes:
                current = (
                    {"plugins": list(current)}
                    if isinstance(current, list)
                    else deepcopy(current) if isinstance(current, dict) else {"plugins": []}
                )
                current.update(folder_changes)

            if normalized_name == folder_name:
                folders[folder_name] = current
            else:
                folders = {
                    (normalized_name if name == folder_name else name): (current if name == folder_name else value)
                    for name, value in folders.items()
                }
            return PluginFolderResult(True, f"文件夹 '{normalized_name}' 已更新"), folders

        return await self._change(f"更新插件文件夹 {folder_name}", change)

    async def update_plugins(
        self,
        folder_name: str,
        plugin_ids: list[str],
        expected_plugin_ids: list[str] | None = None,
    ) -> PluginFolderResult:
        """条件更新指定文件夹的插件顺序和成员，并保留展示配置。"""
        next_plugin_ids = list(plugin_ids)

        def change(folders: PluginFolders) -> tuple[PluginFolderResult, PluginFolders]:
            """基于最新成员列表检查预期快照并替换目标列表。"""
            if folder_name not in folders:
                return PluginFolderResult(False, f"文件夹 '{folder_name}' 不存在"), folders
            folder_data = folders[folder_name]
            current_plugin_ids = _folder_plugins(folder_data) or []
            if expected_plugin_ids is not None and current_plugin_ids != expected_plugin_ids:
                return PluginFolderResult(False, "插件文件夹已被其他请求修改，请重新读取后再试"), folders
            folders[folder_name] = _with_folder_plugins(folder_data, next_plugin_ids)
            return PluginFolderResult(True, f"文件夹 '{folder_name}' 中的插件已更新"), folders

        return await self._change(f"更新插件文件夹 {folder_name}", change)

    async def assign_plugin(self, folder_name: str, plugin_id: str) -> PluginFolderResult:
        """把一个插件原子迁移到目标文件夹，并从其他文件夹移除。"""
        def change(folders: PluginFolders) -> tuple[PluginFolderResult, PluginFolders]:
            """在同一最新快照内完成跨文件夹成员迁移。"""
            if folder_name not in folders:
                return PluginFolderResult(False, f"文件夹 '{folder_name}' 不存在"), folders

            for name, folder_data in folders.items():
                plugins = _folder_plugins(folder_data)
                if plugins is None:
                    if name == folder_name:
                        folders[name] = _with_folder_plugins(folder_data, [])
                    continue
                folders[name] = _with_folder_plugins(
                    folder_data,
                    [item for item in plugins if item != plugin_id],
                )

            target = folders[folder_name]
            target_plugins = list(_folder_plugins(target) or [])
            target_plugins.append(plugin_id)
            folders[folder_name] = _with_folder_plugins(target, target_plugins)
            return PluginFolderResult(True, f"插件已移动到文件夹 '{folder_name}'"), folders

        return await self._change(f"移动插件到文件夹 {folder_name}", change)

    async def remove_plugin_from_folder(
        self,
        folder_name: str,
        plugin_id: str,
    ) -> PluginFolderResult:
        """只从指定文件夹移除一个插件，不影响其他文件夹。"""
        def change(folders: PluginFolders) -> tuple[PluginFolderResult, PluginFolders]:
            """在最新快照内删除目标文件夹中的指定成员。"""
            if folder_name not in folders:
                return PluginFolderResult(False, f"文件夹 '{folder_name}' 不存在"), folders
            folder_data = folders[folder_name]
            plugins = _folder_plugins(folder_data) or []
            if plugin_id not in plugins:
                return PluginFolderResult(False, f"插件不在文件夹 '{folder_name}' 中"), folders
            folders[folder_name] = _with_folder_plugins(
                folder_data,
                [item for item in plugins if item != plugin_id],
            )
            return PluginFolderResult(True, f"插件已从文件夹 '{folder_name}' 移除"), folders

        return await self._change(f"从文件夹 {folder_name} 移除插件", change)

    async def _change(
        self,
        operation: str,
        change: FolderChange,
    ) -> PluginFolderResult:
        """统一执行带运行时准入的增量文件夹写入并映射稳定失败结果。"""
        try:
            with self._mutation(operation):
                if self._update is not None:
                    return await self._update(change)
                folders = self.get()
                result, changed_folders = change(folders)
                if result.success:
                    await self._write(changed_folders)
                return result
        except PersistenceUnavailableError:
            raise
        except PluginMutationRejectedError as error:
            return PluginFolderResult(False, str(error))
        except Exception as error:  # noqa: BLE001 - HTTP 兼容入口以业务结果表达失败
            logger.error(f"[文件夹API] {operation}失败: {error}")
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
        update=lambda change: get_configured_system_config().async_update_atomically(
            SystemConfigKey.PluginFolders,
            lambda current: change(deepcopy(current) if isinstance(current, dict) else {}),
        ),
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


def _with_folder_plugins(folder_data: Any, plugin_ids: list[str]) -> Any:
    """替换成员列表，同时保留对象格式中的展示配置。"""
    if isinstance(folder_data, dict):
        return {**folder_data, "plugins": list(plugin_ids)}
    return list(plugin_ids)
