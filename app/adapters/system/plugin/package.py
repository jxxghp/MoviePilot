"""插件包文件安装、快照恢复和分身处理适配器。"""

from __future__ import annotations

import asyncio
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.adapters.external.market import PluginHelper as _PluginHelper
from app.runtime.config import settings
from app.runtime.log import logger


@dataclass(frozen=True, slots=True)
class PluginPackageCheckpoint:
    """记录一次插件包变更前可用于补偿恢复的文件快照。"""

    plugin_id: str
    plugin_dir: Path
    transaction_dir: Path
    existed: bool


class PluginPackageManager:
    """隔离插件包安装、本地同步、分身改写和文件补偿能力。"""

    _COPY_IGNORE = ("__pycache__", "*.pyc", ".DS_Store", "node_modules")

    def __init__(self, helper: Optional[_PluginHelper] = None) -> None:
        """保存市场下载实现；文件事务由本适配器独立负责。"""
        self._helper = helper or _PluginHelper()

    @staticmethod
    def _plugin_dir(plugin_id: str) -> Path:
        """解析插件运行目录并拒绝越出宿主插件根目录的标识。"""
        plugins_root = (Path(settings.ROOT_PATH) / "app" / "plugins").resolve()
        plugin_dir = (plugins_root / plugin_id.lower()).resolve()
        if plugin_dir == plugins_root or not plugin_dir.is_relative_to(plugins_root):
            raise ValueError(f"非法插件ID：{plugin_id}")
        return plugin_dir

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

    def install(
        self,
        plugin_id: str,
        repo_url: str,
        package_version: Optional[str] = None,
        release_version: Optional[str] = None,
        force_install: bool = False,
    ) -> tuple[bool, str]:
        """同步安装插件包，下载过程继续复用既有市场兼容策略。"""
        return self._helper.install(
            pid=plugin_id,
            repo_url=repo_url,
            package_version=package_version,
            release_version=release_version,
            force_install=force_install,
        )

    async def async_install(
        self,
        plugin_id: str,
        repo_url: str,
        package_version: Optional[str] = None,
        release_version: Optional[str] = None,
        force_install: bool = False,
    ) -> tuple[bool, str]:
        """异步安装插件包，下载过程继续复用既有市场兼容策略。"""
        return await self._helper.async_install(
            pid=plugin_id,
            repo_url=repo_url,
            package_version=package_version,
            release_version=release_version,
            force_install=force_install,
        )

    def sync_local(self, plugin_id: str, source_dir: Path) -> bool:
        """用本地仓库内容原子替换运行副本，失败时恢复原目录。"""
        source_dir = source_dir.resolve()
        plugin_dir = self._plugin_dir(plugin_id)
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

    def clone(
        self,
        *,
        plugin_id: str,
        clone_id: str,
        original_class_name: str,
        suffix: str,
        name: str,
        description: str,
        version: Optional[str] = None,
        icon: Optional[str] = None,
    ) -> tuple[bool, str]:
        """复制并改写插件分身文件，任一步失败都删除不完整目标。"""
        original_dir = self._plugin_dir(plugin_id)
        clone_dir = self._plugin_dir(clone_id)
        if not original_dir.is_dir():
            return False, f"原插件目录 {original_dir} 不存在"
        if clone_dir.exists():
            return False, f"分身插件 {clone_id} 已存在"

        checkpoint = self.checkpoint(clone_id)
        try:
            shutil.copytree(original_dir, clone_dir)
            success, message = self._modify_plugin_files(
                plugin_dir=clone_dir,
                original_class_name=original_class_name,
                suffix=suffix,
                name=name,
                description=description,
                version=version,
                icon=icon,
            )
            if not success:
                self.rollback(checkpoint)
                return False, message
            self.commit(checkpoint)
            logger.info(f"已复制插件目录：{original_dir} -> {clone_dir}")
            return True, "文件修改成功"
        except Exception as err:
            try:
                self.rollback(checkpoint)
            except Exception as rollback_err:
                logger.error(
                    f"清理插件分身 {clone_id} 失败：{rollback_err}",
                    exc_info=True,
                )
            return False, f"创建插件分身文件失败：{err}"

    def _modify_plugin_files(
        self,
        *,
        plugin_dir: Path,
        original_class_name: str,
        suffix: str,
        name: str,
        description: str,
        version: Optional[str],
        icon: Optional[str],
    ) -> tuple[bool, str]:
        """改写分身的 Python 元数据和联邦前端资源。"""
        clone_class_name = f"{original_class_name}{suffix}"
        init_file = plugin_dir / "__init__.py"
        if init_file.exists():
            success, message = self._modify_python_file(
                file_path=init_file,
                original_class_name=original_class_name,
                clone_class_name=clone_class_name,
                name=name,
                description=description,
                version=version,
                icon=icon,
            )
            if not success:
                return False, message

        dist_dir = plugin_dir / "dist"
        if dist_dir.exists():
            success, message = self._modify_federation_files(
                dist_dir=dist_dir,
                original_class_name=original_class_name,
                clone_class_name=clone_class_name,
            )
            if not success:
                return False, message
        return True, "文件修改成功"

    @staticmethod
    def _modify_python_file(
        *,
        file_path: Path,
        original_class_name: str,
        clone_class_name: str,
        name: str,
        description: str,
        version: Optional[str],
        icon: Optional[str],
    ) -> tuple[bool, str]:
        """改写插件主类名称、展示元数据和独立配置前缀。"""
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
            content = content.replace(
                f"class {original_class_name}",
                f"class {clone_class_name}",
            )
            if name:
                content = re.sub(
                    r'plugin_name\s*=\s*["\'][^"\']*["\']',
                    f'plugin_name = "{name}"',
                    content,
                )
            if description:
                content = re.sub(
                    r'plugin_desc\s*=\s*["\'][^"\']*["\']',
                    f'plugin_desc = "{description}"',
                    content,
                )
            content = re.sub(
                r'plugin_config_prefix\s*=\s*["\'][^"\']*["\']',
                f'plugin_config_prefix = "{clone_class_name.lower()}_"',
                content,
            )
            if version:
                content = re.sub(
                    r'plugin_version\s*=\s*["\'][^"\']*["\']',
                    f'plugin_version = "{version}"',
                    content,
                )
            if icon and icon.strip():
                content = re.sub(
                    r'plugin_icon\s*=\s*["\'][^"\']*["\']',
                    f'plugin_icon = "{icon}"',
                    content,
                )
            if "def init_plugin(self" in content:
                init_index = content.index("def init_plugin(self")
                content = (
                    content[:init_index]
                    + "is_clone = True\n\n    "
                    + content[init_index:]
                )
            file_path.write_text(content, encoding="utf-8")
            return True, "Python文件修改成功"
        except Exception as err:
            logger.error(f"修改Python文件失败：{err}")
            return False, f"修改Python文件失败：{err}"

    def _modify_federation_files(
        self,
        *,
        dist_dir: Path,
        original_class_name: str,
        clone_class_name: str,
    ) -> tuple[bool, str]:
        """改写联邦构建产物中的插件类名和样式命名空间。"""
        try:
            for file_path in dist_dir.rglob("*"):
                if not file_path.is_file() or file_path.suffix not in {".js", ".css"}:
                    continue
                try:
                    content = file_path.read_text(encoding="utf-8", errors="replace")
                    if file_path.suffix == ".js":
                        content = content.replace(original_class_name, clone_class_name)
                        content = content.replace(
                            f'"{original_class_name}"',
                            f'"{clone_class_name}"',
                        )
                        content = content.replace(
                            f"'{original_class_name}'",
                            f"'{clone_class_name}'",
                        )
                        content = content.replace(
                            f"css__{original_class_name}__",
                            f"css__{clone_class_name}__",
                        )
                    content = content.replace(
                        original_class_name.lower(),
                        clone_class_name.lower(),
                    )
                    file_path.write_text(content, encoding="utf-8")
                except Exception as err:
                    logger.warning(f"修改联邦插件文件 {file_path} 失败：{err}")
            self._rename_federation_assets(
                dist_dir,
                original_class_name,
                clone_class_name,
            )
            return True, "联邦插件文件修改完成"
        except Exception as err:
            logger.error(f"修改联邦插件文件失败：{err}")
            return False, f"修改联邦插件文件失败：{err}"

    @staticmethod
    def _rename_federation_assets(
        dist_dir: Path,
        original_class_name: str,
        clone_class_name: str,
    ) -> None:
        """重命名包含原类名的顶层联邦资源，避免分身资源冲突。"""
        try:
            for file_path in dist_dir.glob("*"):
                if not file_path.is_file():
                    continue
                if original_class_name.lower() not in file_path.name.lower():
                    continue
                new_name = file_path.name.replace(
                    original_class_name.lower(),
                    clone_class_name.lower(),
                )
                new_path = file_path.parent / new_name
                if not new_path.exists():
                    file_path.rename(new_path)
        except Exception as err:
            logger.warning(f"重命名联邦插件资源文件失败：{err}")
