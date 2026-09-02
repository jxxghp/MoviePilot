"""插件已装版本查询与虚拟实例版本绑定切换。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

from app.runtime.extensions.plugin.version import (
    plugin_version_dirs,
    read_plugin_versions_manifest,
)
from app.schemas.plugin import PluginInstance, PluginRuntimeStatus

StartInstance = Callable[[str, Optional[str]], dict[str, PluginRuntimeStatus]]
MultiVersionBlockers = Callable[[str, list[Path]], list[str]]


class PluginVersionBinding:
    """组装插件版本总览，并执行实例级版本绑定切换。"""

    def __init__(
        self,
        *,
        plugins_root: Path,
        plugin_exists: Callable[[str], bool],
        get_instance: Callable[[str], Optional[PluginInstance]],
        instances_for_source: Callable[[str], list[PluginInstance]],
        save_instance: Callable[[PluginInstance], None],
        running: Callable[[], dict[str, Any]],
        start: StartInstance,
        stop: Callable[[str], None],
        multi_version_blockers: MultiVersionBlockers,
        log: Any,
    ) -> None:
        """保存版本目录、实例持久化和生命周期端口。"""
        self._plugins_root = plugins_root
        self._plugin_exists = plugin_exists
        self._get_instance = get_instance
        self._instances_for_source = instances_for_source
        self._save_instance = save_instance
        self._running = running
        self._start = start
        self._stop = stop
        self._multi_version_blockers = multi_version_blockers
        self._logger = log

    def _plugin_root(self, plugin_id: str) -> Path:
        """定位插件源码根目录。"""
        return self._plugins_root / plugin_id.lower()

    def _current_version(self, plugin_id: str) -> Optional[str]:
        """读取版本元信息登记的当前版本号。"""
        manifest = read_plugin_versions_manifest(self._plugin_root(plugin_id))
        current = manifest.get("current")
        return current if isinstance(current, str) and current else None

    def overview(self, plugin_id: str) -> dict[str, Any]:
        """组装插件已装版本列表与各实例的版本绑定。

        :param plugin_id: 插件ID
        :return: 含已装版本列表与各实例绑定信息的字典
        :raise LookupError: 插件不存在
        """
        if not self._plugin_exists(plugin_id):
            raise LookupError(f"插件 {plugin_id} 不存在")
        plugin_root = self._plugin_root(plugin_id)
        manifest = read_plugin_versions_manifest(plugin_root)
        current_version = self._current_version(plugin_id)
        registered = {
            entry.get("version"): entry
            for entry in (manifest.get("versions") or [])
            if isinstance(entry, dict)
        }
        installed_versions = [
            {
                "version": version,
                "directory": path.name,
                "installed_at": (registered.get(version) or {}).get("installed_at"),
                "source": (registered.get(version) or {}).get("source"),
                "is_current": version == current_version,
            }
            for version, path in sorted(plugin_version_dirs(plugin_root).items())
        ]
        running = self._running()
        instances = [
            {
                "instance_id": instance.instance_id,
                "plugin_version": instance.plugin_version,
                "follow_current_version": instance.follow_current_version,
                "running": instance.instance_id in running,
            }
            for instance in self._instances_for_source(plugin_id)
        ]
        return {
            "plugin_id": plugin_id,
            "current_version": current_version,
            "installed_versions": installed_versions,
            "instances": instances,
        }

    def _instance_expected_version(
        self,
        instance: PluginInstance,
        current_version: Optional[str],
    ) -> Optional[str]:
        """解析实例按其绑定本应运行的版本，供并存判定使用。"""
        if instance.follow_current_version:
            return current_version
        return instance.plugin_version or current_version

    def _creates_version_coexistence(
        self,
        instance: PluginInstance,
        target_version: str,
    ) -> bool:
        """判断把指定实例切到目标版本后，该插件是否会出现多版本同时在跑。"""
        current_version = self._current_version(instance.source_plugin_id)
        versions = {current_version} if current_version else set()
        for sibling in self._instances_for_source(instance.source_plugin_id):
            if sibling.instance_id == instance.instance_id:
                continue
            sibling_version = self._instance_expected_version(sibling, current_version)
            if sibling_version:
                versions.add(sibling_version)
        versions.add(target_version)
        return len(versions) > 1

    def set_instance_version(
        self,
        instance_id: str,
        *,
        follow_current_version: bool,
        plugin_version: Optional[str] = None,
    ) -> tuple[bool, str]:
        """设置实例的版本绑定，并立即完成一次停止再启动。

        不跟随当前版本时校验目标版本已安装；如本次切换会让该插件的多个实例
        分处不同版本，先跑多版本并存静态扫描，命中阻断原因即拒绝切换、不做
        任何改动。切换走停止再启动的完整生命周期，不做热替换：热替换等于在
        运行期换掉一个已注册事件、已起定时任务、可能有在途请求的实例。目标
        版本启动失败时已生效版本保持不动，以该版本重新启动完成回退；回退同样
        失败才判定本次切换失败，失败过程全程记录明确日志。

        :param instance_id: 实例ID
        :param follow_current_version: 是否跟随插件当前版本
        :param plugin_version: 不跟随当前版本时的目标版本号
        :return: `(是否成功, 成功时为实例ID／失败时为可读原因)`
        """
        instance = self._get_instance(instance_id)
        if instance is None:
            return False, f"插件实例 {instance_id} 不存在"

        target_version: Optional[str] = None
        if not follow_current_version:
            target_version = (plugin_version or "").strip()
            if not target_version:
                return False, "未跟随当前版本时必须指定目标版本"
            plugin_root = self._plugin_root(instance.source_plugin_id)
            installed = plugin_version_dirs(plugin_root)
            if target_version not in installed:
                return False, f"插件 {instance.source_plugin_id} 未安装版本 {target_version}"
            if self._creates_version_coexistence(instance, target_version):
                blockers = self._multi_version_blockers(
                    instance.source_plugin_id.lower(), list(installed.values())
                )
                if blockers:
                    return False, (
                        f"插件 {instance.source_plugin_id} 的写法不支持多版本并存，"
                        "拒绝切换：" + "；".join(blockers)
                    )

        self._save_instance(
            instance.model_copy(update={"follow_current_version": follow_current_version})
        )
        self._stop(instance_id)
        results = self._start(instance_id, target_version)
        if results.get(instance_id) == PluginRuntimeStatus.ACTIVE:
            return True, instance_id

        if follow_current_version:
            self._logger.error(f"插件实例 {instance_id} 切换为跟随当前版本失败")
            return False, "切换为跟随当前版本失败，请查看插件日志"

        fallback_version = instance.plugin_version
        if not fallback_version or fallback_version == target_version:
            self._logger.error(
                f"插件实例 {instance_id} 切换到版本 {target_version} 失败，"
                "且没有可回退的已生效版本"
            )
            return False, f"切换到版本 {target_version} 失败，请查看插件日志"

        self._logger.error(
            f"插件实例 {instance_id} 切换到版本 {target_version} 失败，"
            f"已生效版本 {fallback_version} 保持不变，正在以该版本重新启动"
        )
        fallback_results = self._start(instance_id, fallback_version)
        if fallback_results.get(instance_id) == PluginRuntimeStatus.ACTIVE:
            return False, f"切换到版本 {target_version} 失败，已回退到原版本 {fallback_version}"

        self._logger.error(
            f"插件实例 {instance_id} 以原版本 {fallback_version} 回退启动同样失败"
        )
        return False, (
            f"切换到版本 {target_version} 失败，回退到原版本 {fallback_version} 同样失败"
        )
