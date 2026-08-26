"""插件包文件安装、快照恢复和分身处理适配器。"""

from __future__ import annotations

import hashlib
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, cast

from app.adapters.external.market import PluginHelper as _PluginHelper
from app.adapters.system.host import SystemUtils
from app.runtime.execution import (
    run_in_threadpool_to_completion as _await_thread_operation,
)
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting

@dataclass(frozen=True, slots=True)
class PluginPackageCheckpoint:
    """记录运行目录快照及待提升的容器恢复备份。"""

    plugin_id: str
    plugin_dir: Path
    persistent_backup_dir: Path
    backup_staging_dir: Path | None
    backup_previous_dir: Path | None
    transaction_dir: Path
    plugin_existed: bool
    persistent_backup_existed: bool

    @property
    def existed(self) -> bool:
        """保留旧调用方读取运行目录存在状态的兼容属性。"""
        return self.plugin_existed

    @property
    def rollback_marker(self) -> Path:
        """返回文件补偿完成标记，供 PREPARED 重放保持幂等。"""
        return self.transaction_dir / ".rollback-complete"


class PluginPackageManager:
    """隔离插件包安装、本地同步、分身改写和文件补偿能力。"""

    _COPY_IGNORE = ("__pycache__", "*.pyc", ".DS_Store", "node_modules")

    def __init__(self, helper: Optional[_PluginHelper] = None) -> None:
        """保存市场下载实现；文件事务由本适配器独立负责。"""
        self._helper = helper or _PluginHelper()

    @staticmethod
    def __plugin_dir(plugin_id: str) -> Path:
        """解析插件运行目录并拒绝越出宿主插件根目录的标识。"""
        plugins_root = (
            Path(get_runtime_setting('ROOT_PATH')) / "app" / "plugins"
        ).resolve()
        plugin_dir = (plugins_root / plugin_id.lower()).resolve()
        if plugin_dir == plugins_root or not plugin_dir.is_relative_to(plugins_root):
            raise ValueError(f"非法插件ID：{plugin_id}")
        return plugin_dir

    def checkpoint(
        self,
        plugin_id: str,
        transaction_id: Optional[str] = None,
    ) -> PluginPackageCheckpoint:
        """在包变更前保存运行目录；持久事务使用配置目录承载恢复材料。"""
        plugin_dir = self.__plugin_dir(plugin_id)
        durable = transaction_id is not None
        persistent_backup_dir = (
            Path(get_runtime_setting('CONFIG_PATH'))
            / "plugins_backup"
            / plugin_id.lower()
        ).resolve()
        backup_staging_dir = (
            persistent_backup_dir.parent
            / f".{plugin_id.lower()}.staging-{transaction_id}"
            if durable and SystemUtils.is_docker()
            else None
        )
        backup_previous_dir = (
            persistent_backup_dir.parent
            / f".{plugin_id.lower()}.previous-{transaction_id}"
            if durable and SystemUtils.is_docker()
            else None
        )
        transaction_root = (
            Path(get_runtime_setting('CONFIG_PATH'))
            if durable
            else Path(get_runtime_setting('TEMP_PATH'))
        )
        transaction_dir = (
            transaction_root
            / "plugin_transactions"
            / (transaction_id or f"{plugin_id.lower()}-{uuid.uuid4().hex}")
        )
        plugin_existed = plugin_dir.exists()
        persistent_backup_existed = persistent_backup_dir.exists()
        try:
            transaction_dir.mkdir(parents=True, exist_ok=False)
            if plugin_existed:
                shutil.copytree(plugin_dir, transaction_dir / "package")
        except Exception:
            shutil.rmtree(transaction_dir, ignore_errors=True)
            raise
        return PluginPackageCheckpoint(
            plugin_id=plugin_id,
            plugin_dir=plugin_dir,
            persistent_backup_dir=persistent_backup_dir,
            backup_staging_dir=backup_staging_dir,
            backup_previous_dir=backup_previous_dir,
            transaction_dir=transaction_dir,
            plugin_existed=plugin_existed,
            persistent_backup_existed=persistent_backup_existed,
        )

    def restore_checkpoint(
        self,
        *,
        plugin_id: str,
        transaction_id: str,
        plugin_existed: bool,
        persistent_backup_existed: bool,
    ) -> PluginPackageCheckpoint:
        """按受控根目录和事务 ID 重建崩溃回放所需的文件引用。"""
        plugin_dir = self.__plugin_dir(plugin_id)
        persistent_backup_dir = (
            Path(get_runtime_setting('CONFIG_PATH'))
            / "plugins_backup"
            / plugin_id.lower()
        ).resolve()
        durable_backup = SystemUtils.is_docker()
        return PluginPackageCheckpoint(
            plugin_id=plugin_id,
            plugin_dir=plugin_dir,
            persistent_backup_dir=persistent_backup_dir,
            backup_staging_dir=(
                persistent_backup_dir.parent
                / f".{plugin_id.lower()}.staging-{transaction_id}"
                if durable_backup
                else None
            ),
            backup_previous_dir=(
                persistent_backup_dir.parent
                / f".{plugin_id.lower()}.previous-{transaction_id}"
                if durable_backup
                else None
            ),
            transaction_dir=(
                Path(get_runtime_setting('CONFIG_PATH'))
                / "plugin_transactions"
                / transaction_id
            ),
            plugin_existed=plugin_existed,
            persistent_backup_existed=persistent_backup_existed,
        )

    async def async_checkpoint(
        self,
        plugin_id: str,
        transaction_id: Optional[str] = None,
    ) -> PluginPackageCheckpoint:
        """在线程池中创建插件包文件快照。"""
        return cast(
            PluginPackageCheckpoint,
            await _await_thread_operation(
                self.checkpoint,
                plugin_id,
                transaction_id,
            ),
        )

    @staticmethod
    def commit(checkpoint: PluginPackageCheckpoint) -> None:
        """清理已完成事务的运行目录快照和残余替换材料。"""
        if checkpoint.backup_staging_dir and checkpoint.backup_staging_dir.exists():
            raise RuntimeError("持久备份尚未提升，不能清理插件安装事务")
        if checkpoint.backup_previous_dir and checkpoint.backup_previous_dir.exists():
            raise RuntimeError("旧持久备份尚未清理，不能结束插件安装事务")
        if checkpoint.transaction_dir.exists():
            shutil.rmtree(checkpoint.transaction_dir, ignore_errors=False)

    async def async_commit(self, checkpoint: PluginPackageCheckpoint) -> None:
        """在线程池中清理已提交的插件包快照。"""
        await _await_thread_operation(self.commit, checkpoint)

    @staticmethod
    def rollback(checkpoint: PluginPackageCheckpoint) -> None:
        """兼容旧调用方，恢复运行目录和持久备份后清理恢复材料。"""
        PluginPackageManager.restore(checkpoint)
        PluginPackageManager.cleanup(checkpoint)

    @staticmethod
    def restore(checkpoint: PluginPackageCheckpoint) -> None:
        """恢复运行目录和提交前持久备份，并保留快照直到 journal 删除。"""
        if checkpoint.rollback_marker.is_file():
            return
        PluginPackageManager.__restore_tree(
            target=checkpoint.plugin_dir,
            snapshot=checkpoint.transaction_dir / "package",
            existed=checkpoint.plugin_existed,
            label=f"插件 {checkpoint.plugin_id} 运行目录",
        )
        PluginPackageManager.__rollback_persistent_backup(checkpoint)
        if checkpoint.backup_staging_dir and checkpoint.backup_staging_dir.exists():
            shutil.rmtree(checkpoint.backup_staging_dir, ignore_errors=False)
        checkpoint.transaction_dir.mkdir(parents=True, exist_ok=True)
        checkpoint.rollback_marker.touch(exist_ok=True)

    @staticmethod
    def cleanup(checkpoint: PluginPackageCheckpoint) -> None:
        """在 journal 已删除后清理恢复材料；重复调用保持幂等。"""
        if checkpoint.transaction_dir.exists():
            shutil.rmtree(checkpoint.transaction_dir, ignore_errors=False)

    async def async_rollback(self, checkpoint: PluginPackageCheckpoint) -> None:
        """在线程池中恢复插件包文件快照。"""
        await _await_thread_operation(self.rollback, checkpoint)

    async def async_restore(self, checkpoint: PluginPackageCheckpoint) -> None:
        """在线程池恢复插件状态，并保留 journal 仍需引用的材料。"""
        await _await_thread_operation(self.restore, checkpoint)

    async def async_cleanup(self, checkpoint: PluginPackageCheckpoint) -> None:
        """在线程池清理已失去 journal 所有权的恢复材料。"""
        await _await_thread_operation(self.cleanup, checkpoint)

    @staticmethod
    def __rollback_persistent_backup(
        checkpoint: PluginPackageCheckpoint,
    ) -> None:
        """把已激活但尚未提交的持久备份恢复到事务前状态。"""
        previous = checkpoint.backup_previous_dir
        staging = checkpoint.backup_staging_dir
        if previous is None or staging is None:
            return

        target = checkpoint.persistent_backup_dir
        if previous.exists():
            discarded = target.parent / f".{target.name}.discard-{uuid.uuid4().hex}"
            try:
                if target.exists():
                    target.replace(discarded)
                previous.replace(target)
                if discarded.exists():
                    shutil.rmtree(discarded, ignore_errors=False)
            except Exception:
                if not target.exists() and discarded.exists():
                    discarded.replace(target)
                raise
            finally:
                if target.exists() and discarded.exists():
                    shutil.rmtree(discarded, ignore_errors=True)
            return

        if staging.exists():
            return
        if checkpoint.persistent_backup_existed:
            if target.exists():
                return
            raise FileNotFoundError(
                f"插件 {checkpoint.plugin_id} 的旧持久备份恢复材料不存在"
            )
        if target.exists():
            shutil.rmtree(target, ignore_errors=False)

    @staticmethod
    def __restore_tree(
        *,
        target: Path,
        snapshot: Path,
        existed: bool,
        label: str,
    ) -> None:
        """用同级 staging 替换目录，失败时保留替换前的当前目录。"""
        if existed and not snapshot.is_dir():
            raise FileNotFoundError(f"{label}补偿快照不存在：{snapshot}")

        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.parent / f".{target.name}.restore-{uuid.uuid4().hex}"
        previous = target.parent / f".{target.name}.previous-{uuid.uuid4().hex}"
        try:
            if existed:
                shutil.copytree(snapshot, staging)
            if target.exists():
                target.replace(previous)
            if existed:
                staging.replace(target)
            if previous.exists():
                shutil.rmtree(previous)
        except Exception:
            if not target.exists() and previous.exists():
                previous.replace(target)
            raise
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            if target.exists() and previous.exists():
                shutil.rmtree(previous, ignore_errors=True)

    @classmethod
    def stage_persistent_backup(cls, checkpoint: PluginPackageCheckpoint) -> None:
        """把新载荷复制到持久配置目录的独立 staging，不覆盖现有备份。"""
        staging = checkpoint.backup_staging_dir
        if staging is None:
            return
        if not checkpoint.plugin_dir.is_dir():
            raise FileNotFoundError(
                f"插件 {checkpoint.plugin_id} 运行目录不存在"
            )
        staging.parent.mkdir(parents=True, exist_ok=True)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=False)
        shutil.copytree(
            checkpoint.plugin_dir,
            staging,
            ignore=shutil.ignore_patterns(*cls._COPY_IGNORE),
        )

    async def async_stage_persistent_backup(
        self,
        checkpoint: PluginPackageCheckpoint,
    ) -> None:
        """在线程池准备新载荷的容器恢复备份。"""
        await _await_thread_operation(self.stage_persistent_backup, checkpoint)

    @staticmethod
    def activate_persistent_backup(checkpoint: PluginPackageCheckpoint) -> None:
        """在数据库提交前激活新备份，并保留上一份备份供失败补偿。"""
        staging = checkpoint.backup_staging_dir
        previous = checkpoint.backup_previous_dir
        if staging is None or previous is None:
            return

        target = checkpoint.persistent_backup_dir
        target.parent.mkdir(parents=True, exist_ok=True)
        if staging.exists():
            if target.exists() and not previous.exists():
                target.replace(previous)
            if not target.exists():
                staging.replace(target)
        elif not target.exists():
            raise FileNotFoundError(
                f"插件 {checkpoint.plugin_id} 的持久备份 staging 不存在"
            )

    async def async_activate_persistent_backup(
        self,
        checkpoint: PluginPackageCheckpoint,
    ) -> None:
        """在线程池激活新持久备份，同时保留失败补偿材料。"""
        await _await_thread_operation(self.activate_persistent_backup, checkpoint)

    @staticmethod
    def finalize_persistent_backup(checkpoint: PluginPackageCheckpoint) -> None:
        """数据库提交后清理上一份持久备份；重复调用保持幂等。"""
        staging = checkpoint.backup_staging_dir
        previous = checkpoint.backup_previous_dir
        if staging is None or previous is None:
            return
        if staging.exists():
            raise RuntimeError("新持久备份尚未激活")
        if not checkpoint.persistent_backup_dir.is_dir():
            raise FileNotFoundError(
                f"插件 {checkpoint.plugin_id} 的已提交持久备份不存在"
            )
        if previous.exists():
            shutil.rmtree(previous, ignore_errors=False)

    async def async_finalize_persistent_backup(
        self,
        checkpoint: PluginPackageCheckpoint,
    ) -> None:
        """在线程池清理数据库提交后的旧持久备份。"""
        await _await_thread_operation(self.finalize_persistent_backup, checkpoint)

    def payload_receipt(self, plugin_id: str) -> str:
        """按稳定相对路径和文件内容计算已安装载荷收据。"""
        plugin_dir = self.__plugin_dir(plugin_id)
        if not plugin_dir.is_dir():
            raise FileNotFoundError(f"插件 {plugin_id} 运行目录不存在")
        return self.__tree_receipt(plugin_dir)

    @classmethod
    def persistent_backup_receipt(
        cls,
        checkpoint: PluginPackageCheckpoint,
    ) -> str:
        """计算已提升持久备份的内容收据，供崩溃回放确认终态。"""
        if not checkpoint.persistent_backup_dir.is_dir():
            raise FileNotFoundError(
                f"插件 {checkpoint.plugin_id} 持久备份不存在"
            )
        return cls.__tree_receipt(checkpoint.persistent_backup_dir)

    @classmethod
    def __tree_receipt(cls, root: Path) -> str:
        """对插件目录使用稳定路径和文件内容生成审计收据。"""

        digest = hashlib.sha256()
        for path in sorted(
            root.rglob("*"),
            key=lambda item: item.relative_to(root).as_posix(),
        ):
            relative = path.relative_to(root).as_posix()
            if cls.__ignored_receipt_path(path, root):
                continue
            encoded_path = relative.encode("utf-8")
            digest.update(len(encoded_path).to_bytes(4, "big"))
            digest.update(encoded_path)
            if path.is_symlink():
                digest.update(b"L")
                target = path.readlink().as_posix().encode("utf-8")
                digest.update(len(target).to_bytes(4, "big"))
                digest.update(target)
            elif path.is_dir():
                digest.update(b"D")
            elif path.is_file():
                digest.update(b"F")
                with path.open("rb") as file_handle:
                    for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
                        digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"

    async def async_payload_receipt(self, plugin_id: str) -> str:
        """在线程池计算插件载荷收据。"""
        return cast(
            str,
            await _await_thread_operation(self.payload_receipt, plugin_id),
        )

    async def async_committed_payload_receipt(
        self,
        checkpoint: PluginPackageCheckpoint,
    ) -> str:
        """读取数据库已提交载荷在当前部署模式下的恢复事实。"""
        if checkpoint.backup_staging_dir is not None:
            return cast(
                str,
                await _await_thread_operation(
                    self.persistent_backup_receipt,
                    checkpoint,
                ),
            )
        return await self.async_payload_receipt(checkpoint.plugin_id)

    @classmethod
    def __ignored_receipt_path(cls, path: Path, root: Path) -> bool:
        """排除不会进入运行载荷和持久备份的派生文件。"""
        relative_parts = path.relative_to(root).parts
        return any(
            part in {"__pycache__", "node_modules", ".DS_Store"}
            or part.endswith(".pyc")
            for part in relative_parts
        )

    def install(
        self,
        plugin_id: str,
        repo_url: str,
        package_version: Optional[str] = None,
        release_version: Optional[str] = None,
        force_install: bool = False,
    ) -> tuple[bool, str]:
        """同步安装插件包，下载过程继续复用既有市场兼容策略。"""
        return cast(
            tuple[bool, str],
            cast(Any, self._helper)._PluginHelper__install_package(
                pid=plugin_id,
                repo_url=repo_url,
                package_version=package_version,
                release_version=release_version,
                force_install=force_install,
            ),
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
        return cast(
            tuple[bool, str],
            await cast(Any, self._helper)._PluginHelper__async_install_package(
                pid=plugin_id,
                repo_url=repo_url,
                package_version=package_version,
                release_version=release_version,
                force_install=force_install,
            ),
        )

    def sync_local(self, plugin_id: str, source_dir: Path) -> bool:
        """用本地仓库内容原子替换运行副本，失败时恢复原目录。"""
        source_dir = source_dir.resolve()
        plugin_dir = self.__plugin_dir(plugin_id)
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
        original_dir = self.__plugin_dir(plugin_id)
        clone_dir = self.__plugin_dir(clone_id)
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
