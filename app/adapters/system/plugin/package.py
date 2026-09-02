"""插件包文件安装、快照恢复和分身处理适配器。"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import io
import os
import re
import shutil
import stat
import uuid
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Optional, Protocol, Sequence, cast

import aiofiles  # type: ignore[import-untyped]
import aioshutil
from anyio import Path as AsyncPath

from app.adapters.system.host import SystemUtils
from app.adapters.system.plugin.health import PluginRuntimeHealth
from app.adapters.system.plugin.manifest import (
    PluginDependencyManifestError,
    dependency_manifest_declares_installation,
    load_dependency_manifest,
)
from app.domain.plugin import (
    PluginReleaseInstallPlan,
    build_plugin_release_install_plan,
)
from app.runtime.dependencies.native import (
    LoadedNativeDependencySnapshot,
    NativeDependencyChange,
    capture_loaded_native_dependencies,
    detect_changed_native_dependencies,
)
from app.runtime.execution import (
    run_in_threadpool_to_completion as _await_thread_operation,
)
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.runtime.version import get_app_version

# 判定插件从已装版本切换到另一版本能否被安装期接受，返回拒绝说明或 None
VersionSwitchGuard = Callable[[str, Path, Path], Optional[str]]


def _allow_version_switch(_pid: str, _plugin_dir: Path, _source_dir: Path) -> Optional[str]:
    """未装配版本并存检查端口时不拦截安装，保持今天的单版本行为。"""
    return None


@dataclass(frozen=True, slots=True)
class PluginInstallVersionTarget:
    """已就位暂存内容应当落盘的版本子目录与登记用版本号。"""

    subdirectory: str
    version: str


# 判定已就位的暂存内容应当写入插件根目录下的哪个子目录，必要时原地迁移存量平铺
# 布局；返回 None 表示直接写入插件根目录本身（平铺布局，不登记版本元信息）
InstallTargetResolver = Callable[[str, Path, Path], Optional[PluginInstallVersionTarget]]


def _flat_install_target(
    _pid: str, _plugin_dir: Path, _staged_source_dir: Path
) -> Optional[PluginInstallVersionTarget]:
    """未装配版本目录解析端口时落回平铺布局，保持今天的单版本覆盖安装行为。"""
    return None


# 把已落盘的版本目录登记进版本元信息并置为当前版本：(插件根目录, 版本号, 来源标签)
InstallVersionRegistrar = Callable[[Path, str, str], None]


def _noop_version_registrar(_plugin_dir: Path, _version: str, _source: str) -> None:
    """未装配版本元信息登记端口时不写入 versions.json，保持平铺布局行为。"""
    return None


class PluginPackageSourcePort(Protocol):
    """声明包 owner 读取市场元数据和远端制品所需的外部端口。"""

    def is_local_repo_url(self, repo_url: Optional[str]) -> bool:
        """判断来源是否为本地仓库。"""

    def parse_local_repo_url(self, repo_url: str) -> Optional[str]:
        """解析本地来源中的插件标识。"""

    def parse_local_repo_path(self, repo_url: str) -> Optional[Path]:
        """解析本地来源中的仓库目录。"""

    def parse_local_repo_package_version(self, repo_url: str) -> Optional[str]:
        """解析本地来源中的包代际。"""

    def make_local_repo_url(
        self,
        plugin_id: str,
        repo_path: Optional[Path] = None,
        package_version: Optional[str] = None,
    ) -> str:
        """生成本地仓库来源标识。"""

    def get_local_plugin_candidate(
        self,
        pid: str,
        package_version: Optional[str] = None,
        repo_path: Optional[Path] = None,
        strict_compat: bool = True,
        strict_system_version: bool = True,
    ) -> Optional[dict[str, Any]]:
        """读取一个本地插件候选。"""

    def check_plugin_system_version(
        self, plugin_info: dict[str, Any]
    ) -> tuple[bool, str]:
        """校验插件声明的宿主版本约束。"""

    def get_plugin_package_version(
        self, plugin_id: str, repo_url: str, package_version: Optional[str]
    ) -> Optional[str]:
        """选择适用于当前宿主的远端索引代际。"""

    async def async_get_plugin_package_version(
        self, plugin_id: str, repo_url: str, package_version: Optional[str]
    ) -> Optional[str]:
        """异步选择适用于当前宿主的远端索引代际。"""

    def get_plugins(
        self, repo_url: str, package_version: Optional[str] = None
    ) -> Optional[dict[str, dict[str, Any]]]:
        """读取远端插件索引。"""

    async def async_get_plugins(
        self, repo_url: str, package_version: Optional[str] = None
    ) -> Optional[dict[str, dict[str, Any]]]:
        """异步读取远端插件索引。"""

    def get_plugin_release_versions(
        self, plugin_id: str, repo_url: str
    ) -> list[dict[str, Any]]:
        """读取插件可安装 Release。"""

    async def async_get_plugin_release_versions(
        self, plugin_id: str, repo_url: str
    ) -> list[dict[str, Any]]:
        """异步读取插件可安装 Release。"""

    def get_repo_info(self, repo_url: str) -> tuple[Optional[str], Optional[str]]:
        """解析 GitHub 仓库所有者和仓库名。"""

    def request(
        self,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        timeout: Optional[int] = 60,
        is_api: bool = False,
    ) -> Any:
        """同步读取 GitHub 元数据或制品。"""

    async def async_request(
        self,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        timeout: Optional[int] = 60,
        is_api: bool = False,
    ) -> Any:
        """异步读取 GitHub 元数据或制品。"""


@dataclass(slots=True)
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
    native_dependencies: LoadedNativeDependencySnapshot | None = None

    @property
    def existed(self) -> bool:
        """保留旧调用方读取运行目录存在状态的兼容属性。"""
        return self.plugin_existed

    @property
    def rollback_marker(self) -> Path:
        """返回文件补偿完成标记，供 PREPARED 重放保持幂等。"""
        return self.transaction_dir / ".rollback-complete"


@dataclass(frozen=True, slots=True)
class _RemotePluginInstallSelection:
    """冻结远端插件完成准入后的安装方式和索引事实。"""

    user_repo: str
    package_version: str
    release_tag: Optional[str]
    fallback_to_filelist: bool


class PluginPackageManager:
    """隔离插件包安装、本地同步、分身改写和文件补偿能力。"""

    _COPY_IGNORE = ("__pycache__", "*.pyc", ".DS_Store", "node_modules")

    def __init__(
        self,
        source: Optional[PluginPackageSourcePort] = None,
        *,
        health: Optional[PluginRuntimeHealth] = None,
        plugin_root: Optional[Path] = None,
        version_switch_guard: VersionSwitchGuard = _allow_version_switch,
        install_target_resolver: InstallTargetResolver = _flat_install_target,
        install_version_registrar: InstallVersionRegistrar = _noop_version_registrar,
    ) -> None:
        """保存外部来源端口、依赖健康 owner 和版本目录布局相关的注入端口。

        版本写法体检、目标目录决策和版本元信息登记都依赖运行时扩展包，不属于
        适配器层职责，因此只接受可注入的端口；未注入时全部退化为今天的单版本
        平铺覆盖安装行为。
        :param source: 市场元数据与制品来源端口
        :param health: 依赖健康 owner
        :param plugin_root: 插件根目录，未注入时按运行配置解析
        :param version_switch_guard: 判定版本切换能否被接受的端口
        :param install_target_resolver: 判定暂存内容落盘子目录的端口
        :param install_version_registrar: 登记已落盘版本元信息的端口
        """
        self._source = source
        self._health = health or PluginRuntimeHealth()
        self._plugin_root = plugin_root.resolve() if plugin_root else None
        self._version_switch_guard = version_switch_guard
        self._install_target_resolver = install_target_resolver
        self._install_version_registrar = install_version_registrar

    def _require_source(self) -> PluginPackageSourcePort:
        """返回已装配来源端口，未完成组合时拒绝执行包写入。"""
        if self._source is None:
            raise RuntimeError("插件包来源客户端尚未完成装配")
        return self._source

    def _plugins_root(self) -> Path:
        """返回组合根注入目录；旧直接构造调用按当前运行配置解析。"""
        return self._plugin_root or (
            Path(get_runtime_setting('ROOT_PATH')) / "app" / "plugins"
        ).resolve()

    def __plugin_dir(self, plugin_id: str) -> Path:
        """解析插件运行目录并拒绝越出宿主插件根目录的标识。"""
        plugins_root = self._plugins_root()
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
    def native_dependency_changes(
        checkpoint: PluginPackageCheckpoint,
    ) -> tuple[NativeDependencyChange, ...]:
        """返回安装中被替换、但当前进程仍持有旧代码的原生发行包。"""
        if checkpoint.native_dependencies is None:
            return ()
        try:
            return detect_changed_native_dependencies(checkpoint.native_dependencies)
        except Exception as error:  # noqa: BLE001 - 诊断失败不能改写安装结果
            logger.warning("检测插件原生依赖变更失败：%s", error)
            return ()

    async def async_native_dependency_changes(
        self,
        checkpoint: PluginPackageCheckpoint,
    ) -> tuple[NativeDependencyChange, ...]:
        """在线程池中比较原生发行包，避免文件枚举阻塞事件循环。"""
        return cast(
            tuple[NativeDependencyChange, ...],
            await _await_thread_operation(
                self.native_dependency_changes,
                checkpoint,
            ),
        )

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

    def is_local_repo_url(self, repo_url: Optional[str]) -> bool:
        """委托外部来源客户端判断本地仓库标识。"""
        return self._require_source().is_local_repo_url(repo_url)

    def parse_local_repo_url(self, repo_url: str) -> Optional[str]:
        """委托外部来源客户端解析本地插件标识。"""
        return self._require_source().parse_local_repo_url(repo_url)

    def parse_local_repo_path(self, repo_url: str) -> Optional[Path]:
        """委托外部来源客户端解析本地仓库路径。"""
        return self._require_source().parse_local_repo_path(repo_url)

    def parse_local_repo_package_version(self, repo_url: str) -> Optional[str]:
        """委托外部来源客户端解析本地包代际。"""
        return self._require_source().parse_local_repo_package_version(repo_url)

    def make_local_repo_url(
        self,
        plugin_id: str,
        repo_path: Optional[Path] = None,
        package_version: Optional[str] = None,
    ) -> str:
        """委托外部来源客户端生成本地来源标识。"""
        return self._require_source().make_local_repo_url(
            plugin_id, repo_path, package_version
        )

    def get_local_plugin_candidate(
        self,
        pid: str,
        package_version: Optional[str] = None,
        repo_path: Optional[Path] = None,
        strict_compat: bool = True,
        strict_system_version: bool = True,
    ) -> Optional[dict[str, Any]]:
        """委托外部来源客户端读取本地插件候选。"""
        return self._require_source().get_local_plugin_candidate(
            pid=pid,
            package_version=package_version,
            repo_path=repo_path,
            strict_compat=strict_compat,
            strict_system_version=strict_system_version,
        )

    def check_plugin_system_version(
        self, plugin_info: dict[str, Any]
    ) -> tuple[bool, str]:
        """委托外部来源客户端校验宿主版本约束。"""
        return self._require_source().check_plugin_system_version(plugin_info)

    def get_plugin_package_version(
        self, plugin_id: str, repo_url: str, package_version: Optional[str]
    ) -> Optional[str]:
        """委托外部来源客户端选择远端索引代际。"""
        return self._require_source().get_plugin_package_version(
            plugin_id, repo_url, package_version
        )

    async def async_get_plugin_package_version(
        self, plugin_id: str, repo_url: str, package_version: Optional[str]
    ) -> Optional[str]:
        """异步委托外部来源客户端选择远端索引代际。"""
        return await self._require_source().async_get_plugin_package_version(
            plugin_id, repo_url, package_version
        )

    def get_plugins(
        self, repo_url: str, package_version: Optional[str] = None
    ) -> Optional[dict[str, dict[str, Any]]]:
        """委托外部来源客户端读取远端插件索引。"""
        return self._require_source().get_plugins(repo_url, package_version)

    async def async_get_plugins(
        self, repo_url: str, package_version: Optional[str] = None
    ) -> Optional[dict[str, dict[str, Any]]]:
        """异步委托外部来源客户端读取远端插件索引。"""
        return await self._require_source().async_get_plugins(
            repo_url, package_version
        )

    def get_plugin_release_versions(
        self, plugin_id: str, repo_url: str
    ) -> list[dict[str, Any]]:
        """委托外部来源客户端读取可安装 Release。"""
        return self._require_source().get_plugin_release_versions(
            plugin_id, repo_url
        )

    async def async_get_plugin_release_versions(
        self, plugin_id: str, repo_url: str
    ) -> list[dict[str, Any]]:
        """异步委托外部来源客户端读取可安装 Release。"""
        return await self._require_source().async_get_plugin_release_versions(
            plugin_id, repo_url
        )

    def get_repo_info(self, repo_url: str) -> tuple[Optional[str], Optional[str]]:
        """委托外部来源客户端解析 GitHub 仓库信息。"""
        return self._require_source().get_repo_info(repo_url)

    def __request_with_fallback(
        self,
        url: str,
        headers: Optional[dict[str, str]] = None,
        timeout: Optional[int] = 60,
        is_api: bool = False,
    ) -> Any:
        """通过外部来源客户端同步读取远端内容。"""
        return self._require_source().request(
            url, headers=headers, timeout=timeout, is_api=is_api
        )

    async def __async_request_with_fallback(
        self,
        url: str,
        headers: Optional[dict[str, str]] = None,
        timeout: Optional[int] = 60,
        is_api: bool = False,
    ) -> Any:
        """通过外部来源客户端异步读取远端内容。"""
        return await self._require_source().async_request(
            url, headers=headers, timeout=timeout, is_api=is_api
        )

    @staticmethod
    def _build_remote_plugin_install_plan(
        *,
        pid: str,
        meta: dict[str, Any],
        release_version: Optional[str] = None,
        release_items: Sequence[dict[str, Any]] = (),
    ) -> tuple[Optional[PluginReleaseInstallPlan], str]:
        """使用纯领域规则选择 Release 或文件列表安装计划。"""
        return build_plugin_release_install_plan(
            plugin_id=pid,
            metadata=meta,
            release_version=release_version,
            release_items=release_items,
            current_version=get_app_version(),
        )

    def _resolve_remote_plugin_repository(
        self,
        pid: str,
        repo_url: str,
    ) -> tuple[Optional[str], Optional[tuple[bool, str]]]:
        """统一校验远端安装前置条件并返回规范仓库标识。"""
        if SystemUtils.is_frozen():
            return None, (False, "可执行文件模式下，只能安装本地插件")
        if not pid or not repo_url:
            return None, (False, "参数错误")
        user, repo = self.get_repo_info(repo_url)
        if not user or not repo:
            return None, (False, "不支持的插件仓库地址格式")
        return f"{user}/{repo}", None

    @staticmethod
    def _accept_remote_package_version(
        pid: str,
        package_version: Optional[str],
    ) -> Optional[tuple[bool, str]]:
        """统一记录索引代际选择，并映射找不到兼容插件的失败结果。"""
        if package_version is None:
            message = f"{pid} 没有找到适用于当前版本的插件"
            logger.debug(message)
            return False, message
        if package_version:
            logger.debug(
                f"{pid} 从 package.{package_version}.json 中找到适用于当前版本的插件"
            )
        else:
            logger.debug(f"{pid} 从 package.json 中找到适用于当前版本的插件")
        return None

    @classmethod
    def _select_remote_install(
        cls,
        *,
        pid: str,
        user_repo: str,
        package_version: str,
        meta: dict[str, Any],
        release_version: Optional[str],
        release_items: Sequence[dict[str, Any]],
    ) -> tuple[Optional[_RemotePluginInstallSelection], str]:
        """把元数据和 Release 列表统一映射为可执行的远端安装选择。"""
        plan, message = cls._build_remote_plugin_install_plan(
            pid=pid,
            meta=meta,
            release_version=release_version,
            release_items=release_items,
        )
        if plan is None:
            return None, message
        return (
            _RemotePluginInstallSelection(
                user_repo=user_repo,
                package_version=package_version,
                release_tag=plan.release_tag,
                fallback_to_filelist=plan.fallback_to_filelist,
            ),
            "",
        )

    def install_packages_with_fallback(
        self,
        dependency_files: Path | Sequence[Path],
        find_links_dirs: Optional[list[Path]] = None,
    ) -> tuple[bool, str]:
        """把插件依赖清单交给运行环境健康 owner 安装。"""
        return self._health.install_packages_with_fallback(
            dependency_files, find_links_dirs
        )

    async def async_install_packages_with_fallback(
        self,
        dependency_files: Path | Sequence[Path],
        find_links_dirs: Optional[list[Path]] = None,
    ) -> tuple[bool, str]:
        """异步把插件依赖清单交给运行环境健康 owner 安装。"""
        return await self._health.async_install_packages_with_fallback(
            dependency_files, find_links_dirs
        )

    def install(
        self,
        plugin_id: str,
        repo_url: str,
        package_version: Optional[str] = None,
        release_version: Optional[str] = None,
        force_install: bool = False,
        checkpoint: PluginPackageCheckpoint | None = None,
    ) -> tuple[bool, str]:
        """同步安装插件包；已有事务快照时禁止兼容层再创建第二份备份。"""
        return self.install_raw(
            pid=plugin_id,
            repo_url=repo_url,
            package_version=package_version,
            release_version=release_version,
            force_install=force_install or checkpoint is not None,
            before_dependency_install=(
                (lambda: self.__capture_native_dependencies(checkpoint))
                if checkpoint is not None
                else None
            ),
        )

    async def async_install(
        self,
        plugin_id: str,
        repo_url: str,
        package_version: Optional[str] = None,
        release_version: Optional[str] = None,
        force_install: bool = False,
        checkpoint: PluginPackageCheckpoint | None = None,
    ) -> tuple[bool, str]:
        """异步安装插件包；已有事务快照时禁止兼容层再创建第二份备份。"""
        return await self.async_install_raw(
            pid=plugin_id,
            repo_url=repo_url,
            package_version=package_version,
            release_version=release_version,
            force_install=force_install or checkpoint is not None,
            before_dependency_install=(
                (lambda: self.__capture_native_dependencies(checkpoint))
                if checkpoint is not None
                else None
            ),
        )

    @staticmethod
    def __capture_native_dependencies(
        checkpoint: PluginPackageCheckpoint,
    ) -> None:
        """仅在插件依赖即将安装时记录当前进程的原生载荷。"""
        if checkpoint.native_dependencies is not None:
            return
        try:
            checkpoint.native_dependencies = capture_loaded_native_dependencies()
        except Exception as error:  # noqa: BLE001 - 诊断失败不能阻断插件安装
            logger.warning("记录插件原生依赖安装前状态失败：%s", error)

    def install_raw(self, pid: str, repo_url: str, package_version: Optional[str] = None,
                          release_version: Optional[str] = None, force_install: bool = False,
                          before_dependency_install: Optional[Callable[[], None]] = None) \
            -> tuple[bool, str]:
        """执行已通过来源准入的同步包安装，不负责身份或运行态提交。"""
        if self.is_local_repo_url(repo_url):
            return self.__install_local_package(
                pid=pid,
                repo_url=repo_url,
                force_install=force_install,
                before_dependency_install=before_dependency_install,
            )

        user_repo, error_result = self._resolve_remote_plugin_repository(
            pid, repo_url
        )
        if error_result is not None:
            return error_result
        assert user_repo is not None

        preferred_version = package_version or get_runtime_setting('VERSION_FLAG')
        selected_version = self.get_plugin_package_version(
            pid, repo_url, preferred_version
        )
        if error_result := self._accept_remote_package_version(
            pid, selected_version
        ):
            return error_result
        assert selected_version is not None

        # 2. 决定安装方式（release 或文件列表）并执行统一安装流程。
        meta = self.__get_plugin_meta(pid, repo_url, selected_version)
        release_items = (
            self.get_plugin_release_versions(pid, repo_url)
            if release_version
            else []
        )
        selection, message = self._select_remote_install(
            pid=pid,
            user_repo=user_repo,
            package_version=selected_version,
            meta=meta,
            release_version=release_version,
            release_items=release_items,
        )
        if selection is None:
            return False, message

        release_tag = selection.release_tag
        if release_tag and not selection.fallback_to_filelist:
            def prepare_selected_release(staging_dir: Path) -> tuple[bool, str]:
                return self.__install_from_release(
                    pid,
                    selection.user_repo,
                    release_tag,
                    staging_dir,
                )

            return self.__install_flow_sync(
                pid,
                force_install,
                prepare_selected_release,
                repo_url,
                before_dependency_install,
            )

        if release_tag:
            # 当前索引 Release 失败时回退文件列表，避免发布产物短暂滞后阻断安装。
            def prepare_release(staging_dir: Path) -> tuple[bool, str]:
                ok, msg = self.__install_from_release(
                    pid,
                    selection.user_repo,
                    release_tag,
                    staging_dir,
                )
                if ok:
                    return True, msg
                logger.warning(f"{pid} Release 安装失败，回退文件列表安装：{msg}")
                shutil.rmtree(staging_dir, ignore_errors=True)
                return self.__prepare_content_via_filelist_sync(
                    pid,
                    selection.user_repo,
                    selection.package_version,
                    staging_dir,
                )

            return self.__install_flow_sync(
                pid,
                force_install,
                prepare_release,
                repo_url,
                before_dependency_install,
            )
        # 未声明 release 打包的插件继续使用文件列表方式安装。
        def prepare_filelist(staging_dir: Path) -> tuple[bool, str]:
            return self.__prepare_content_via_filelist_sync(
                pid,
                selection.user_repo,
                selection.package_version,
                staging_dir,
            )

        return self.__install_flow_sync(
            pid,
            force_install,
            prepare_filelist,
            repo_url,
            before_dependency_install,
        )

    def install_local_raw(
        self,
        plugin_id: str,
        repo_url: str = "",
        force_install: bool = False,
        before_dependency_install: Optional[Callable[[], None]] = None,
    ) -> tuple[bool, str]:
        """提供给兼容 Facade 的本地载荷安装入口。"""
        return self.__install_local_package(
            pid=plugin_id,
            repo_url=repo_url,
            force_install=force_install,
            before_dependency_install=before_dependency_install,
        )

    def backup_plugin(self, plugin_id: str) -> Optional[str]:
        """提供给兼容 Facade 的临时备份入口。"""
        return self.__backup_plugin(plugin_id)

    def restore_plugin(self, plugin_id: str, backup_dir: str) -> None:
        """提供给兼容 Facade 的临时备份恢复入口。"""
        self.__restore_plugin(plugin_id, backup_dir)

    def remove_plugin(self, plugin_id: str) -> bool:
        """删除受控插件目录，返回删除前是否存在。"""
        plugin_dir = self.__plugin_dir(plugin_id)
        if not plugin_dir.exists():
            return False
        shutil.rmtree(plugin_dir, ignore_errors=False)
        return True

    def install_from_release(
        self, plugin_id: str, user_repo: str, release_tag: str
    ) -> tuple[bool, str]:
        """提供给兼容 Facade 的 Release 制品安装入口，直接写入插件运行目录。"""
        return self.__install_from_release(
            plugin_id, user_repo, release_tag, self.__plugin_dir(plugin_id)
        )

    def __install_local_package(
        self,
        pid: str,
        repo_url: str = "",
        force_install: bool = False,
        before_dependency_install: Optional[Callable[[], None]] = None,
    ) -> tuple[bool, str]:
        """
        执行已通过来源准入的本地插件包安装。
        """
        local_pid = self.parse_local_repo_url(repo_url) if repo_url else pid
        if not local_pid or local_pid.lower() != pid.lower():
            return False, "本地插件来源与插件ID不匹配"

        repo_path = self.parse_local_repo_path(repo_url) if repo_url else None
        package_version = self.parse_local_repo_package_version(repo_url) if repo_url else None
        candidate = self.get_local_plugin_candidate(
            pid,
            package_version=package_version,
            repo_path=repo_path
        )
        if not candidate:
            return False, f"未找到本地插件：{pid}"
        compatible, message = self.check_plugin_system_version(candidate)
        if not compatible:
            logger.debug(f"{pid} 本地插件系统版本兼容性检查失败：{message}")
            return False, message

        raw_source_path = candidate.get("path")
        if not isinstance(raw_source_path, (str, Path)):
            return False, "本地插件来源路径无效"
        source_dir = Path(raw_source_path)
        dest_dir = self.__plugin_dir(pid)
        try:
            if source_dir.resolve() == dest_dir.resolve():
                return False, "本地插件来源不能与运行目录相同"
        except Exception:
            return False, "本地插件来源路径无效"

        def prepare_local(staging_dir: Path) -> tuple[bool, str]:
            try:
                shutil.copytree(
                    source_dir,
                    staging_dir,
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "node_modules")
                )
                return True, ""
            except Exception as e:
                logger.error(f"复制本地插件 {pid} 失败：{e}")
                return False, f"复制本地插件失败：{e}"

        return self.__install_flow_sync(
            pid=pid,
            force_install=force_install,
            prepare_content=prepare_local,
            source_label="local",
            repo_url=repo_url or self.make_local_repo_url(
                pid,
                (
                    Path(repo_path_value)
                    if isinstance(
                        (repo_path_value := candidate.get("repo_path")),
                        (str, Path),
                    )
                    else None
                ),
                (
                    package_generation
                    if isinstance(
                        (package_generation := candidate.get("package_version")),
                        str,
                    )
                    else None
                ),
            ),
            before_dependency_install=before_dependency_install,
        )

    def __get_file_list(self, pid: str, user_repo: str, package_version: Optional[str] = None) -> \
            tuple[Optional[list[dict[str, Any]]], Optional[str]]:
        """
        获取插件的文件列表
        :param pid: 插件 ID
        :param user_repo: GitHub 仓库的 user/repo 路径
        :return: (文件列表, 错误信息)
        """
        file_api = f"https://api.github.com/repos/{user_repo}/contents/plugins"
        # 如果 package_version 存在（如 "v2"），则加上版本号
        if package_version:
            file_api += f".{package_version}"
        file_api += f"/{pid.lower()}"

        res = self.__request_with_fallback(file_api,
                                           headers=get_runtime_setting('REPO_GITHUB_HEADERS')(repo=user_repo),
                                           is_api=True,
                                           timeout=30)
        if res is None:
            return None, "连接仓库失败"
        elif res.status_code == 404:
            return None, "插件源码目录不存在"
        elif res.status_code != 200:
            return None, f"连接仓库失败：{res.status_code} - " \
                         f"{'超出速率限制，请设置Github Token或稍后重试' if res.status_code == 403 else res.reason}"

        try:
            ret = res.json()
            if isinstance(ret, list) and len(ret) > 0 and "message" not in ret[0]:
                return ret, ""
            else:
                return None, "插件在仓库中不存在或返回数据格式不正确"
        except Exception as e:
            logger.error(f"插件数据解析失败：{e}")
            return None, "插件数据解析失败"

    def __resolve_download_file(
        self,
        pid: str,
        remote_path: object,
        package_version: Optional[str],
        dest_root: Path,
    ) -> Path:
        """把市场文件路径限定到给定目标根目录之下，拒绝绝对路径和目录穿越。"""
        if not isinstance(remote_path, str) or not remote_path or "\\" in remote_path:
            raise ValueError("插件文件路径无效")
        pure_path = PurePosixPath(remote_path)
        if pure_path.is_absolute() or PureWindowsPath(remote_path).is_absolute():
            raise ValueError("插件文件路径无效")
        parts = pure_path.parts
        expected_root = f"plugins.{package_version}" if package_version else "plugins"
        if (
            len(parts) < 3
            or parts[0] != expected_root
            or parts[1].casefold() != pid.casefold()
            or any(part in {"", ".", ".."} for part in parts[2:])
        ):
            raise ValueError("插件文件路径无效")
        resolved_root = dest_root.resolve()
        file_path = (resolved_root / Path(*parts[2:])).resolve()
        if not file_path.is_relative_to(resolved_root):
            raise ValueError("插件文件路径无效")
        return file_path

    def __download_files(self, pid: str, file_list: list[dict[str, Any]], user_repo: str,
                         package_version: Optional[str], dest_root: Path) -> tuple[bool, str]:
        """
        下载插件文件
        :param pid: 插件 ID
        :param file_list: 要下载的文件列表，包含文件的元数据（包括下载链接）
        :param user_repo: GitHub 仓库的 user/repo 路径
        :param dest_root: 文件落盘的目标根目录
        :return: (是否成功, 错误信息)
        """
        if not file_list:
            return False, "文件列表为空"

        # 使用栈结构来替代递归调用，避免递归深度过大问题
        stack = [(pid, file_list)]

        while stack:
            current_pid, current_file_list = stack.pop()

            for item in current_file_list:
                download_url = item.get("download_url")
                if isinstance(download_url, str) and download_url:
                    try:
                        file_path = self.__resolve_download_file(
                            pid,
                            item.get("path"),
                            package_version,
                            dest_root,
                        )
                    except ValueError as error:
                        return False, str(error)
                    logger.debug(f"正在下载文件：{item.get('path')}")
                    res = self.__request_with_fallback(download_url,
                                                       headers=get_runtime_setting('REPO_GITHUB_HEADERS')(repo=user_repo))
                    if not res:
                        return False, f"文件 {item.get('path')} 下载失败！"
                    elif res.status_code != 200:
                        return False, f"下载文件 {item.get('path')} 失败：{res.status_code}"

                    # 创建插件文件夹并写入文件
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(res.text)
                    logger.debug(f"文件 {item.get('path')} 下载成功，保存路径：{file_path}")
                else:
                    # 如果是子目录，则将子目录内容加入栈中继续处理
                    directory_name = item.get("name")
                    if (
                        not isinstance(directory_name, str)
                        or directory_name in {"", ".", ".."}
                        or PurePosixPath(directory_name).name != directory_name
                        or PureWindowsPath(directory_name).name != directory_name
                    ):
                        return False, "插件目录路径无效"
                    sub_list, msg = self.__get_file_list(f"{current_pid}/{directory_name}", user_repo,
                                                         package_version)
                    if not sub_list:
                        return False, msg or "插件子目录读取失败"
                    stack.append((f"{current_pid}/{directory_name}", sub_list))

        return True, ""

    def __install_dependencies_if_required(
        self,
        pid: str,
        content_dir: Path,
        before_dependency_install: Optional[Callable[[], None]] = None,
    ) -> tuple[bool, bool, str]:
        """
        安装插件依赖。
        :param pid: 插件 ID
        :param content_dir: 插件本次已落盘的源码目录
        :return: (是否存在依赖，安装是否成功, 错误信息)
        """
        try:
            manifest = load_dependency_manifest(content_dir)
        except PluginDependencyManifestError as error:
            logger.error(f"{pid} 依赖清单无效：{error}")
            return True, False, str(error)
        if manifest is not None:
            if (
                before_dependency_install is not None
                and dependency_manifest_declares_installation(manifest)
            ):
                try:
                    before_dependency_install()
                except Exception as error:  # noqa: BLE001 - 观察失败不能阻断安装
                    logger.warning(f"{pid} 依赖安装前状态记录失败：{error}")
            logger.info(f"{pid} 存在依赖，开始尝试安装依赖")
            success, error_message = self.install_packages_with_fallback(manifest.path)
            return True, success, "" if success else error_message

        return False, False, "不存在依赖"

    def __backup_plugin(self, pid: str) -> Optional[str]:
        """
        备份旧插件目录
        :param pid: 插件 ID
        :return: 备份目录路径
        """
        plugin_dir = self._plugins_root() / pid.lower()
        backup_dir = Path(get_runtime_setting('TEMP_PATH')) / "plugin_backup" / pid.lower()

        if plugin_dir.exists():
            # 备份时清理已有的备份目录，防止残留文件影响
            if backup_dir.exists():
                shutil.rmtree(backup_dir, ignore_errors=True)
                logger.debug(f"{pid} 旧的备份目录已清理 {backup_dir}")

            shutil.copytree(plugin_dir, backup_dir, dirs_exist_ok=True)
            logger.debug(f"{pid} 插件已备份到 {backup_dir}")

        return str(backup_dir) if backup_dir.exists() else None

    def __restore_plugin(self, pid: str, backup_dir: str) -> None:
        """
        还原旧插件目录
        :param pid: 插件 ID
        :param backup_dir: 备份目录路径
        """
        plugin_dir = self._plugins_root() / pid.lower()
        if plugin_dir.exists():
            shutil.rmtree(plugin_dir, ignore_errors=True)
            logger.debug(f"{pid} 已清理插件目录 {plugin_dir}")

        if Path(backup_dir).exists():
            shutil.copytree(backup_dir, plugin_dir, dirs_exist_ok=True)
            logger.debug(f"{pid} 已还原插件目录 {plugin_dir}")
            shutil.rmtree(backup_dir, ignore_errors=True)
            logger.debug(f"{pid} 已删除备份目录 {backup_dir}")

    def __remove_old_plugin(self, pid: str) -> None:
        """
        删除旧插件
        :param pid: 插件 ID
        """
        plugin_dir = self._plugins_root() / pid.lower()
        if plugin_dir.exists():
            shutil.rmtree(plugin_dir, ignore_errors=True)

    def refresh_persistent_backup(self, pid: str) -> bool:
        """
        刷新插件持久化备份目录，供 docker 重置后恢复使用
        """
        if not SystemUtils.is_docker():
            return True

        plugin_dir = self._plugins_root() / pid.lower()
        if not plugin_dir.exists():
            logger.warn(f"{pid} 插件目录不存在，跳过刷新插件备份")
            return False

        backup_root = get_runtime_setting('CONFIG_PATH') / "plugins_backup"
        backup_dir = backup_root / pid.lower()
        staging_dir = backup_root / f".{pid.lower()}.tmp-{uuid.uuid4().hex}"
        previous_dir = backup_root / f".{pid.lower()}.old-{uuid.uuid4().hex}"
        try:
            backup_root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(
                plugin_dir,
                staging_dir,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
            )
            if backup_dir.exists():
                backup_dir.replace(previous_dir)
            staging_dir.replace(backup_dir)
            if previous_dir.exists():
                shutil.rmtree(previous_dir, ignore_errors=True)
            logger.info(f"已刷新插件备份: {pid}")
            return True
        except Exception as e:
            if not backup_dir.exists() and previous_dir.exists():
                try:
                    previous_dir.replace(backup_dir)
                except Exception as rollback_error:
                    logger.error(
                        f"恢复插件旧备份失败，已保留恢复材料 {previous_dir}: "
                        f"{rollback_error}"
                    )
            logger.error(f"刷新插件备份失败: {pid} - {e}")
            return False
        finally:
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
            if backup_dir.exists() and previous_dir.exists():
                shutil.rmtree(previous_dir, ignore_errors=True)

    def __get_plugin_meta(self, pid: str, repo_url: str,
                          package_version: Optional[str]) -> dict[str, Any]:
        """读取远端插件元数据，并把异常收敛为空映射。"""
        try:
            plugins = (
                          self.get_plugins(repo_url) if not package_version
                          else self.get_plugins(repo_url, package_version)
                      ) or {}
            meta = plugins.get(pid)
            return meta if isinstance(meta, dict) else {}
        except Exception as e:
            logger.error(f"获取插件 {pid} 元数据失败：{e}")
            return {}

    def __new_install_staging_dir(self, pid: str) -> Path:
        """分配一个全新的安装暂存目录，用于在触碰插件根目录前完整准备待装内容。"""
        return (
            Path(get_runtime_setting('TEMP_PATH'))
            / "plugin_install_staging"
            / f"{pid.lower()}-{uuid.uuid4().hex}"
        )

    @staticmethod
    def __swap_staged_plugin_content(staging_dir: Path, final_dir: Path) -> None:
        """把已就位的暂存内容换入最终目录，任一步失败都保留换入前的目录内容。

        与既有的运行目录补偿替换手法一致（见 __restore_tree）：先把旧目标改名
        挪到同级临时位置，暂存内容改名落位后再删除旧目标；只要新内容还没落位，
        旧目标就仍然完整，因此中途失败可以原样退回。跨设备无法原子改名时退化
        为复制加删除，复制完成后才删除暂存内容，避免留下半份文件。

        :param staging_dir: 已就位的待安装内容目录
        :param final_dir: 最终写入目标目录，可能已存在旧内容
        :raise OSError: 改名或复制失败
        """
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        previous = final_dir.parent / f".{final_dir.name}.previous-{uuid.uuid4().hex}"
        had_previous_content = final_dir.exists()
        try:
            if had_previous_content:
                os.rename(final_dir, previous)
            try:
                os.rename(staging_dir, final_dir)
            except OSError as error:
                if getattr(error, "errno", None) != errno.EXDEV:
                    raise
                logger.warning(
                    f"插件安装内容跨设备无法原子改名，退化为复制后删除："
                    f"{staging_dir} -> {final_dir} - {error}"
                )
                shutil.copytree(staging_dir, final_dir)
                shutil.rmtree(staging_dir, ignore_errors=True)
        except OSError:
            if not final_dir.exists() and had_previous_content and previous.exists():
                os.rename(previous, final_dir)
            raise
        finally:
            if previous.exists():
                shutil.rmtree(previous, ignore_errors=True)

    def __place_staged_plugin_content(
        self,
        pid: str,
        plugin_dir: Path,
        staging_dir: Path,
        source_label: str,
    ) -> tuple[Optional[Path], str]:
        """决定暂存内容的落盘子目录、原子换入，并在写入版本目录时登记版本元信息。

        目标目录决策委托给注入的解析端口，必要时该端口会原地迁移存量平铺布局；
        本方法只负责在决策就绪后做机械的目录换入和登记，不重复做写法体检。

        :param pid: 插件 ID
        :param plugin_dir: 插件根目录
        :param staging_dir: 已就位的暂存内容目录
        :param source_label: 登记版本元信息使用的来源标签
        :return: (落盘后的内容目录, 错误信息)；失败时内容目录为 None
        """
        try:
            target = self._install_target_resolver(pid, plugin_dir, staging_dir)
        except Exception as error:  # noqa: BLE001 - 组合根注入的解析端口失败按安装失败处理
            return None, f"解析插件安装目标失败：{error}"

        final_dir = plugin_dir if target is None else plugin_dir / target.subdirectory
        try:
            self.__swap_staged_plugin_content(staging_dir, final_dir)
        except OSError as error:
            return None, f"写入插件内容失败：{error}"

        if target is not None:
            try:
                self._install_version_registrar(plugin_dir, target.version, source_label)
            except Exception as error:  # noqa: BLE001 - 组合根注入的登记端口失败按安装失败处理
                return None, f"登记插件版本元信息失败：{error}"

        return final_dir, ""

    def __install_flow_sync(
        self,
        pid: str,
        force_install: bool,
        prepare_content: Callable[[Path], tuple[bool, str]],
        repo_url: Optional[str] = None,
        before_dependency_install: Optional[Callable[[], None]] = None,
        source_label: str = "market",
    ) -> tuple[bool, str]:
        """
        同步安装统一流程：暂存内容→并存检查→备份→落位→安装依赖→上报
        prepare_content 负责把插件文件放到调用时给定的暂存目录；只有暂存内容
        齐备且并存检查通过后，才会触碰插件根目录，任一步失败插件根目录都保持
        改动前的状态。
        """
        plugin_dir = self.__plugin_dir(pid)
        staging_dir = self.__new_install_staging_dir(pid)
        try:
            success, message = prepare_content(staging_dir)
            if not success:
                logger.error(f"{pid} 准备插件内容失败：{message}")
                return False, message

            rejection = self._version_switch_guard(pid, plugin_dir, staging_dir)
            if rejection:
                logger.warning(f"{pid} 安装被并存检查拒绝：{rejection}")
                return False, rejection

            backup_dir = None
            if not force_install:
                backup_dir = self.__backup_plugin(pid)

            content_dir, place_message = self.__place_staged_plugin_content(
                pid, plugin_dir, staging_dir, source_label,
            )
            if content_dir is None:
                logger.error(f"{pid} 落位插件内容失败：{place_message}")
                if backup_dir:
                    self.__restore_plugin(pid, backup_dir)
                    logger.warn(f"{pid} 插件安装失败，已还原备份插件")
                else:
                    self.__remove_old_plugin(pid)
                    logger.warn(f"{pid} 已清理对应插件目录，请尝试重新安装")
                return False, place_message

            dependencies_exist, dep_ok, dep_msg = self.__install_dependencies_if_required(
                pid, content_dir, before_dependency_install,
            )
            if dependencies_exist and not dep_ok:
                logger.error(f"{pid} 依赖安装失败：{dep_msg}")
                if backup_dir:
                    self.__restore_plugin(pid, backup_dir)
                    logger.warn(f"{pid} 插件安装失败，已还原备份插件")
                else:
                    self.__remove_old_plugin(pid)
                    logger.warn(f"{pid} 已清理对应插件目录，请尝试重新安装")
                return False, dep_msg

            if backup_dir:
                shutil.rmtree(backup_dir, ignore_errors=True)
            return True, ""
        finally:
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)

    @staticmethod
    def __validate_release_zip_name(name: str) -> None:
        """
        校验 release zip 成员名在 POSIX 与 Windows 语义下都只能表示相对路径。
        """
        if not name:
            raise ValueError("非法 Release 压缩包成员：成员名为空")
        if "\x00" in name:
            raise ValueError(f"非法 Release 压缩包成员：{name}")
        if "\\" in name:
            raise ValueError(f"非法 Release 压缩包成员：{name}")

        posix_path = PurePosixPath(name)
        windows_path = PureWindowsPath(name)
        if (
            name.startswith("//")
            or posix_path.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
        ):
            raise ValueError(f"非法 Release 压缩包成员：{name}")

        parts = [part for part in posix_path.parts if part not in ("", ".")]
        if not parts:
            raise ValueError(f"非法 Release 压缩包成员：{name}")
        if ".." in parts:
            raise ValueError(f"非法 Release 压缩包成员：{name}")

    @staticmethod
    def __validate_release_zip_type(info: zipfile.ZipInfo) -> None:
        """
        release zip 只接受普通文件和目录，避免归档内的符号链接或设备文件影响安装边界。
        """
        mode = info.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        if not file_type:
            return
        if stat.S_ISREG(mode) or stat.S_ISDIR(mode):
            return
        raise ValueError(f"非法 Release 压缩包成员：{info.filename}")

    @staticmethod
    def __get_release_zip_base_prefix(infos: list[zipfile.ZipInfo]) -> str:
        """
        识别 release zip 的单一顶层目录，用于保持插件包根目录剥离行为。
        """
        names = [info.filename for info in infos]
        names_with_slash = [name for name in names if "/" in name]
        if names_with_slash and len(names_with_slash) == len(names):
            first_seg = names_with_slash[0].split("/", 1)[0]
            if first_seg and all(name.startswith(first_seg + "/") for name in names):
                return first_seg + "/"
        return ""

    @classmethod
    def __iter_release_zip_targets(
        cls, zf: zipfile.ZipFile, dest_base: Path
    ) -> list[tuple[zipfile.ZipInfo, Path, bool]]:
        """
        将 release zip 成员解析为安装目标路径，并保证目标路径不会逃逸插件目录。
        """
        infos = zf.infolist()
        for info in infos:
            cls.__validate_release_zip_type(info)
            cls.__validate_release_zip_name(info.filename)

        base_prefix = cls.__get_release_zip_base_prefix(infos)
        dest_root = dest_base.resolve()
        targets = []
        for info in infos:
            raw_name = info.filename
            rel_name = raw_name[len(base_prefix):] if base_prefix else raw_name
            if not rel_name:
                if base_prefix and raw_name == base_prefix:
                    continue
                raise ValueError(f"非法 Release 压缩包成员：{raw_name}")

            cls.__validate_release_zip_name(rel_name)
            rel_parts = [part for part in PurePosixPath(rel_name).parts if part not in ("", ".")]
            if not rel_parts:
                raise ValueError(f"非法 Release 压缩包成员：{raw_name}")
            dest_path = (dest_root / Path(*rel_parts)).resolve()
            try:
                dest_path.relative_to(dest_root)
            except ValueError as exc:
                raise ValueError(f"非法 Release 压缩包成员：{raw_name}") from exc
            targets.append((info, dest_path, info.is_dir()))
        return targets

    def __install_from_release(
        self, pid: str, user_repo: str, release_tag: str, dest_root: Path
    ) -> tuple[bool, str]:
        """
        通过 GitHub Release 资产文件安装插件。
        规范：release 中存在名为 "{pid}_v{version}.zip" 的资产，zip 根即插件文件；
        将其全部解压到 dest_root
        """
        # 拼接资产文件名
        asset_name = f"{release_tag.lower()}.zip"

        release_api = f"https://api.github.com/repos/{user_repo}/releases/tags/{release_tag}"
        rel_res = self.__request_with_fallback(
            release_api,
            headers=get_runtime_setting('REPO_GITHUB_HEADERS')(repo=user_repo),
            timeout=30,
            is_api=True,
        )
        if rel_res is None:
            return False, "获取 Release 信息失败：连接失败"
        if rel_res.status_code == 404:
            return False, f"{release_tag} 插件发布包不存在"
        if rel_res.status_code != 200:
            return False, f"获取 Release 信息失败：{rel_res.status_code}"

        try:
            rel_json = rel_res.json()
            assets = rel_json.get("assets") or []
            asset = next((a for a in assets if a.get("name") == asset_name), None)
            if not asset:
                return False, f"未找到资产文件：{asset_name}"
            asset_id = asset.get("id")
            if not asset_id:
                return False, "资产缺少ID信息"
            # 构建资产的API下载URL
            download_url = f"https://api.github.com/repos/{user_repo}/releases/assets/{asset_id}"
        except Exception as e:
            logger.error(f"解析 Release 信息失败：{e}")
            return False, f"解析 Release 信息失败：{e}"

        # 使用资产的API端点下载，需要设置Accept头为application/octet-stream
        headers = get_runtime_setting('REPO_GITHUB_HEADERS')(repo=user_repo).copy()
        headers["Accept"] = "application/octet-stream"
        res = self.__request_with_fallback(download_url, headers=headers, is_api=True)
        if res is None or res.status_code != 200:
            return False, f"下载资产失败：{res.status_code if res else '连接失败'}"

        try:
            with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
                infos = zf.infolist()
                if not infos:
                    return False, "压缩包内容为空"
                targets = self.__iter_release_zip_targets(zf, dest_root)
                wrote_any = False
                for info, dest_path, is_dir in targets:
                    if is_dir:
                        dest_path.mkdir(parents=True, exist_ok=True)
                        continue
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info, 'r') as src, open(dest_path, 'wb') as dst:
                        dst.write(src.read())
                    wrote_any = True
                if not wrote_any:
                    return False, "压缩包中无可写入文件"
            return True, ""
        except Exception as e:
            logger.error(f"解压 Release 压缩包失败：{e}")
            return False, f"解压 Release 压缩包失败：{e}"

    async def __async_get_file_list(self, pid: str, user_repo: str, package_version: Optional[str] = None) -> \
            tuple[Optional[list[dict[str, Any]]], Optional[str]]:
        """
        异步获取插件的文件列表
        :param pid: 插件 ID
        :param user_repo: GitHub 仓库的 user/repo 路径
        :return: (文件列表, 错误信息)
        """
        file_api = f"https://api.github.com/repos/{user_repo}/contents/plugins"
        # 如果 package_version 存在（如 "v2"），则加上版本号
        if package_version:
            file_api += f".{package_version}"
        file_api += f"/{pid.lower()}"

        res = await self.__async_request_with_fallback(file_api,
                                                       headers=get_runtime_setting('REPO_GITHUB_HEADERS')(repo=user_repo),
                                                       is_api=True,
                                                       timeout=30)
        if res is None:
            return None, "连接仓库失败"
        elif res.status_code == 404:
            return None, "插件源码目录不存在"
        elif res.status_code != 200:
            return None, f"连接仓库失败：{res.status_code} - " \
                         f"{'超出速率限制，请设置Github Token或稍后重试' if res.status_code == 403 else res.text}"

        try:
            ret = res.json()
            if isinstance(ret, list) and len(ret) > 0 and "message" not in ret[0]:
                return ret, ""
            else:
                return None, "插件在仓库中不存在或返回数据格式不正确"
        except Exception as e:
            logger.error(f"插件数据解析失败：{e}")
            return None, "插件数据解析失败"

    async def __async_download_files(self, pid: str, file_list: list[dict[str, Any]], user_repo: str,
                                     package_version: Optional[str], dest_root: Path) -> tuple[bool, str]:
        """
        异步下载插件文件
        :param pid: 插件 ID
        :param file_list: 要下载的文件列表，包含文件的元数据（包括下载链接）
        :param user_repo: GitHub 仓库的 user/repo 路径
        :param dest_root: 文件落盘的目标根目录
        :return: (是否成功, 错误信息)
        """
        if not file_list:
            return False, "文件列表为空"

        # 使用栈结构来替代递归调用，避免递归深度过大问题
        stack = [(pid, file_list)]

        while stack:
            current_pid, current_file_list = stack.pop()

            for item in current_file_list:
                download_url = item.get("download_url")
                if isinstance(download_url, str) and download_url:
                    try:
                        resolved_path = self.__resolve_download_file(
                            pid,
                            item.get("path"),
                            package_version,
                            dest_root,
                        )
                    except ValueError as error:
                        return False, str(error)
                    logger.debug(f"正在下载文件：{item.get('path')}")
                    res = await self.__async_request_with_fallback(download_url,
                                                                   headers=get_runtime_setting('REPO_GITHUB_HEADERS')(repo=user_repo))
                    if not res:
                        return False, f"文件 {item.get('path')} 下载失败！"
                    elif res.status_code != 200:
                        return False, f"下载文件 {item.get('path')} 失败：{res.status_code}"

                    # 创建插件文件夹并写入文件
                    file_path = AsyncPath(resolved_path)
                    await file_path.parent.mkdir(parents=True, exist_ok=True)
                    async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
                        await f.write(res.text)
                    logger.debug(f"文件 {item.get('path')} 下载成功，保存路径：{file_path}")
                else:
                    # 如果是子目录，则将子目录内容加入栈中继续处理
                    directory_name = item.get("name")
                    if (
                        not isinstance(directory_name, str)
                        or directory_name in {"", ".", ".."}
                        or PurePosixPath(directory_name).name != directory_name
                        or PureWindowsPath(directory_name).name != directory_name
                    ):
                        return False, "插件目录路径无效"
                    sub_list, msg = await self.__async_get_file_list(f"{current_pid}/{directory_name}", user_repo,
                                                                     package_version)
                    if not sub_list:
                        return False, msg or "插件子目录读取失败"
                    stack.append((f"{current_pid}/{directory_name}", sub_list))

        return True, ""

    async def __async_backup_plugin(self, pid: str) -> Optional[str]:
        """
        异步备份旧插件目录
        :param pid: 插件 ID
        :return: 备份目录路径
        """
        plugin_dir = AsyncPath(self._plugins_root()) / pid.lower()
        backup_dir = AsyncPath(get_runtime_setting('TEMP_PATH')) / "plugin_backup" / pid.lower()

        if await plugin_dir.exists():
            try:
                if await backup_dir.exists():
                    await aioshutil.rmtree(backup_dir, ignore_errors=True)
                    logger.debug(f"{pid} 旧的备份目录已清理 {backup_dir}")

                await self._async_copytree(plugin_dir, backup_dir)
                logger.debug(f"{pid} 插件已备份到 {backup_dir}")
            except asyncio.CancelledError:
                await aioshutil.rmtree(backup_dir, ignore_errors=True)
                raise

        return str(backup_dir) if await backup_dir.exists() else None

    async def __async_restore_plugin(self, pid: str, backup_dir: str) -> None:
        """
        异步还原旧插件目录
        :param pid: 插件 ID
        :param backup_dir: 备份目录路径
        """
        plugin_dir = AsyncPath(self._plugins_root()) / pid.lower()
        if await plugin_dir.exists():
            await aioshutil.rmtree(plugin_dir, ignore_errors=True)
            logger.debug(f"{pid} 已清理插件目录 {plugin_dir}")

        backup_path = AsyncPath(backup_dir)
        if await backup_path.exists():
            await self._async_copytree(src=backup_path, dst=plugin_dir)
            logger.debug(f"{pid} 已还原插件目录 {plugin_dir}")
            await aioshutil.rmtree(backup_path, ignore_errors=True)
            logger.debug(f"{pid} 已删除备份目录 {backup_dir}")

    async def __async_remove_old_plugin(self, pid: str) -> None:
        """
        异步删除旧插件
        :param pid: 插件 ID
        """
        plugin_dir = AsyncPath(self._plugins_root()) / pid.lower()
        if await plugin_dir.exists():
            await aioshutil.rmtree(plugin_dir, ignore_errors=True)

    async def _async_copytree(self, src: AsyncPath, dst: AsyncPath) -> None:
        """
        异步递归复制目录
        :param src: 源目录
        :param dst: 目标目录
        """
        if not await src.exists():
            return

        await dst.mkdir(parents=True, exist_ok=True)

        async for item in src.iterdir():
            dst_item = dst / item.name
            if await item.is_dir():
                await self._async_copytree(item, dst_item)
            else:
                async with aiofiles.open(item, 'rb') as src_file:
                    content = await src_file.read()
                async with aiofiles.open(dst_item, 'wb') as dst_file:
                    await dst_file.write(content)

    async def __async_install_dependencies_if_required(
        self,
        pid: str,
        content_dir: Path,
        before_dependency_install: Optional[Callable[[], None]] = None,
    ) -> tuple[bool, bool, str]:
        """
        异步安装插件依赖。
        :param pid: 插件 ID
        :param content_dir: 插件本次已落盘的源码目录
        :return: (是否存在依赖，安装是否成功, 错误信息)
        """
        try:
            manifest = load_dependency_manifest(content_dir)
        except PluginDependencyManifestError as error:
            logger.error(f"{pid} 依赖清单无效：{error}")
            return True, False, str(error)
        if manifest is not None:
            if (
                before_dependency_install is not None
                and dependency_manifest_declares_installation(manifest)
            ):
                try:
                    await _await_thread_operation(before_dependency_install)
                except Exception as error:  # noqa: BLE001 - 观察失败不能阻断安装
                    logger.warning(f"{pid} 依赖安装前状态记录失败：{error}")
            logger.info(f"{pid} 存在依赖，开始尝试安装依赖")
            success, error_message = await self.async_install_packages_with_fallback(
                manifest.path
            )
            return True, success, "" if success else error_message

        return False, False, "不存在依赖"

    async def async_install_raw(
            self,
            pid: str,
            repo_url: str,
            package_version: Optional[str] = None,
            release_version: Optional[str] = None,
            force_install: bool = False,
            before_dependency_install: Optional[Callable[[], None]] = None,
    ) -> tuple[bool, str]:
        """执行已通过来源准入的异步包安装，不负责身份或运行态提交。"""
        if self.is_local_repo_url(repo_url):
            return cast(
                tuple[bool, str],
                await _await_thread_operation(
                    self.__install_local_package,
                    pid,
                    repo_url,
                    force_install,
                    before_dependency_install,
                ),
            )

        user_repo, error_result = self._resolve_remote_plugin_repository(
            pid, repo_url
        )
        if error_result is not None:
            return error_result
        assert user_repo is not None

        preferred_version = package_version or get_runtime_setting('VERSION_FLAG')
        selected_version = await self.async_get_plugin_package_version(
            pid, repo_url, preferred_version
        )
        if error_result := self._accept_remote_package_version(
            pid, selected_version
        ):
            return error_result
        assert selected_version is not None

        # 2. 统一异步安装流程（release 或文件列表）。
        meta = await self.__async_get_plugin_meta(pid, repo_url, selected_version)
        release_items = (
            await self.async_get_plugin_release_versions(pid, repo_url)
            if release_version
            else []
        )
        selection, message = self._select_remote_install(
            pid=pid,
            user_repo=user_repo,
            package_version=selected_version,
            meta=meta,
            release_version=release_version,
            release_items=release_items,
        )
        if selection is None:
            return False, message

        release_tag = selection.release_tag
        if release_tag and not selection.fallback_to_filelist:
            async def prepare_selected_release(staging_dir: Path) -> tuple[bool, str]:
                return await self.__async_install_from_release(
                    pid,
                    selection.user_repo,
                    release_tag,
                    staging_dir,
                )

            return await self.__install_flow_async(
                pid,
                force_install,
                prepare_selected_release,
                repo_url,
                before_dependency_install,
            )

        if release_tag:
            # 当前索引 Release 失败时回退文件列表，保持同步与异步安装一致。
            async def prepare_release(staging_dir: Path) -> tuple[bool, str]:
                ok, msg = await self.__async_install_from_release(
                    pid,
                    selection.user_repo,
                    release_tag,
                    staging_dir,
                )
                if ok:
                    return True, msg
                logger.warning(f"{pid} Release 安装失败，回退文件列表安装：{msg}")
                await aioshutil.rmtree(staging_dir, ignore_errors=True)
                return await self.__prepare_content_via_filelist_async(
                    pid,
                    selection.user_repo,
                    selection.package_version,
                    staging_dir,
                )

            return await self.__install_flow_async(
                pid,
                force_install,
                prepare_release,
                repo_url,
                before_dependency_install,
            )
        # 未声明 release 打包的插件继续使用文件列表方式安装。
        async def prepare_filelist(staging_dir: Path) -> tuple[bool, str]:
            return await self.__prepare_content_via_filelist_async(
                pid,
                selection.user_repo,
                selection.package_version,
                staging_dir,
            )

        return await self.__install_flow_async(
            pid,
            force_install,
            prepare_filelist,
            repo_url,
            before_dependency_install,
        )

    async def async_install_local_raw(
        self,
        plugin_id: str,
        repo_url: str = "",
        force_install: bool = False,
        before_dependency_install: Optional[Callable[[], None]] = None,
    ) -> tuple[bool, str]:
        """提供给兼容 Facade 的异步本地载荷安装入口。"""
        return await self.async_install_raw(
            pid=plugin_id,
            repo_url=repo_url or self.make_local_repo_url(plugin_id),
            force_install=force_install,
            before_dependency_install=before_dependency_install,
        )

    async def async_backup_plugin(self, plugin_id: str) -> Optional[str]:
        """提供给兼容 Facade 的异步临时备份入口。"""
        return await self.__async_backup_plugin(plugin_id)

    async def async_restore_plugin(
        self, plugin_id: str, backup_dir: str
    ) -> None:
        """提供给兼容 Facade 的异步临时备份恢复入口。"""
        await self.__async_restore_plugin(plugin_id, backup_dir)

    async def async_remove_plugin(self, plugin_id: str) -> None:
        """提供给兼容 Facade 的异步旧载荷清理入口。"""
        await self.__async_remove_old_plugin(plugin_id)

    async def async_install_from_release(
        self, plugin_id: str, user_repo: str, release_tag: str
    ) -> tuple[bool, str]:
        """提供给兼容 Facade 的异步 Release 制品安装入口，直接写入插件运行目录。"""
        return await self.__async_install_from_release(
            plugin_id, user_repo, release_tag, self.__plugin_dir(plugin_id)
        )

    async def __async_get_plugin_meta(self, pid: str, repo_url: str,
                                      package_version: Optional[str]) -> dict[str, Any]:
        """异步读取远端插件元数据，并把异常收敛为空映射。"""
        try:
            plugins = (
                          await self.async_get_plugins(repo_url) if not package_version
                          else await self.async_get_plugins(repo_url, package_version)
                      ) or {}
            meta = plugins.get(pid)
            return meta if isinstance(meta, dict) else {}
        except Exception as e:
            logger.warn(f"获取插件 {pid} 元数据失败：{e}")
            return {}

    async def __install_flow_async(
        self,
        pid: str,
        force_install: bool,
        prepare_content: Callable[[Path], Awaitable[tuple[bool, str]]],
        repo_url: Optional[str] = None,
        before_dependency_install: Optional[Callable[[], None]] = None,
    ) -> tuple[bool, str]:
        """
        异步安装流程：暂存内容→并存检查→备份→落位→安装依赖→上报
        prepare_content 负责把插件文件放到调用时给定的暂存目录；只有暂存内容
        齐备且并存检查通过后，才会触碰插件根目录，任一步失败插件根目录都保持
        改动前的状态。
        """
        plugin_dir = self.__plugin_dir(pid)
        staging_dir = self.__new_install_staging_dir(pid)
        backup_dir = None
        try:
            success, message = await prepare_content(staging_dir)
            if not success:
                logger.error(f"{pid} 准备插件内容失败：{message}")
                return False, message

            rejection = await _await_thread_operation(
                self._version_switch_guard, pid, plugin_dir, staging_dir,
            )
            if rejection:
                logger.warning(f"{pid} 安装被并存检查拒绝：{rejection}")
                return False, rejection

            if not force_install:
                backup_dir = await self.__async_backup_plugin(pid)

            content_dir, place_message = cast(
                tuple[Optional[Path], str],
                await _await_thread_operation(
                    self.__place_staged_plugin_content,
                    pid,
                    plugin_dir,
                    staging_dir,
                    "market",
                ),
            )
            if content_dir is None:
                logger.error(f"{pid} 落位插件内容失败：{place_message}")
                if backup_dir:
                    await self.__async_restore_plugin(pid, backup_dir)
                    logger.warning(f"{pid} 插件安装失败，已还原备份插件")
                else:
                    await self.__async_remove_old_plugin(pid)
                    logger.warning(f"{pid} 已清理对应插件目录，请尝试重新安装")
                return False, place_message

            dependencies_exist, dep_ok, dep_msg = await self.__async_install_dependencies_if_required(
                pid, content_dir, before_dependency_install,
            )
            if dependencies_exist and not dep_ok:
                logger.error(f"{pid} 依赖安装失败：{dep_msg}")
                if backup_dir:
                    await self.__async_restore_plugin(pid, backup_dir)
                    logger.warning(f"{pid} 插件安装失败，已还原备份插件")
                else:
                    await self.__async_remove_old_plugin(pid)
                    logger.warning(f"{pid} 已清理对应插件目录，请尝试重新安装")
                return False, dep_msg

            return True, ""
        except asyncio.CancelledError:
            logger.warning(
                f"{pid} 插件安装被取消，Python 依赖环境可能已经改变"
            )
            raise
        finally:
            if backup_dir:
                await aioshutil.rmtree(backup_dir, ignore_errors=True)
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)

    def __prepare_content_via_filelist_sync(self, pid: str, user_repo: str,
                                            package_version: Optional[str],
                                            dest_root: Path) -> tuple[bool, str]:
        """
        同步准备插件内容，通过文件列表获取插件文件和依赖
        """
        runtime_pid = pid.lower()
        file_list, msg = self.__get_file_list(runtime_pid, user_repo, package_version)
        if not file_list:
            if msg == "插件源码目录不存在":
                return False, f"{pid} {msg}"
            return False, msg or "插件文件列表读取失败"
        ok, m = self.__download_files(runtime_pid, file_list, user_repo, package_version, dest_root)
        if not ok:
            return False, m
        return True, ""

    async def __prepare_content_via_filelist_async(self, pid: str, user_repo: str,
                                                   package_version: Optional[str],
                                                   dest_root: Path) -> tuple[bool, str]:
        """
        异步准备插件内容，通过文件列表获取插件文件和依赖
        """
        runtime_pid = pid.lower()
        file_list, msg = await self.__async_get_file_list(
            runtime_pid,
            user_repo,
            package_version,
        )
        if not file_list:
            if msg == "插件源码目录不存在":
                return False, f"{pid} {msg}"
            return False, msg or "插件文件列表读取失败"
        ok, m = await self.__async_download_files(
            runtime_pid,
            file_list,
            user_repo,
            package_version,
            dest_root,
        )
        if not ok:
            return False, m
        return True, ""

    async def __async_install_from_release(
        self, pid: str, user_repo: str, release_tag: str, dest_root: Path
    ) -> tuple[bool, str]:
        """
        通过 GitHub Release 资产文件安装插件（异步）。
        规范：release 中存在名为 "{pid}_v{version}.zip" 的资产，zip 根即插件文件；
        将其全部解压到 dest_root
        """
        # 拼接资产文件名
        asset_name = f"{release_tag.lower()}.zip"

        release_api = f"https://api.github.com/repos/{user_repo}/releases/tags/{release_tag}"
        rel_res = await self.__async_request_with_fallback(
            release_api,
            headers=get_runtime_setting('REPO_GITHUB_HEADERS')(repo=user_repo),
            timeout=30,
            is_api=True,
        )
        if rel_res is None:
            return False, "获取 Release 信息失败：连接失败"
        if rel_res.status_code == 404:
            return False, f"{release_tag} 插件发布包不存在"
        if rel_res.status_code != 200:
            return False, f"获取 Release 信息失败：{rel_res.status_code}"

        try:
            rel_json = rel_res.json()
            assets = rel_json.get("assets") or []
            asset = next((a for a in assets if a.get("name") == asset_name), None)
            if not asset:
                return False, f"未找到资产文件：{asset_name}"
            asset_id = asset.get("id")
            if not asset_id:
                return False, "资产缺少ID信息"
            # 构建资产的API下载URL
            download_url = f"https://api.github.com/repos/{user_repo}/releases/assets/{asset_id}"
        except Exception as e:
            logger.error(f"解析 Release 信息失败：{e}")
            return False, f"解析 Release 信息失败：{e}"

        # 使用资产的API端点下载，需要设置Accept头为application/octet-stream
        headers = get_runtime_setting('REPO_GITHUB_HEADERS')(repo=user_repo).copy()
        headers["Accept"] = "application/octet-stream"
        res = await self.__async_request_with_fallback(download_url,
                                                       headers=headers,
                                                       is_api=True)
        if res is None or res.status_code != 200:
            return False, f"下载资产失败：{res.status_code if res else '连接失败'}"

        try:
            with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
                infos = zf.infolist()
                if not infos:
                    return False, "压缩包内容为空"
                targets = self.__iter_release_zip_targets(zf, dest_root)
                wrote_any = False
                for info, dest_path, is_dir in targets:
                    async_dest_path = AsyncPath(dest_path)
                    if is_dir:
                        await async_dest_path.mkdir(parents=True, exist_ok=True)
                        continue
                    await async_dest_path.parent.mkdir(parents=True, exist_ok=True)
                    data = await asyncio.to_thread(
                        self.__read_release_zip_member,
                        zf,
                        info,
                    )
                    async with aiofiles.open(dest_path, 'wb') as dst:
                        await dst.write(data)
                    wrote_any = True
                if not wrote_any:
                    return False, "压缩包中无可写入文件"
            return True, ""
        except Exception as e:
            logger.error(f"解压 Release 压缩包失败：{e}")
            return False, f"解压 Release 压缩包失败：{e}"

    @staticmethod
    def __read_release_zip_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
        """在线程池读取并解压单个 Release 文件，避免阻塞事件循环。"""
        with zf.open(info, "r") as source:
            return source.read()

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
