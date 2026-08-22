"""插件分身创建运行时用例。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional

from app.schemas.plugin import PluginInstance, PluginRuntimeStatus


class PluginCloneService:
    """创建共享源码的虚拟插件实例并协调失败回滚。"""

    def __init__(
        self,
        *,
        plugin_class: Callable[[str], Optional[Any]],
        plugin_exists: Callable[[str], bool],
        source_plugin_id: Callable[[str], str],
        save_instance: Callable[[PluginInstance], Any],
        delete_instance: Callable[[str], bool],
        read_config: Callable[[str], dict],
        save_config: Callable[[str, dict], bool],
        delete_config: Callable[[str], bool],
        reload_plugin: Callable[[str], Any],
        remove_plugin: Callable[[str], Any],
        log: Any,
    ) -> None:
        """保存实例描述、持久化和运行态端口。"""
        self._plugin_class = plugin_class
        self._plugin_exists = plugin_exists
        self._source_plugin_id = source_plugin_id
        self._save_instance = save_instance
        self._delete_instance = delete_instance
        self._read_config = read_config
        self._save_config = save_config
        self._delete_config = delete_config
        self._reload_plugin = reload_plugin
        self._remove_plugin = remove_plugin
        self._logger = log

    def clone(
        self,
        *,
        plugin_id: str,
        suffix: str,
        name: str,
        description: str,
        version: Optional[str] = None,
        icon: Optional[str] = None,
    ) -> tuple[bool, str]:
        """创建虚拟分身，复制隔离配置并保持默认禁用语义。"""
        if not plugin_id or not suffix:
            return False, "插件ID和分身后缀不能为空"
        if self._plugin_class(plugin_id) is None:
            return False, f"原插件 {plugin_id} 不存在"

        clone_id = f"{plugin_id}{suffix.lower()}"
        if self._plugin_exists(clone_id):
            return False, f"分身插件 {clone_id} 已存在"

        try:
            instance = PluginInstance(
                instance_id=clone_id,
                source_plugin_id=self._source_plugin_id(plugin_id),
                plugin_name=name or None,
                plugin_desc=description or None,
                plugin_icon=icon or None,
            )
            self._save_instance(instance)

            original_config = self._read_config(plugin_id)
            if original_config:
                clone_config = dict(original_config)
                clone_config["enable"] = False
                clone_config["enabled"] = False
                if not self._save_config(clone_id, clone_config):
                    raise RuntimeError("虚拟实例配置保存失败")

            status = self._reload_plugin(clone_id)
            if status is PluginRuntimeStatus.LOAD_FAILED:
                raise RuntimeError("虚拟实例加载失败")
            self._logger.info(f"插件分身 {clone_id} 创建成功")
            return True, clone_id
        except Exception as error:  # noqa: BLE001
            self._rollback(clone_id)
            self._logger.error(f"创建插件分身失败：{error}")
            return False, f"创建插件分身失败：{error}"

    def _rollback(self, clone_id: str) -> None:
        """逐项清理失败实例，单个清理错误不得阻断其余回滚。"""
        rollback_steps = (
            ("运行态", self._remove_plugin),
            ("实例描述", self._delete_instance),
            ("配置", self._delete_config),
        )
        for label, rollback in rollback_steps:
            try:
                rollback(clone_id)
            except Exception as rollback_error:  # noqa: BLE001
                self._logger.warning(
                    f"回滚插件分身 {clone_id} 的{label}失败：{rollback_error}"
                )
