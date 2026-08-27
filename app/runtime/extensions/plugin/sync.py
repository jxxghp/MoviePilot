"""插件市场同步运行时用例。"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Optional

from app.runtime.extensions.plugin.system import PluginSystemServices


class PluginSyncService:
    """根据已安装清单同步缺失或过期插件，不参与插件实例生命周期。"""

    def __init__(
        self,
        *,
        frozen: Callable[[], bool],
        installed_plugins: Callable[[], list[str]],
        online_plugins: Callable[[], list[Any]],
        local_plugins: Callable[[], list[Any]],
        merge_plugins: Callable[[list[Any], list[Any], list[Any]], list[Any]],
        plugin_exists: Callable[[str, Optional[str]], bool],
        install: Callable[[str, Optional[str], bool, object | None], tuple[bool, str]],
        log: Any,
    ) -> None:
        """保存目录读取、包安装和持久化报告端口。"""
        self._frozen = frozen
        self._installed_plugins = installed_plugins
        self._online_plugins = online_plugins
        self._local_plugins = local_plugins
        self._merge_plugins = merge_plugins
        self._plugin_exists = plugin_exists
        self._install = install
        self._logger = log

    def sync(
        self,
        startup_token: object | None = None,
        *,
        online_restore_plugins: set[str] | None = None,
    ) -> list[str]:
        """并发安装本地缺失、需要更新或应恢复在线载荷的插件。"""
        if self._frozen():
            return []

        installed = {
            plugin_id.lower()
            for plugin_id in self._installed_plugins()
        }
        online = self._online_plugins()
        local = self._local_plugins()
        local_plugin_ids = {
            plugin.id.lower()
            for plugin in local
        }
        deferred_plugin_ids = installed & local_plugin_ids
        restore_plugin_ids = {
            plugin_id.lower()
            for plugin_id in (online_restore_plugins or set())
        } - local_plugin_ids
        candidates = self._merge_plugins(online + local, [], []) if online or local else []
        targets = [
            plugin
            for plugin in candidates
            if plugin.id.lower() in installed
            and (
                plugin.id.lower() in deferred_plugin_ids
                or plugin.id.lower() in restore_plugin_ids
                or (
                    plugin.system_version_compatible is not False
                    and not self._plugin_exists(plugin.id, plugin.plugin_version)
                )
            )
        ]
        if not targets:
            return []

        self._logger.info("开始安装第三方插件...")
        synced: list[str] = []
        failed: list[str] = []
        failed_deferred: list[str] = []

        def install_one(plugin: Any) -> None:
            """安装一个插件并记录结果。"""
            started = time.time()
            state, message = self._install(
                plugin.id,
                None,
                False,
                startup_token,
            )
            elapsed = time.time() - started
            if state:
                self._logger.info(
                    f"插件 {plugin.plugin_name} 同步成功，耗时：{elapsed:.2f} 秒"
                )
                synced.append(plugin.id)
            else:
                if plugin.id.lower() in deferred_plugin_ids:
                    failed_deferred.append(plugin.id)
                self._logger.error(
                    f"插件 {plugin.plugin_name} 同步失败："
                    f"{message}，耗时：{elapsed:.2f} 秒"
                )
                failed.append(plugin.id)

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(install_one, plugin): plugin for plugin in targets}
            for future in as_completed(futures):
                plugin = futures[future]
                try:
                    future.result()
                except Exception as error:  # noqa: BLE001
                    if plugin.id.lower() in deferred_plugin_ids:
                        failed_deferred.append(plugin.id)
                    self._logger.error(
                        f"插件 {plugin.plugin_name} 安装过程中出现异常: {error}"
                    )

        self._logger.info(
            f"第三方插件安装完成，成功：{len(synced)} 个，失败：{len(failed)} 个"
        )
        if failed_deferred:
            raise RuntimeError(
                "延后激活的插件同步未完成："
                f"{', '.join(sorted(set(failed_deferred)))}"
            )
        return synced


class LocalPluginSyncService:
    """同步本地插件仓源码到运行目录，并记录热重载抑制窗口。"""

    def __init__(
        self,
        *,
        installed_plugins: Callable[[], list[str]],
        candidate: Callable[[str], Optional[dict]],
        system: Callable[[], PluginSystemServices],
        recent_sync: dict[str, float],
        log: Any,
    ) -> None:
        """保存本地候选、包同步和运行态监控端口。"""
        self._installed_plugins = installed_plugins
        self._candidate = candidate
        self._system = system
        self._recent_sync = recent_sync
        self._logger = log

    def sync(self, plugin_id: str, candidate: Optional[dict] = None) -> bool:
        """同步已安装且兼容的本地插件，成功后记录短时事件抑制标记。"""
        normalized_plugin_id = plugin_id.lower()
        installed = {
            installed_id.lower()
            for installed_id in self._installed_plugins()
        }
        if normalized_plugin_id not in installed:
            self._logger.info(f"本地插件 {plugin_id} 尚未安装，跳过自动同步和热重载")
            return False
        candidate = candidate or self._candidate(plugin_id)
        if not candidate or candidate.get("compatible") is False:
            if candidate:
                self._logger.info(
                    f"本地插件 {plugin_id} 不满足同步条件，跳过同步："
                    f"{candidate.get('skip_reason')}"
                )
            return False
        repo_url = candidate.get("repo_url")
        if not isinstance(repo_url, str) or not repo_url.startswith("local://"):
            self._logger.error(f"本地插件 {plugin_id} 缺少可验证的本地来源标识")
            return False
        try:
            state, message = self._system().install_plugin(
                plugin_id=plugin_id,
                repo_url=repo_url,
                package_version=candidate.get("package_version") or None,
                force=True,
                local_sync=True,
                explicit_source=True,
            )
            if not state:
                self._logger.error(f"同步本地插件 {plugin_id} 失败：{message}")
                return False
            self._recent_sync[normalized_plugin_id] = time.time()
            self._logger.info(f"已同步本地插件 {plugin_id}")
            return True
        except Exception as error:
            self._logger.error(f"同步本地插件 {plugin_id} 失败：{error}")
            return False
