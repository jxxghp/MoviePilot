"""插件运行目录与本地仓库的文件变化监控。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

FederatedChangeResolver = Callable[[Path], Optional[tuple[str, Optional[dict], bool]]]
RuntimePluginResolver = Callable[[Path], Optional[str]]
MonitorSuppression = Callable[[str], bool]
LocalCandidateResolver = Callable[[Path], Optional[dict]]
LocalPluginSync = Callable[[str, Optional[dict]], bool]
PluginReloader = Callable[[str], Any]
DependencyManifestStatus = Callable[[Path], Optional[bool]]
WatchFunction = Callable[..., Any]


class PluginMonitorController:
    """独立管理插件文件监控线程的启动、停止和重建。"""

    def __init__(self, *, runner: Callable[[], None], log: Any) -> None:
        """保存监控循环入口和日志端口，线程状态仅由本组件持有。"""
        self._runner = runner
        self._logger = log
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lifecycle_lock = threading.RLock()
        self._closed = False

    @property
    def stop_event(self) -> threading.Event:
        """返回供 watchfiles 监听的停止事件。"""
        return self._stop_event

    def reload(self, enabled: bool) -> None:
        """按当前配置停止旧线程，并在启用时创建新线程。"""
        stopped = self.stop()
        if enabled and stopped:
            self.start()

    def start(self) -> None:
        """启动唯一的守护监控线程。"""
        with self._lifecycle_lock:
            if self._closed:
                self._logger.info("插件文件修改监测已进入停机封口，跳过启动")
                return
            if self._thread and self._thread.is_alive():
                self._logger.info("插件文件修改监测已经在运行中...")
                return
            self._logger.info("开始监测插件文件修改...")
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._runner, daemon=True)
            self._thread.start()

    def reopen(self) -> bool:
        """为新的应用生命周期解除封口，仍有旧线程时拒绝重开。"""
        with self._lifecycle_lock:
            if self._thread and self._thread.is_alive():
                self._logger.warning("旧插件文件监测线程仍在运行，无法开启新生命周期")
                return False
            self._thread = None
            self._closed = False
            return True

    def stop(self, timeout: float = 5.0) -> bool:
        """临时停止监控线程，并返回其是否在预算内真正退出。"""
        return self._stop_with_budget(timeout=timeout, close=False)

    def close(self, timeout: float = 5.0) -> bool:
        """永久封口当前生命周期，并返回监控线程是否真正退出。"""
        return self._stop_with_budget(timeout=timeout, close=True)

    def _stop_with_budget(self, *, timeout: float, close: bool) -> bool:
        """在同一预算内取得生命周期锁、设置封口并等待线程退出。"""
        timeout = max(0.0, timeout)
        deadline = time.monotonic() + timeout
        self._stop_event.set()
        if not self._lifecycle_lock.acquire(timeout=timeout):
            self._logger.warning(
                f"插件文件修改监测线程在{timeout:g}秒内未能取得停机所有权。"
            )
            return False
        try:
            if close:
                self._closed = True
            thread = self._thread
            self._stop_event.set()
            if not thread or not thread.is_alive():
                self._thread = None
                self._logger.info("未启用插件文件修改监测，无需停止")
                return True
            self._logger.info("正在停止插件文件修改监测...")
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                self._logger.warning(
                    f"插件文件修改监测线程在{timeout:g}秒内未能正常停止。"
                )
                return False
            self._thread = None
            self._logger.info("插件文件修改监测停止完成")
            return True
        finally:
            self._lifecycle_lock.release()


class PluginChangeMonitor:
    """把文件变化归并为本地同步和运行态重载动作。"""

    def __init__(
        self,
        *,
        runtime_root: Path,
        local_roots: Callable[[], list[Path]],
        stop_event: Any,
        recent_sync: dict[str, float],
        federated_change: FederatedChangeResolver,
        runtime_plugin: RuntimePluginResolver,
        local_candidate: LocalCandidateResolver,
        sync_local: LocalPluginSync,
        reload_plugin: PluginReloader,
        dependency_manifest_status: DependencyManifestStatus,
        watch: WatchFunction,
        log: Any,
        monitor_suppressed: Optional[MonitorSuppression] = None,
    ) -> None:
        """保存监控路径、变化解析器和副作用回调。"""
        self._runtime_root = runtime_root
        self._local_roots = local_roots
        self._stop_event = stop_event
        self._recent_sync = recent_sync
        self._federated_change = federated_change
        self._runtime_plugin = runtime_plugin
        self._monitor_suppressed = monitor_suppressed or (lambda _plugin_id: False)
        self._local_candidate = local_candidate
        self._sync_local = sync_local
        self._reload_plugin = reload_plugin
        self._dependency_manifest_status = dependency_manifest_status
        self._watch = watch
        self._logger = log

    def run(self) -> None:
        """运行 watchfiles 主循环并按批次同步、重载插件。"""
        plugin_paths = [str(self._runtime_root)]
        plugin_paths.extend(
            str(path)
            for path in self._local_roots()
            if path.exists() and path.is_dir()
        )
        self._logger.info(">>> 监控线程已启动，准备进入watch循环...")
        for changes in self._watch(
            *plugin_paths,
            stop_event=self._stop_event,
            rust_timeout=1000,
            yield_on_timeout=True,
        ):
            if not changes:
                continue
            self._process_changes(changes)

    def _process_changes(self, changes: Any) -> None:
        """把一批文件事件归并为最多一次同步和一次重载。"""
        plugins_to_reload = set()
        local_plugins_to_sync = {}
        for _change_type, path_str in changes:
            event_path = Path(path_str)
            if "__pycache__" in event_path.parts:
                continue
            manifest_status = self._dependency_manifest_status(event_path)
            if manifest_status is not None:
                self._handle_dependency_manifest_change(
                    event_path,
                    active=manifest_status,
                )
                continue

            federated_change = self._federated_change(event_path)
            if federated_change:
                plugin_id, candidate, remote_entry_ready = federated_change
                if candidate and remote_entry_ready:
                    if candidate.get("compatible") is False:
                        self._logger.info(
                            f"检测到本地插件 {plugin_id} 联邦构建产物变化，"
                            f"但跳过同步：{candidate.get('skip_reason')}"
                        )
                    elif plugin_id not in local_plugins_to_sync:
                        local_plugins_to_sync[plugin_id] = (
                            candidate,
                            event_path,
                            False,
                        )
                continue

            if event_path.suffix != ".py":
                continue
            runtime_plugin_id = self._runtime_plugin(event_path)
            if runtime_plugin_id and self._monitor_suppressed(runtime_plugin_id):
                self._logger.debug(
                    f"插件 {runtime_plugin_id} 正在写入，跳过本批文件监控重载"
                )
                continue
            candidate = (
                self._local_candidate(event_path)
                if not runtime_plugin_id
                else None
            )
            if runtime_plugin_id:
                last_sync_time = self._recent_sync.get(runtime_plugin_id.lower())
                if last_sync_time and time.time() - last_sync_time < 2:
                    continue
                plugins_to_reload.add(runtime_plugin_id)
            elif candidate:
                if candidate.get("compatible") is False:
                    package_version = candidate.get("package_version")
                    source_root = (
                        f"plugins.{package_version}"
                        if package_version
                        else "plugins"
                    )
                    self._logger.info(
                        f"检测到本地插件 {candidate.get('id')} 文件变化，"
                        f"来源：{source_root}，文件：{event_path}，"
                        f"但跳过同步：{candidate.get('skip_reason')}"
                    )
                    continue
                local_plugins_to_sync[candidate.get("id")] = (
                    candidate,
                    event_path,
                    True,
                )

        for plugin_id, (candidate, event_path, should_reload) in (
            local_plugins_to_sync.items()
        ):
            package_version = candidate.get("package_version")
            source_root = (
                f"plugins.{package_version}" if package_version else "plugins"
            )
            change_name = "Python 文件" if should_reload else "联邦构建产物"
            self._logger.info(
                f"检测到本地插件 {plugin_id} {change_name}变化，"
                f"来源：{source_root}，文件：{event_path}"
            )
            if self._sync_local(plugin_id, candidate) and should_reload:
                plugins_to_reload.add(plugin_id)

        if not plugins_to_reload:
            return
        self._logger.info(
            f"检测到插件文件变化，准备重载: {list(plugins_to_reload)}"
        )
        for plugin_id in plugins_to_reload:
            try:
                self._reload_plugin(plugin_id)
            except Exception as err:
                self._logger.error(
                    f"插件 {plugin_id} 热重载失败: {err}",
                    exc_info=True,
                )

    def _handle_dependency_manifest_change(
        self,
        event_path: Path,
        *,
        active: bool,
    ) -> None:
        """记录依赖文件变化，但不在监控线程中隐式安装依赖。"""
        candidate = self._local_candidate(event_path)
        if not candidate:
            return
        if candidate.get("compatible") is False:
            self._logger.info(
                f"检测到本地插件 {candidate.get('id')} 依赖文件变化，"
                f"但跳过处理：{candidate.get('skip_reason')}"
            )
            return
        if not active:
            self._logger.debug(
                f"检测到本地插件 {candidate.get('id')} 非生效依赖文件变化："
                f"{event_path.name}"
            )
            return
        self._logger.warning(
            f"检测到本地插件 {candidate.get('id')} 依赖文件变化，"
            "请重新安装本地插件以安装依赖"
        )
